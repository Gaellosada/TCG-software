"""``use_cache`` opt-out flag + cache-clear endpoint.

Verifies the Settings toggle: ``use_cache=False`` bypasses the on-disk result
cache entirely (no read, no write, always fresh), the flag propagates to composed
children, and it is EXCLUDED from the cache key (so toggling it never changes
identity). Plus ``POST /api/portfolio/cache/clear`` empties the cache.

The on-disk cache is isolated per test by the autouse root-conftest fixture
(tmp dir); ``portfolio._result_cache`` is that per-test instance, so tests can
inspect ``.count()`` directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

import tcg.core.api.portfolio as portfolio
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
CLOSES_BY_LABEL: dict[str, list[float]] = {
    "up": [100.0, 101.0, 102.5, 103.0, 105.0, 104.0, 106.0, 108.0, 107.0, 110.0],
    "down": [200.0, 199.0, 198.0, 197.5, 196.0, 197.0, 195.0, 193.0, 194.0, 190.0],
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


def _pure_body(labels: list[str], **overrides) -> dict:
    body = {
        "legs": {
            lbl: {"type": "instrument", "collection": "INDEX", "symbol": lbl}
            for lbl in labels
        },
        "weights": {lbl: 100.0 / len(labels) for lbl in labels},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    body.update(overrides)
    return body


def _composed(child: dict, **overrides) -> dict:
    # FUND-OF-FUNDS: the frontend inlines the child's OWN range into
    # ``portfolio.start/end`` (only ``use_cache`` is dropped), so the child
    # sub-body is byte-identical to a standalone compute → shared cache entry.
    child_inlined = {k: v for k, v in child.items() if k != "use_cache"}
    body = {
        "legs": {
            "block": {
                "type": "portfolio",
                "portfolio_id": "c",
                "portfolio": child_inlined,
            }
        },
        "weights": {"block": 100.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": child["start"],
        "end": child["end"],
    }
    body.update(overrides)
    return body


async def _post(client, body):
    r = await client.post("/api/portfolio/compute", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── use_cache default True still hits ──────────────────────────────────


class TestDefaultCachingUnchanged:
    async def test_default_true_hits_on_repeat(self, client, compute_spy):
        body = _pure_body(["up", "down"])  # no use_cache → default True
        first = await _post(client, body)
        assert first["from_cache"] is False
        second = await _post(client, body)
        assert second["from_cache"] is True
        assert compute_spy["n"] == 1  # served from cache the 2nd time
        assert portfolio._result_cache.count() == 1


# ── use_cache False bypasses entirely ──────────────────────────────────


class TestBypass:
    async def test_false_recomputes_every_time_and_never_populates(
        self, client, compute_spy
    ):
        body = _pure_body(["up", "down"], use_cache=False)
        for _ in range(3):
            data = await _post(client, body)
            assert data["from_cache"] is False
            assert isinstance(data["computed_ms"], int)
        assert compute_spy["n"] == 3  # every call recomputed
        assert portfolio._result_cache.count() == 0  # never written

    async def test_false_does_not_read_a_prewarmed_entry(self, client, compute_spy):
        body_cached = _pure_body(["up", "down"])  # populate the cache (True)
        await _post(client, body_cached)
        assert compute_spy["n"] == 1

        body_bypass = _pure_body(["up", "down"], use_cache=False)
        data = await _post(client, body_bypass)
        assert data["from_cache"] is False
        assert compute_spy["n"] == 2  # recomputed despite a warm entry


# ── key excludes use_cache ─────────────────────────────────────────────


class TestKeyExcludesUseCache:
    def test_key_identical_regardless_of_flag(self):
        on = portfolio.PortfolioRequest(**_pure_body(["up", "down"], use_cache=True))
        off = portfolio.PortfolioRequest(**_pure_body(["up", "down"], use_cache=False))
        assert portfolio._portfolio_cache_key(on) == portfolio._portfolio_cache_key(off)

    async def test_true_after_false_hits_the_prior_true_entry(
        self, client, compute_spy
    ):
        # A True compute populates the entry; a later True compute of the SAME
        # body hits it — the intervening flag value never changed identity.
        await _post(client, _pure_body(["up", "down"], use_cache=True))
        again = await _post(client, _pure_body(["up", "down"], use_cache=True))
        assert again["from_cache"] is True
        assert compute_spy["n"] == 1


# ── propagation to composed children ───────────────────────────────────


class TestComposedPropagation:
    async def test_composed_false_recomputes_children_fresh(self, client, compute_spy):
        child = _pure_body(["up", "down"])
        body = _composed(child, use_cache=False)
        # Each composed compute recomputes BOTH the parent and the child (no
        # cache read/write anywhere), so two computes = 4 uncached runs.
        await _post(client, body)
        await _post(client, body)
        assert compute_spy["n"] == 4
        assert portfolio._result_cache.count() == 0

    async def test_composed_true_default_reuses_child(self, client, compute_spy):
        child = _pure_body(["up", "down"])
        # Standalone child (True) populates; composed (True) reuses it.
        await _post(client, child)
        assert compute_spy["n"] == 1
        await _post(client, _composed(child))  # parent computes, child cached
        assert compute_spy["n"] == 2  # +1 parent only, child from cache


# ── legacy None-range composed child passes through as None ─────────────


class TestNoneRangeComposedChild:
    """A legacy composed body whose inlined child OMITS start/end must pass
    through as start=None/end=None (child computes over its FULL data overlap) —
    NEVER a fallback to the parent's range, which would miss the standalone cache
    entry and produce numerically-wrong results (the original re-anchor bug)."""

    _CHILD_NO_RANGE = {
        "legs": {"up": {"type": "instrument", "collection": "INDEX", "symbol": "up"}},
        "weights": {"up": 100.0},
        "rebalance": "none",
        "return_type": "normal",
    }

    def test_child_request_preserves_none_range_and_key_matches_standalone(self):
        standalone = portfolio.PortfolioRequest(**self._CHILD_NO_RANGE)  # None range
        inlined = portfolio.PortfolioRequest(**self._CHILD_NO_RANGE)
        child_body = portfolio._child_request(inlined, use_cache=True)
        assert child_body.start is None
        assert child_body.end is None
        # Key parity with a standalone None-range compute (NOT the parent range).
        assert portfolio._portfolio_cache_key(
            standalone
        ) == portfolio._portfolio_cache_key(child_body)

    async def test_none_range_composed_does_not_500(self, client):
        body = {
            "legs": {
                "block": {
                    "type": "portfolio",
                    "portfolio_id": "c",
                    "portfolio": dict(self._CHILD_NO_RANGE),  # no start/end
                }
            },
            "weights": {"block": 100.0},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-02",
            "end": "2024-01-15",
        }
        r = await client.post("/api/portfolio/compute", json=body)
        assert r.status_code == 200, r.text


# ── clear endpoint ─────────────────────────────────────────────────────


class TestClearEndpoint:
    async def test_clear_empties_cache_then_next_compute_misses(
        self, client, compute_spy
    ):
        body = _pure_body(["up", "down"])
        await _post(client, body)
        assert portfolio._result_cache.count() == 1

        r = await client.post("/api/portfolio/cache/clear")
        assert r.status_code == 200, r.text
        assert r.json() == {"cleared": True}
        assert portfolio._result_cache.count() == 0

        after = await _post(client, body)
        assert after["from_cache"] is False  # miss → recompute
        assert compute_spy["n"] == 2


# ── default TTL resolution (NIT-1) ─────────────────────────────────────


class TestDefaultCacheTtl:
    def test_unset_uses_generous_30_day_default(self, monkeypatch):
        monkeypatch.delenv("TCG_CACHE_TTL_SECONDS", raising=False)
        assert portfolio._default_cache_ttl() == float(30 * 24 * 3600)

    def test_zero_disables_expiry(self, monkeypatch):
        monkeypatch.setenv("TCG_CACHE_TTL_SECONDS", "0")
        assert portfolio._default_cache_ttl() is None

    def test_positive_value_sets_ttl(self, monkeypatch):
        monkeypatch.setenv("TCG_CACHE_TTL_SECONDS", "3600")
        assert portfolio._default_cache_ttl() == 3600.0

    def test_negative_or_garbage_fails_safe_to_no_expiry(self, monkeypatch):
        monkeypatch.setenv("TCG_CACHE_TTL_SECONDS", "-5")
        assert portfolio._default_cache_ttl() is None
        monkeypatch.setenv("TCG_CACHE_TTL_SECONDS", "notanumber")
        assert portfolio._default_cache_ttl() is None

    def test_lazy_cache_applies_default_ttl(self, monkeypatch, tmp_path):
        # A freshly created default cache carries the resolved TTL. Force a
        # rebuild with the env unset, pointed at a tmp file (Sign 10).
        monkeypatch.delenv("TCG_CACHE_TTL_SECONDS", raising=False)
        monkeypatch.setattr(portfolio, "_result_cache", None)
        monkeypatch.setattr(
            portfolio, "_default_cache_path", lambda: str(tmp_path / "ttl_probe.sqlite")
        )
        cache = portfolio._get_result_cache()
        assert cache._ttl == float(30 * 24 * 3600)
