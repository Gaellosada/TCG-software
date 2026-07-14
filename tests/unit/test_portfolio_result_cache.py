"""Backend on-disk result cache — BC-1..BC-9 (backend_cache_design.md §criteria).

Endpoint tests drive the cached ``compute_portfolio`` through a mock market-data
service (one controlled ``PriceSeries`` per requested leg label, so a compute is
cheap + deterministic). The on-disk cache is isolated per test by the autouse
``_isolate_portfolio_result_cache`` fixture in the root conftest (Sign 10), so
every test starts with a fresh empty cache and the real user cache is never
touched.

The inner-compute spy monkeypatches ``portfolio._compute_portfolio_uncached``
(the pure computation the cache wraps) and counts calls: unified reuse (BC-1)
means a composed leg referencing an already-computed child does NOT increment it.
"""

from __future__ import annotations

import asyncio
import time

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

import tcg.core.api.portfolio as portfolio
from tcg.core.cache import DiskResultCache, canonical_hash
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
_META = {"from_cache", "computed_ms"}


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
    """Wrap the pure (uncached) compute, counting how often it actually runs."""
    real = portfolio._compute_portfolio_uncached
    calls = {"n": 0}

    async def _spy(body, svc, classify, repo):
        calls["n"] += 1
        return await real(body, svc, classify, repo)

    monkeypatch.setattr(portfolio, "_compute_portfolio_uncached", _spy)
    return calls


# ── request builders ───────────────────────────────────────────────────


def _instrument_leg(label: str) -> dict:
    return {"type": "instrument", "collection": "INDEX", "symbol": label}


def _pure_body(labels: list[str], start="2024-01-01", end="2024-12-31") -> dict:
    return {
        "legs": {lbl: _instrument_leg(lbl) for lbl in labels},
        "weights": {lbl: 100.0 / len(labels) for lbl in labels},
        "rebalance": "none",
        "return_type": "normal",
        "start": start,
        "end": end,
    }


