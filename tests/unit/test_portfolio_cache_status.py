"""Cache-STATUS endpoint (`POST /api/portfolio/cache/status`) + `DiskResultCache.peek`.

The status endpoint is a pure, side-effect-free existence check: for each query
body it computes the same canonical key the compute path uses (excluding
``use_cache``) and ``peek``s the on-disk cache — no compute, no dwh read, no LRU
bump. The on-disk cache is isolated per test by the autouse root-conftest fixture
(tmp dir).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

import tcg.core.api.portfolio as portfolio
from tcg.core.cache import DiskResultCache
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


def _pure_body(labels, **overrides):
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


async def _compute(client, body):
    r = await client.post("/api/portfolio/compute", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _status(client, queries):
    r = await client.post("/api/portfolio/cache/status", json={"queries": queries})
    assert r.status_code == 200, r.text
    return r.json()["results"]


# ── status reflects real cache state ───────────────────────────────────


class TestStatusReflectsCache:
    async def test_cached_true_after_compute_false_for_edited(
        self, client, compute_spy
    ):
        body = _pure_body(["up", "down"])
        edited = _pure_body(["up", "down"], weights={"up": 30.0, "down": 70.0})

        # Before any compute: both uncached.
        assert await _status(client, [body, edited]) == [
            {"cached": False},
            {"cached": False},
        ]

        await _compute(client, body)  # populate the cache for `body`
        assert compute_spy["n"] == 1

        results = await _status(client, [body, edited])
        assert results == [{"cached": True}, {"cached": False}]
        # Status did NOT compute anything.
        assert compute_spy["n"] == 1

    async def test_status_never_triggers_compute(self, client, compute_spy):
        body = _pure_body(["up", "down"])
        for _ in range(5):
            await _status(client, [body])
        assert compute_spy["n"] == 0  # pure lookups, never computed


# ── batch ordering + malformed handling ────────────────────────────────


class TestBatchAndMalformed:
    async def test_batch_returns_n_results_in_order(self, client):
        a = _pure_body(["up"])
        b = _pure_body(["down"])
        c = _pure_body(["up", "down"])
        await client.post("/api/portfolio/compute", json=b)  # cache only `b`
        results = await _status(client, [a, b, c])
        assert results == [{"cached": False}, {"cached": True}, {"cached": False}]

    async def test_empty_queries_returns_empty(self, client):
        assert await _status(client, []) == []

    async def test_malformed_body_is_false_no_500(self, client):
        # Missing required fields / nonsense → cached:false, never a 500.
        results = await _status(client, [{}, {"legs": "not-a-dict"}, {"weights": {}}])
        assert results == [{"cached": False}, {"cached": False}, {"cached": False}]


# ── use_cache excluded from the status key ─────────────────────────────


class TestUseCacheIrrelevantToStatus:
    async def test_use_cache_flag_does_not_change_status(self, client):
        # Populate with a default (use_cache=True) compute.
        await _compute(client, _pure_body(["up", "down"]))
        on = _pure_body(["up", "down"], use_cache=True)
        off = _pure_body(["up", "down"], use_cache=False)
        results = await _status(client, [on, off])
        assert results == [{"cached": True}, {"cached": True}]  # flag irrelevant


# ── DiskResultCache.peek unit ──────────────────────────────────────────


class TestPeekUnit:
    async def test_peek_hit_miss_without_mutation(self, tmp_path):
        cache = DiskResultCache(tmp_path / "c.sqlite")
        assert await cache.peek("k") is False
        await cache.put("k", {"v": 1})
        assert await cache.peek("k") is True
        # peek does not bump last_access / evict / delete: still exactly 1 entry
        # and a subsequent get still hits.
        assert cache.count() == 1
        assert await cache.get("k") == {"v": 1}

    async def test_peek_respects_ttl_without_deleting(self, tmp_path, monkeypatch):
        import time

        cache = DiskResultCache(tmp_path / "c.sqlite", ttl_seconds=100.0)
        await cache.put("k", {"v": 1})
        assert await cache.peek("k") is True
        real_time = time.time
        monkeypatch.setattr("tcg.core.cache.time.time", lambda: real_time() + 1000.0)
        assert await cache.peek("k") is False  # expired → absent
        # Non-mutating: the (expired) row is NOT deleted by peek.
        assert cache.count() == 1

    async def test_peek_does_not_bump_lru(self, tmp_path):
        # With cap 2: put a, b; peek a repeatedly; put c → a (still LRU, peek did
        # not refresh it) is evicted, not b.
        cache = DiskResultCache(tmp_path / "c.sqlite", max_entries=2)
        await cache.put("a", {"v": "a"})
        await cache.put("b", {"v": "b"})
        for _ in range(3):
            await cache.peek("a")  # must NOT refresh a's last_access
        await cache.put("c", {"v": "c"})
        assert await cache.peek("a") is False  # a evicted (peek didn't save it)
        assert await cache.peek("b") is True
        assert await cache.peek("c") is True
