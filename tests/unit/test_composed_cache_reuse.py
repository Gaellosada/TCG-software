"""Fund-of-funds composed cache reuse + read-only ``/cache/get`` + version salt.

Covers the cache-consistency work:
  * KEY PARITY (SC2): a composed leg's child sub-body (``_child_request``) hashes
    to the SAME key a STANDALONE compute of that child would — the single most
    important correctness property (children share one cache entry).
  * COMPOSED REUSE (SC1): composing two individually-cached children recomputes
    NEITHER child (only the parent runs) — proven via a compute spy.
  * ``/cache/get`` (SC4/SC6): returns the cached blob on a HIT and ``null`` on a
    MISS, and NEVER computes on a miss (spy asserts zero compute calls).
  * COMPUTE_VERSION bump (SC3): invalidates old keys.

Backend-authoritative: bodies are built by hand, the on-disk cache is isolated
per test by the autouse root-conftest tmp-dir fixture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

import tcg.core.api.portfolio as portfolio
from tcg.core.api.portfolio import (
    PortfolioRequest,
    _child_request,
    _portfolio_cache_key,
)
from tcg.data.service import DefaultMarketDataService
from tcg.types.market import PriceSeries


DATES = [
    20240102,
    20240103,
    20240104,
    20240105,
    20240108,
    20240109,
    20240110,
    20240111,
    20240112,
    20240115,
]
CLOSES_BY_LABEL = {
    "up": [100.0, 101.0, 102.5, 103.0, 105.0, 104.0, 106.0, 108.0, 107.0, 110.0],
    "down": [200.0, 199.0, 198.0, 197.5, 196.0, 197.0, 195.0, 193.0, 194.0, 190.0],
    "flat": [50.0] * 10,
}


def _price_series(close_vals: list[float]) -> PriceSeries:
    n = len(DATES)
    c = np.array(close_vals, dtype=np.float64)
    return PriceSeries(
        dates=np.array(DATES, dtype=np.int64),
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


@pytest.fixture
def mock_app():
    from fastapi import FastAPI
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.core.api.errors import tcg_error_handler
    from tcg.types.errors import TCGError

    common_dates = np.array(DATES, dtype=np.int64)

    async def _aligned(legs_spec):
        return common_dates, {
            label: _price_series(
                CLOSES_BY_LABEL.get(label, [100.0 + i for i in range(len(DATES))])
            )
            for label in legs_spec
        }

    svc = MagicMock()
    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = svc
    app.state.app_db_repo = object()
    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def compute_spy(monkeypatch):
    real = portfolio._compute_portfolio_uncached
    calls = {"n": 0}

    async def _spy(body, svc, classify, repo):
        calls["n"] += 1
        return await real(body, svc, classify, repo)

    monkeypatch.setattr(portfolio, "_compute_portfolio_uncached", _spy)
    return calls


def _child_dict(labels, weights, start="2024-01-02", end="2024-01-15"):
    return {
        "legs": {
            lbl: {"type": "instrument", "collection": "INDEX", "symbol": lbl}
            for lbl in labels
        },
        "weights": {lbl: w for lbl, w in zip(labels, weights)},
        "rebalance": "none",
        "return_type": "normal",
        "start": start,
        "end": end,
    }


def _composed_body(children: dict[str, dict], weights: dict[str, float]):
    return {
        "legs": {
            label: {"type": "portfolio", "portfolio_id": label, "portfolio": child}
            for label, child in children.items()
        },
        "weights": weights,
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-02",
        "end": "2024-01-15",
    }


async def _compute(client, body):
    r = await client.post("/api/portfolio/compute", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── SC2: KEY PARITY (the core invariant) ───────────────────────────────


class TestKeyParity:
    def test_child_body_key_equals_standalone(self):
        """The composed leg's child body hashes to the standalone child's key."""
        child_dict = _child_dict(["up", "down"], [60.0, 40.0])
        standalone = PortfolioRequest(**child_dict)  # what /compute parses
        inlined = PortfolioRequest(**child_dict)  # leg.portfolio, own range
        child_body = _child_request(inlined, use_cache=True)
        assert _portfolio_cache_key(standalone) == _portfolio_cache_key(child_body)

    def test_use_cache_does_not_change_child_key(self):
        child_dict = _child_dict(["up", "down"], [60.0, 40.0])
        standalone = PortfolioRequest(**child_dict)
        inlined = PortfolioRequest(**child_dict)
        # use_cache is stripped from the key at all levels → parity regardless.
        assert _portfolio_cache_key(standalone) == _portfolio_cache_key(
            _child_request(inlined, use_cache=False)
        )

    def test_different_range_keys_differently(self):
        """Sanity: a DIFFERENT child range (the old re-anchor bug) would MISS."""
        own = PortfolioRequest(**_child_dict(["up", "down"], [60.0, 40.0]))
        narrowed = PortfolioRequest(
            **_child_dict(["up", "down"], [60.0, 40.0], start="2024-01-05")
        )
        assert _portfolio_cache_key(_child_request(own, True)) != _portfolio_cache_key(
            _child_request(narrowed, True)
        )


# ── SC1: composed reuse — cached children are NOT recomputed ────────────


class TestComposedReuse:
    async def test_two_cached_children_not_recomputed(self, client, compute_spy):
        child_a = _child_dict(["up", "down"], [60.0, 40.0])
        child_b = _child_dict(["down", "flat"], [50.0, 50.0])

        # Compute each child STANDALONE → each populates its own cache entry.
        await _compute(client, child_a)
        await _compute(client, child_b)
        assert compute_spy["n"] == 2

        # Compose them. The two children must be served from cache (identical
        # keys); only the PARENT runs a compute → exactly one more call.
        composed = _composed_body({"A": child_a, "B": child_b}, {"A": 50.0, "B": 50.0})
        resp = await _compute(client, composed)
        assert set(resp["leg_equities"].keys()) == {"A", "B"}
        assert compute_spy["n"] == 3  # +1 parent only; children reused

    async def test_composed_from_cache_matches_fresh(self, client, compute_spy):
        """SC2 end-to-end: composed result is identical whether the child is
        served from cache or freshly computed."""
        child = _child_dict(["up", "down"], [60.0, 40.0])
        composed = _composed_body({"A": child}, {"A": 100.0})

        fresh = await _compute(client, composed)  # child computed fresh
        cached = await _compute(client, composed)  # whole thing cached
        assert cached["from_cache"] is True
        np.testing.assert_array_equal(
            np.array(fresh["portfolio_equity"]),
            np.array(cached["portfolio_equity"]),
        )


# ── SC4 / SC6: /cache/get is read-only and never computes on a miss ─────


class TestCacheGet:
    async def test_miss_returns_null_and_never_computes(self, client, compute_spy):
        body = _child_dict(["up", "down"], [60.0, 40.0])
        r = await client.post("/api/portfolio/cache/get", json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload == {"result": None, "from_cache": False}
        assert compute_spy["n"] == 0  # a MISS must NEVER compute

    async def test_hit_returns_blob_without_computing(self, client, compute_spy):
        body = _child_dict(["up", "down"], [60.0, 40.0])
        await _compute(client, body)  # populate cache
        assert compute_spy["n"] == 1

        r = await client.post("/api/portfolio/cache/get", json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["from_cache"] is True
        assert payload["result"] is not None
        assert payload["result"]["from_cache"] is True
        assert payload["result"]["computed_ms"] is None
        assert "portfolio_equity" in payload["result"]
        # A HIT reads only — no additional compute.
        assert compute_spy["n"] == 1

    async def test_repeated_get_never_computes(self, client, compute_spy):
        body = _child_dict(["up"], [100.0])
        for _ in range(5):
            r = await client.post("/api/portfolio/cache/get", json=body)
            assert r.json()["result"] is None
        assert compute_spy["n"] == 0


# ── Fund-of-funds date-window clipping (composed slider) ────────────────


class TestComposedDateWindow:
    async def test_portfolio_only_composed_honors_parent_window(self, client):
        """A portfolio-only composed portfolio (children over their OWN full
        range) must clip the composed equity to the parent's [start,end] slider.
        Pre-fix this was ignored (children full-range, no clip) → the slider was
        a no-op."""
        child_a = _child_dict(["up"], [100.0])  # own full range 01-02..01-15
        child_b = _child_dict(["down"], [100.0])
        composed = _composed_body({"A": child_a, "B": child_b}, {"A": 50.0, "B": 50.0})
        # Narrow the PARENT to a sub-window (children stay full-range).
        composed["start"] = "2024-01-08"
        composed["end"] = "2024-01-12"
        resp = await _compute(client, composed)
        assert resp["dates"][0] == "2024-01-08"
        assert resp["dates"][-1] == "2024-01-12"
        assert len(resp["dates"]) == 5  # 01-08,09,10,11,12

    async def test_portfolio_only_composed_full_range_when_unset(self, client):
        """No parent window → the composed equity spans the full child
        intersection (the clip is a no-op)."""
        child_a = _child_dict(["up"], [100.0])
        child_b = _child_dict(["down"], [100.0])
        composed = _composed_body({"A": child_a, "B": child_b}, {"A": 50.0, "B": 50.0})
        composed.pop("start")
        composed.pop("end")
        resp = await _compute(client, composed)
        assert resp["dates"][0] == "2024-01-02"
        assert resp["dates"][-1] == "2024-01-15"
        assert len(resp["dates"]) == len(DATES)

    async def test_out_of_range_parent_window_raises_400(self, client):
        """A parent window with no data in it → the SAME 400 the instrument path
        uses (never a 500)."""
        child = _child_dict(["up"], [100.0])
        composed = _composed_body({"A": child}, {"A": 100.0})
        composed["start"] = "2030-01-01"
        composed["end"] = "2030-12-31"
        r = await client.post("/api/portfolio/compute", json=composed)
        assert r.status_code == 400, r.text
        assert "selected date range" in r.text


# ── Differing child ranges: intersection + re-anchor + disjoint→400 ─────


class TestDifferingChildRanges:
    async def test_partial_overlap_intersects_and_reanchors(self, client):
        # child_a: 01-02..01-10 ; child_b: 01-08..01-15 → overlap 01-08..01-10.
        child_a = _child_dict(["up"], [100.0], start="2024-01-02", end="2024-01-10")
        child_b = _child_dict(["down"], [100.0], start="2024-01-08", end="2024-01-15")
        composed = _composed_body({"A": child_a, "B": child_b}, {"A": 50.0, "B": 50.0})
        resp = await _compute(client, composed)
        assert resp["dates"][0] == "2024-01-08"
        assert resp["dates"][-1] == "2024-01-10"
        assert len(resp["dates"]) == 3
        assert all(v is not None for v in resp["portfolio_equity"])

        # Re-anchor parity: restricting BOTH children to EXACTLY the overlap
        # yields the same parent equity — the composed engine re-anchors to child
        # RETURNS within the overlap, independent of each child's absolute equity
        # level (or pre-overlap history) before the first common bar.
        child_a2 = _child_dict(["up"], [100.0], start="2024-01-08", end="2024-01-10")
        child_b2 = _child_dict(["down"], [100.0], start="2024-01-08", end="2024-01-10")
        composed2 = _composed_body(
            {"A": child_a2, "B": child_b2}, {"A": 50.0, "B": 50.0}
        )
        resp2 = await _compute(client, composed2)
        np.testing.assert_allclose(
            np.array(resp["portfolio_equity"], dtype=float),
            np.array(resp2["portfolio_equity"], dtype=float),
        )

    async def test_disjoint_children_raise_400_not_500(self, client):
        child_a = _child_dict(["up"], [100.0], start="2024-01-02", end="2024-01-04")
        child_b = _child_dict(["down"], [100.0], start="2024-01-11", end="2024-01-15")
        composed = _composed_body({"A": child_a, "B": child_b}, {"A": 50.0, "B": 50.0})
        r = await client.post("/api/portfolio/compute", json=composed)
        assert r.status_code == 400, r.text
        assert "overlapping" in r.text.lower()


# ── /cache/get degrades to a MISS on a cache error (never a 500) ────────


class TestCacheGetErrorDegradation:
    async def test_cache_get_error_returns_miss_not_500(self, client, monkeypatch):
        """A cache glitch (``cache.get`` raising) must degrade to a 200 MISS, so
        a cache error can NEVER block the auto-display UI."""

        class _BoomCache:
            async def get(self, key):
                raise RuntimeError("simulated cache backend failure")

        monkeypatch.setattr(portfolio, "_get_result_cache", lambda: _BoomCache())
        body = _child_dict(["up", "down"], [60.0, 40.0])
        r = await client.post("/api/portfolio/cache/get", json=body)
        assert r.status_code == 200, r.text
        assert r.json() == {"result": None, "from_cache": False}


# ── SC3: COMPUTE_VERSION bump invalidates old keys ──────────────────────


class TestComputeVersionSalt:
    async def test_bump_invalidates(self, client, monkeypatch):
        body = _child_dict(["up", "down"], [60.0, 40.0])
        await _compute(client, body)

        # Same body, current version → HIT.
        hit = await client.post("/api/portfolio/cache/get", json=body)
        assert hit.json()["from_cache"] is True

        # Bump the compute version → the key namespaces away → MISS.
        monkeypatch.setattr(portfolio, "COMPUTE_VERSION", "9.9.9-test")
        miss = await client.post("/api/portfolio/cache/get", json=body)
        assert miss.json() == {"result": None, "from_cache": False}
