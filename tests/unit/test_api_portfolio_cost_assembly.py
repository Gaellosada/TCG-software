"""Integration test for the PORTFOLIO cost ASSEMBLY (slippage/fees).

The unit tests in ``tests/engine/test_metrics_costs.py`` exercise the cost math
with a ``roll_turnover`` array injected by hand.  This test closes the gap the
reviewer flagged: it drives the REAL ``compute_portfolio`` route end-to-end
(through the httpx ASGI transport + a mocked ``MarketDataService``) so the
ASSEMBLY that turns a continuous-futures leg's ``roll_dates`` into round-trip
turnover — and lands it in the equity/totals — is actually run, not just read.

No dwh: the market-data service is fully mocked (``get_aligned_prices`` returns
controlled ``PriceSeries``; ``get_continuous`` returns a controlled
``ContinuousSeries`` whose ``roll_dates`` fall inside the window).  This mirrors
the fixture shape in ``test_api_portfolio.py`` / ``test_portfolio_result_cache.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.data.service import DefaultMarketDataService
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    PriceSeries,
    RollStrategy,
)

# 8 business days in Jan 2024.
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
# One leg drifts up, one drifts down -> nonzero daily-rebalance turnover so the
# cost bites even before any roll is added.
_SPX = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
_FUT = [50.0, 49.5, 49.0, 48.5, 48.0, 47.5, 47.0, 46.5]
# Interior roll boundary inside the window (excludes the initial open, exactly
# as ``ContinuousSeries.roll_dates`` does).
_ROLL_DATE_IN_WINDOW = 20240108


def _price_series(close_vals: list[float]) -> PriceSeries:
    n = len(close_vals)
    c = np.array(close_vals, dtype=np.float64)
    return PriceSeries(
        dates=np.array(_DATES, dtype=np.int64),
        open=c - 0.5,
        high=c + 0.5,
        low=c - 1.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


def _make_client(roll_dates: tuple[int, ...]) -> AsyncClient:
    """Build an ASGI client over a portfolio router backed by a mocked service.

    ``roll_dates`` is what ``svc.get_continuous`` reports for the continuous leg
    (drives the round-trip roll-turnover assembly in the cost path).
    """
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.types.errors import TCGError

    common_dates = np.array(_DATES, dtype=np.int64)
    aligned = {
        "SPX": _price_series(_SPX),
        "FUT": _price_series(_FUT),
    }

    cseries = ContinuousSeries(
        collection="FUT_SP_500",
        roll_config=ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
            cycle="HMUZ",
        ),
        prices=_price_series(_FUT),
        roll_dates=roll_dates,
        contracts=("C1", "C2"),
    )

    svc = MagicMock()
    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(return_value=(common_dates, aligned))
    svc.get_continuous = AsyncMock(return_value=cseries)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = svc
    app.state.app_db_repo = object()

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _body(slippage_bps: float, fees_bps: float) -> dict:
    body: dict = {
        "legs": {
            "SPX": {"type": "instrument", "collection": "INDEX", "symbol": "SP500"},
            "FUT": {
                "type": "continuous",
                "collection": "FUT_SP_500",
                "strategy": "front_month",
                "adjustment": "none",
                "cycle": "HMUZ",
            },
        },
        "weights": {"SPX": 50.0, "FUT": 50.0},
        "rebalance": "daily",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
        "use_cache": False,
    }
    if slippage_bps:
        body["slippage_bps"] = slippage_bps
    if fees_bps:
        body["fees_bps"] = fees_bps
    return body


async def _compute(client: AsyncClient, body: dict) -> dict:
    r = await client.post("/api/portfolio/compute", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_portfolio_roll_turnover_lands_in_equity_and_totals():
    """A continuous roll in-window + nonzero bps: the round-trip cost lands in
    the response totals AND drags final equity below the 0-bps baseline."""
    async with _make_client(roll_dates=(_ROLL_DATE_IN_WINDOW,)) as client:
        zero = await _compute(client, _body(0.0, 0.0))
        costed = await _compute(client, _body(10.0, 5.0))

    # 0 bps: both cost rows are exactly 0.
    assert zero["total_slippage_paid_pct"] == 0.0
    assert zero["total_fees_paid_pct"] == 0.0

    # Nonzero bps: both cost totals are strictly positive and on the 0.xx% scale
    # (percent units, NOT x100 inflated) — a round-trip on a half-notional leg at
    # 10/5 bps over a handful of bars is well under 1%.
    assert costed["total_slippage_paid_pct"] > 0.0
    assert costed["total_fees_paid_pct"] > 0.0
    assert 0.0 < costed["total_slippage_paid_pct"] < 5.0
    assert 0.0 < costed["total_fees_paid_pct"] < 5.0
    # Slippage rate (10 bps) is twice the fee rate (5 bps) over identical turnover.
    assert costed["total_slippage_paid_pct"] == pytest.approx(
        2.0 * costed["total_fees_paid_pct"], rel=1e-6
    )

    # The cost actually bites: final equity is lower than the 0-bps run.
    assert costed["portfolio_equity"][-1] < zero["portfolio_equity"][-1]


@pytest.mark.asyncio
async def test_continuous_roll_boundary_specifically_increases_cost():
    """Isolate the ROLL-turnover assembly: the SAME portfolio at the SAME bps
    costs strictly more when ``get_continuous`` reports an in-window roll
    boundary than when it reports none — proving the roll's round-trip is charged
    on top of the daily-drift turnover (not just entry/rebalance)."""
    async with _make_client(roll_dates=()) as client:
        no_roll = await _compute(client, _body(10.0, 0.0))
    async with _make_client(roll_dates=(_ROLL_DATE_IN_WINDOW,)) as client:
        with_roll = await _compute(client, _body(10.0, 0.0))

    assert with_roll["total_slippage_paid_pct"] > no_roll["total_slippage_paid_pct"]
    assert with_roll["portfolio_equity"][-1] < no_roll["portfolio_equity"][-1]
