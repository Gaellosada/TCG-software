"""Approach 3 — transparent LRU over composed-portfolio sub-computations.

Covers A3-1..A3-5 (design_spec §7):
  * A3-1 results byte-identical to the uncached path (a cache HIT returns exactly
    what a recompute would);
  * A3-2 a HIT avoids recompute (miss/compute counter);
  * A3-3 editing the child spec → different key → recompute;
  * A3-4 the LRU eviction bound is respected (direct cache unit tests);
  * A3-5 handled by the full suite + import-linter gate.

The endpoint-level tests reuse the composed-portfolio mock shape (one controlled
``PriceSeries`` per requested leg label) so a child compute is cheap and
deterministic. Cache stats (``misses`` == number of real child computes) are the
externally-observable spy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.api._portfolio_leg_cache import BoundedLRUCache, canonical_key
from tcg.core.api.portfolio import _PORTFOLIO_LEG_CACHE
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
    d = np.array(DATES, dtype=np.int64)
    c = np.array(close_vals, dtype=np.float64)
    return PriceSeries(
        dates=d,
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


@pytest.fixture(autouse=True)
def _clear_leg_cache():
    _PORTFOLIO_LEG_CACHE.clear()
    yield
    _PORTFOLIO_LEG_CACHE.clear()


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


def _instrument_leg(label: str) -> dict:
    return {"type": "instrument", "collection": "INDEX", "symbol": label}


def _composed(child_weights: dict[str, float], rebalance: str = "none") -> dict:
    child = {
        "legs": {lbl: _instrument_leg(lbl) for lbl in child_weights},
        "weights": dict(child_weights),
        "rebalance": rebalance,
        "return_type": "normal",
    }
    return {
        "legs": {
            "block": {"type": "portfolio", "portfolio_id": "c", "portfolio": child}
        },
        "weights": {"block": 100.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
    }


# ── A3-1 / A3-2: hit is byte-identical and skips recompute ─────────────


class TestCacheHit:
    async def test_hit_is_byte_identical_and_skips_recompute(self, client: AsyncClient):
        body = _composed({"up": 60.0, "down": 40.0})

        r1 = await client.post("/api/portfolio/compute", json=body)
        assert r1.status_code == 200, r1.text
        assert _PORTFOLIO_LEG_CACHE.misses == 1  # one real child compute
        assert _PORTFOLIO_LEG_CACHE.hits == 0

        r2 = await client.post("/api/portfolio/compute", json=body)
        assert r2.status_code == 200, r2.text
        # A3-2: the second identical request was served from cache — NO second
        # child compute.
        assert _PORTFOLIO_LEG_CACHE.misses == 1
        assert _PORTFOLIO_LEG_CACHE.hits == 1

        # A3-1: byte-identical result (exact equality of the JSON equity list).
        assert r2.json()["portfolio_equity"] == r1.json()["portfolio_equity"]
        assert r2.json()["dates"] == r1.json()["dates"]


# ── A3-3: editing the child → new key → recompute ──────────────────────


class TestChildEditRecomputes:
    async def test_child_weight_change_busts_key(self, client: AsyncClient):
        await client.post(
            "/api/portfolio/compute", json=_composed({"up": 60.0, "down": 40.0})
        )
        assert _PORTFOLIO_LEG_CACHE.misses == 1

        # Different child spec (weights) → different body → different key → miss.
        await client.post(
            "/api/portfolio/compute", json=_composed({"up": 30.0, "down": 70.0})
        )
        assert _PORTFOLIO_LEG_CACHE.misses == 2

    async def test_child_leg_set_change_busts_key(self, client: AsyncClient):
        await client.post("/api/portfolio/compute", json=_composed({"up": 100.0}))
        assert _PORTFOLIO_LEG_CACHE.misses == 1
        # Adding a leg to the child changes the body → recompute.
        await client.post(
            "/api/portfolio/compute", json=_composed({"up": 50.0, "down": 50.0})
        )
        assert _PORTFOLIO_LEG_CACHE.misses == 2


# ── canonical_key determinism ──────────────────────────────────────────


class TestCanonicalKey:
    def test_key_is_order_independent(self):
        a = {"legs": {"x": 1, "y": 2}, "weights": {"a": 0.5, "b": 0.5}}
        b = {"weights": {"b": 0.5, "a": 0.5}, "legs": {"y": 2, "x": 1}}
        assert canonical_key(a) == canonical_key(b)

    def test_key_changes_with_content(self):
        a = {"weights": {"a": 0.5}}
        b = {"weights": {"a": 0.6}}
        assert canonical_key(a) != canonical_key(b)


# ── A3-4: bounded LRU eviction (direct unit tests) ─────────────────────


class TestBoundedLRU:
    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            BoundedLRUCache(capacity=0)

    def test_evicts_least_recently_used_when_over_capacity(self):
        c: BoundedLRUCache[tuple] = BoundedLRUCache(capacity=3)
        for k in ("a", "b", "c"):
            c.put(k, (k,))
        assert len(c) == 3
        # Touch "a" so it becomes MRU; LRU order is now b, c, a.
        assert c.get("a") == ("a",)
        # Insert a 4th distinct key → evict the LRU ("b").
        c.put("d", ("d",))
        assert len(c) == 3  # bound respected
        assert not c.peek("b")  # evicted
        assert c.peek("a") and c.peek("c") and c.peek("d")

    def test_len_never_exceeds_capacity_under_many_inserts(self):
        cap = 5
        c: BoundedLRUCache[tuple] = BoundedLRUCache(capacity=cap)
        for i in range(50):  # 50 distinct keys ≫ cap
            c.put(f"k{i}", (i,))
            assert len(c) <= cap
        assert len(c) == cap
        # Only the last `cap` keys survive; an old one recomputes (miss).
        assert not c.peek("k0")
        assert c.get("k0") is None  # miss
        assert c.peek(f"k{49}")

    async def test_get_or_compute_computes_once_per_key(self):
        c: BoundedLRUCache[tuple] = BoundedLRUCache(capacity=2)
        calls = {"n": 0}

        async def _mk():
            calls["n"] += 1
            return ("v",)

        assert await c.get_or_compute("x", _mk) == ("v",)  # miss → compute
        assert await c.get_or_compute("x", _mk) == ("v",)  # hit → no compute
        assert calls["n"] == 1
        assert c.hits == 1 and c.misses == 1

    async def test_evicted_key_recomputes(self):
        c: BoundedLRUCache[tuple] = BoundedLRUCache(capacity=1)
        calls = {"n": 0}

        async def _mk():
            calls["n"] += 1
            return (calls["n"],)

        await c.get_or_compute("a", _mk)  # compute a (n=1)
        await c.get_or_compute("b", _mk)  # compute b (n=2) → evicts a
        await c.get_or_compute("a", _mk)  # a evicted → recompute (n=3)
        assert calls["n"] == 3
