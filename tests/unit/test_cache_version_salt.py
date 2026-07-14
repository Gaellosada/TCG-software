"""BE-B1: compute-version cache salt + recursive use_cache exclusion.

The durable on-disk cache (30-day TTL) has no restart-wipe, so a code/config
change (engine / option pricer constants / ``tcg/types/multipliers.py``) could
otherwise serve a stale WRONG equity with ``from_cache: true`` until TTL. The key
is salted with ``COMPUTE_VERSION`` so any version bump namespace-invalidates
durable entries. Both the compute path and ``/cache/status`` use the SAME salted
key, so they stay consistent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

import tcg.core.api.portfolio as portfolio
from tcg.data.service import DefaultMarketDataService
from tcg.types.market import PriceSeries


DATES = [20240102, 20240103, 20240104, 20240105, 20240108]
CLOSES = {
    "up": [100.0, 101.0, 102.0, 103.0, 104.0],
    "down": [200.0, 199.0, 198.0, 197.0, 196.0],
}


def _ps(vals):
    c = np.array(vals, dtype=np.float64)
    return PriceSeries(
        dates=np.array(DATES, dtype=np.int64),
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(len(DATES), 1000.0),
    )


@pytest.fixture
def mock_app():
    from fastapi import FastAPI
    from tcg.core.api.portfolio import router
    from tcg.core.api.errors import tcg_error_handler
    from tcg.types.errors import TCGError

    cd = np.array(DATES, dtype=np.int64)

    async def _aligned(legs_spec):
        return cd, {
            lbl: _ps(CLOSES.get(lbl, [100.0 + i for i in range(len(DATES))]))
            for lbl in legs_spec
        }

    svc = MagicMock()
    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)
    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(router)
    app.state.market_data = svc
    app.state.app_db_repo = object()
    return app


@pytest.fixture
async def client(mock_app):
    async with AsyncClient(
        transport=ASGITransport(app=mock_app), base_url="http://t"
    ) as ac:
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


def _body(**overrides):
    b = {
        "legs": {
            lbl: {"type": "instrument", "collection": "INDEX", "symbol": lbl}
            for lbl in ("up", "down")
        },
        "weights": {"up": 50.0, "down": 50.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    b.update(overrides)
    return b


async def _compute(client, body):
    r = await client.post("/api/portfolio/compute", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _status(client, queries):
    r = await client.post("/api/portfolio/cache/status", json={"queries": queries})
    assert r.status_code == 200, r.text
    return r.json()["results"]


# ── compute-version salt ───────────────────────────────────────────────


class TestComputeVersionSalt:
    def test_version_change_changes_key(self, monkeypatch):
        b = portfolio.PortfolioRequest(**_body())
        k1 = portfolio._portfolio_cache_key(b)
        monkeypatch.setattr(portfolio, "COMPUTE_VERSION", "different-version")
        assert portfolio._portfolio_cache_key(b) != k1

    async def test_same_version_hits_new_version_misses(self, client, compute_spy):
        body = _body()
        first = await _compute(client, body)
        assert first["from_cache"] is False
        assert compute_spy["n"] == 1

        # Same version → hit.
        assert (await _compute(client, body))["from_cache"] is True
        assert compute_spy["n"] == 1

        # A new release bumps COMPUTE_VERSION → the old durable entry no longer
        # matches → recompute (fresh), not a stale from_cache:true.
        import tcg.core.api.portfolio as p

        original = p.COMPUTE_VERSION
        try:
            # A distinct sentinel version (must differ from the current
            # COMPUTE_VERSION so the durable key namespaces away).
            p.COMPUTE_VERSION = "0.0.0-version-salt-test"
            after = await _compute(client, body)
        finally:
            p.COMPUTE_VERSION = original
        assert after["from_cache"] is False
        assert compute_spy["n"] == 2

    async def test_status_uses_salted_key(self, client):
        body = _body()
        # Cache under an OLD version, then query status under the CURRENT version.
        import tcg.core.api.portfolio as p

        original = p.COMPUTE_VERSION
        try:
            p.COMPUTE_VERSION = "old-version"
            await _compute(client, body)  # entry written under old salt
        finally:
            p.COMPUTE_VERSION = original
        # Under the current version, that old entry is a different key → not found.
        assert await _status(client, [body]) == [{"cached": False}]

        # Cache under the current version → status sees it.
        await _compute(client, body)
        assert await _status(client, [body]) == [{"cached": True}]


# ── recursive use_cache exclusion ──────────────────────────────────────


class TestRecursiveUseCacheExclusion:
    def test_top_level_flag_excluded(self):
        on = portfolio.PortfolioRequest(**_body(use_cache=True))
        off = portfolio.PortfolioRequest(**_body(use_cache=False))
        assert portfolio._portfolio_cache_key(on) == portfolio._portfolio_cache_key(off)

    def test_nested_child_flag_excluded(self):
        def composed(child_uc):
            child = {
                "legs": {
                    "x": {"type": "instrument", "collection": "INDEX", "symbol": "up"}
                },
                "weights": {"x": 100.0},
                "rebalance": "none",
                "return_type": "normal",
                "use_cache": child_uc,
            }
            return portfolio.PortfolioRequest(
                legs={
                    "blk": {
                        "type": "portfolio",
                        "portfolio_id": "c",
                        "portfolio": child,
                    }
                },
                weights={"blk": 100.0},
            )

        # A nested legs.<x>.portfolio.use_cache flag must NOT affect the key.
        assert portfolio._portfolio_cache_key(
            composed(True)
        ) == portfolio._portfolio_cache_key(composed(False))

    def test_strip_use_cache_removes_all_levels(self):
        raw = {
            "use_cache": True,
            "legs": {"blk": {"portfolio": {"use_cache": False, "weights": {"x": 1}}}},
            "weights": {"blk": 100},
        }
        stripped = portfolio._strip_use_cache(raw)
        assert "use_cache" not in stripped
        assert "use_cache" not in stripped["legs"]["blk"]["portfolio"]
        assert stripped["weights"] == {"blk": 100}  # non-use_cache content intact
