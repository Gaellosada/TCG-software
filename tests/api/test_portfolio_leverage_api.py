"""F1 API path — non-normalizing (leveraged) portfolio combine end-to-end.

Drives the REAL ``POST /api/portfolio/compute`` → ``_compute_portfolio_uncached``
combine path (§7) with only the market-data FETCH mocked (no DB, no live
warehouse). The compute, weight handling, cost-overlay and metrics are all real.

Not marked ``integration``: that marker means "requires live app-data
PostgreSQL", which this test does NOT — mocking only ``get_aligned_prices``
keeps it in the DB-free suite the brief says F1 supports, while still exercising
the real compute path. (The alternative — a live-dwh test — would be skipped in
every DB-free run and could not paste real output here.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.data.service import DefaultMarketDataService
from tcg.types.market import PriceSeries


_DATES = [
    20240102,
    20240103,
    20240104,
    20240105,
    20240108,
    20240109,
    20240110,
    20240111,
]
# "up" rises monotonically, "down" falls monotonically → a +2×/−1× book that
# preserves leverage must swing markedly more than the normalized (÷Σ|w|) one.
_CLOSES = {
    "up": [100.0, 101.0, 102.5, 103.0, 105.0, 104.0, 106.0, 108.0],
    "down": [200.0, 199.0, 198.0, 197.5, 196.0, 197.0, 195.0, 193.0],
}


def _price_series(vals: list[float]) -> PriceSeries:
    n = len(_DATES)
    c = np.array(vals, dtype=np.float64)
    return PriceSeries(
        dates=np.array(_DATES, dtype=np.int64),
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


@pytest.fixture
def app():
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.types.errors import TCGError

    common = np.array(_DATES, dtype=np.int64)

    async def _aligned(legs_spec):
        return common, {label: _price_series(_CLOSES[label]) for label in legs_spec}

    svc = MagicMock()
    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)

    application = FastAPI()
    application.add_exception_handler(TCGError, tcg_error_handler)
    application.include_router(portfolio_router)
    application.state.market_data = svc
    application.state.app_db_repo = object()
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _body(normalize: bool, rebalance: str = "none") -> dict:
    # Percent weights: +200% long "up", −100% short "down" → gross 300%.
    return {
        "legs": {
            "up": {"type": "instrument", "collection": "INDEX", "symbol": "up"},
            "down": {"type": "instrument", "collection": "INDEX", "symbol": "down"},
        },
        "weights": {"up": 200.0, "down": -100.0},
        "rebalance": rebalance,
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
        "use_cache": False,
        "normalize_weights": normalize,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("rebalance", ["none", "daily"])
async def test_leverage_flag_amplifies_equity(client, rebalance: str) -> None:
    lev = (await client.post("/api/portfolio/compute", json=_body(False, rebalance))).json()
    nrm = (await client.post("/api/portfolio/compute", json=_body(True, rebalance))).json()

    lev_eq = np.array(lev["portfolio_equity"])
    nrm_eq = np.array(nrm["portfolio_equity"])
    assert np.all(np.isfinite(lev_eq))
    assert np.all(np.isfinite(nrm_eq))

    # First-bar return: up +1.0%, down −0.5%. Leveraged (fraction) weights are
    # +2.0 / −1.0 (200%/100% ÷100): 2*0.01 − 1*(−0.005) = 0.025. Normalized
    # divides by gross Σ|w|=3 → 0.025/3. The leveraged bar-1 return is exactly
    # gross× the normalized one.
    lev_r1 = lev_eq[1] / 100.0 - 1.0
    nrm_r1 = nrm_eq[1] / 100.0 - 1.0
    assert lev_r1 == pytest.approx(0.025, rel=1e-9)
    assert nrm_r1 == pytest.approx(0.025 / 3.0, rel=1e-9)
    assert lev_r1 == pytest.approx(3.0 * nrm_r1, rel=1e-9)

    # And the leveraged final equity deviates from par by more than normalized.
    assert abs(lev_eq[-1] - 100.0) > abs(nrm_eq[-1] - 100.0)


@pytest.mark.asyncio
async def test_default_request_is_normalized(client) -> None:
    """Omitting the flag == normalize_weights=True (byte-identical default)."""
    default = (
        await client.post(
            "/api/portfolio/compute",
            json={k: v for k, v in _body(True).items() if k != "normalize_weights"},
        )
    ).json()
    explicit = (await client.post("/api/portfolio/compute", json=_body(True))).json()
    np.testing.assert_array_equal(
        np.array(default["portfolio_equity"]), np.array(explicit["portfolio_equity"])
    )