def _composed_referencing(child: dict) -> dict:
    # The composed body threads its OWN start/end to the child (the child sub-body
    # inlined here omits start/end; the backend injects the parent's range) — so a
    # composed leg over range R keys identically to a standalone compute of the
    # child over R.
    child_inlined = {k: v for k, v in child.items() if k not in ("start", "end")}
    return {
        "legs": {
            "block": {
                "type": "portfolio",
                "portfolio_id": "child",
                "portfolio": child_inlined,
            }
        },
        "weights": {"block": 100.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": child["start"],
        "end": child["end"],
    }


async def _equity(client, body):
    r = await client.post("/api/portfolio/compute", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── BC-1: unified reuse (Bug 2 fix) ────────────────────────────────────


class TestUnifiedReuse:
    async def test_composed_child_served_from_standalone_cache(
        self, client: AsyncClient, compute_spy
    ):
        child = _pure_body(["up", "down"])

        # 1) Standalone compute of the child populates the cache.
        await _equity(client, child)
        assert compute_spy["n"] == 1

        # 2) A composed portfolio referencing that child over the SAME range: the
        #    parent body is a new compute (+1), but the CHILD is served from the
        #    cache the standalone populated — NOT recomputed. So exactly +1, not +2.
        await _equity(client, _composed_referencing(child))
        assert compute_spy["n"] == 2  # would be 3 if the child recomputed (Bug 2)

    async def test_standalone_and_composed_child_share_one_key(self):
        # Sign 9: the standalone request body and the composed child sub-body
        # serialize to the SAME canonical key.
        child = _pure_body(["up", "down"])
        standalone_body = portfolio.PortfolioRequest(**child)
        composed = _composed_referencing(child)
        leg = composed["legs"]["block"]["portfolio"]
        child_sub = portfolio.PortfolioRequest(
            legs=leg["legs"],
            weights=leg["weights"],
            rebalance=leg["rebalance"],
            return_type=leg["return_type"],
            start=composed["start"],
            end=composed["end"],
        )
        assert portfolio._portfolio_cache_key(
            standalone_body
        ) == portfolio._portfolio_cache_key(child_sub)

    def test_cost_params_are_in_cache_key(self):
        # HARD correctness: changing slippage_bps / fees_bps MUST change the key,
        # else a costed run would be served a stale zero-cost cached result.
        base = portfolio.PortfolioRequest(**_pure_body(["up", "down"]))
        slip = portfolio.PortfolioRequest(
            **_pure_body(["up", "down"]), slippage_bps=5.0
        )
        fees = portfolio.PortfolioRequest(**_pure_body(["up", "down"]), fees_bps=5.0)
        k_base = portfolio._portfolio_cache_key(base)
        assert k_base != portfolio._portfolio_cache_key(slip)
        assert k_base != portfolio._portfolio_cache_key(fees)
        assert portfolio._portfolio_cache_key(slip) != portfolio._portfolio_cache_key(
            fees
        )

    def test_omitted_cost_field_equals_explicit_zero(self):
        # The FE omits slippage_bps/fees_bps when 0; an absent field defaults to
        # 0.0, so an omitted-field body keys IDENTICALLY to an explicit-0 body
        # (the /cache/status probe stays consistent with the compute body).
        omitted = portfolio.PortfolioRequest(**_pure_body(["up", "down"]))
        explicit = portfolio.PortfolioRequest(
            **_pure_body(["up", "down"]), slippage_bps=0.0, fees_bps=0.0
        )
        assert omitted.slippage_bps == 0.0 and omitted.fees_bps == 0.0
        assert portfolio._portfolio_cache_key(
            omitted
        ) == portfolio._portfolio_cache_key(explicit)


# ── BC-3 + BC-5: transparency + from_cache flag ────────────────────────


class TestFromCacheFlagAndTransparency:
    async def test_flag_false_then_true_and_result_identical(
        self, client: AsyncClient, compute_spy
    ):
        body = _pure_body(["up", "down"])

        first = await _equity(client, body)
        assert first["from_cache"] is False
        assert isinstance(first["computed_ms"], int)
        assert compute_spy["n"] == 1

        second = await _equity(client, body)
        assert second["from_cache"] is True
        assert second["computed_ms"] is None
        assert compute_spy["n"] == 1  # served from cache, no recompute

        # BC-3: byte-identical apart from the two response-only meta fields.
        assert {k: v for k, v in first.items() if k not in _META} == {
            k: v for k, v in second.items() if k not in _META
        }


# ── BC-6: range is in the key ──────────────────────────────────────────


class TestRangeInKey:
    async def test_different_range_recomputes(self, client: AsyncClient, compute_spy):
        await _equity(client, _pure_body(["up", "down"], end="2024-12-31"))
        assert compute_spy["n"] == 1
        # Same spec, DIFFERENT range → different key → recompute (a portfolio's
        # contribution is not range-invariant, so reuse across ranges is wrong).
        await _equity(client, _pure_body(["up", "down"], end="2024-06-30"))
        assert compute_spy["n"] == 2


# ── BC-9: concurrency ──────────────────────────────────────────────────


class TestConcurrency:
    async def test_concurrent_identical_computes_are_consistent(
        self, client: AsyncClient
    ):
        body = _pure_body(["up", "down"])
        results = await asyncio.gather(
            *[client.post("/api/portfolio/compute", json=body) for _ in range(8)]
        )
        assert all(r.status_code == 200 for r in results)
        equities = [r.json()["portfolio_equity"] for r in results]
        # Every response — cache hit or a benign concurrent double-compute — is the
        # same equity curve. No corruption, no wrong answer.
        for eq in equities[1:]:
            assert eq == equities[0]


# ── BC-2 / BC-4: DiskResultCache unit tests ────────────────────────────


class TestDiskResultCacheUnit:
    async def test_durability_across_instances_same_file(self, tmp_path):
        # BC-2: put via one instance, get via a NEW instance on the same file
        # (simulated restart) → hit.
        path = tmp_path / "c.sqlite"
        a = DiskResultCache(path)
        await a.put("k", {"v": [1, 2, 3]})
        b = DiskResultCache(path)  # fresh instance, same file
        assert await b.get("k") == {"v": [1, 2, 3]}

    async def test_get_returns_fresh_independent_object(self, tmp_path):
        cache = DiskResultCache(tmp_path / "c.sqlite")
        await cache.put("k", {"arr": [1, 2, 3]})
        one = await cache.get("k")
        two = await cache.get("k")
        one["arr"].append(999)  # mutate one read
        assert two == {"arr": [1, 2, 3]}  # the other read is unaffected (fresh)

    async def test_lru_eviction_bound(self, tmp_path):
        # BC-4: cap respected; least-recently-accessed evicted.
        cache = DiskResultCache(tmp_path / "c.sqlite", max_entries=3)
        for k in ("a", "b", "c"):
            await cache.put(k, {"k": k})
        await cache.get("a")  # touch "a" → "b" is now LRU
        await cache.put("d", {"k": "d"})  # over cap → evict LRU ("b")
        assert cache.count() == 3
        assert await cache.get("b") is None  # evicted → miss
        assert await cache.get("a") == {"k": "a"}
        assert await cache.get("d") == {"k": "d"}

    async def test_ttl_expiry(self, tmp_path, monkeypatch):
        # BC-4 (optional TTL): an entry older than the TTL is a miss.
        cache = DiskResultCache(tmp_path / "c.sqlite", ttl_seconds=100.0)
        await cache.put("k", {"v": 1})
        assert await cache.get("k") == {"v": 1}  # fresh
        # Advance the clock past the TTL.
        real_time = time.time
        monkeypatch.setattr("tcg.core.cache.time.time", lambda: real_time() + 1000.0)
        assert await cache.get("k") is None  # expired → miss
        assert cache.count() == 0  # expired entry was dropped

    def test_rejects_non_positive_capacity(self, tmp_path):
        with pytest.raises(ValueError):
            DiskResultCache(tmp_path / "c.sqlite", max_entries=0)

    def test_canonical_hash_is_order_independent(self):
        assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
        assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})

    async def test_get_or_compute_computes_once(self, tmp_path):
        cache = DiskResultCache(tmp_path / "c.sqlite")
        calls = {"n": 0}

        async def _mk():
            calls["n"] += 1
            return {"v": 1}

        assert await cache.get_or_compute("k", _mk) == {"v": 1}
        assert await cache.get_or_compute("k", _mk) == {"v": 1}
        assert calls["n"] == 1

    def test_clear_empties_the_store(self, tmp_path):
        cache = DiskResultCache(tmp_path / "c.sqlite")
        cache._put_sync("k", {"v": 1})
        assert cache.count() == 1
        cache.clear()
        assert cache.count() == 0
