"""Issue #3 (futures, trap a): the chosen roll ``strategy`` must reach the
roll config on EVERY continuous-futures call path — not just the Data endpoint.

``build_roll_config`` previously hardcoded FRONT_MONTH and dropped ``strategy``;
it is called from three sites:
  * ``_series_fetch.py`` (the SIGNALS price fetcher + the input-overlap leaf),
  * ``indicators.py`` (the INDICATORS compute path),
  * the Data ``/continuous`` endpoint builds its own config (already threaded).

These tests drive the real call sites with an ``end_of_month`` instrument and
assert the ``ContinuousRollConfig`` handed to ``MarketDataService.get_continuous``
carries ``RollStrategy.END_OF_MONTH``.  Without the threading the strategy would
silently degrade to FRONT_MONTH and the feature would half-work (chart only).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.api._series_fetch import make_signal_fetcher
from tcg.core.app import create_app
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    PriceSeries,
    RollStrategy,
)
from tcg.types.signal import InstrumentContinuous


def _make_continuous_series(
    strategy: RollStrategy = RollStrategy.END_OF_MONTH,
) -> ContinuousSeries:
    prices = PriceSeries(
        dates=np.array([20240102, 20240103], dtype=np.int64),
        open=np.array([100.0, 101.0]),
        high=np.array([100.5, 101.5]),
        low=np.array([99.5, 100.5]),
        close=np.array([100.2, 101.2]),
        volume=np.array([1000.0, 1100.0]),
    )
    return ContinuousSeries(
        collection="FUT_ES",
        roll_config=ContinuousRollConfig(strategy=strategy),
        prices=prices,
        roll_dates=(20240103,),
        contracts=("ESH24", "ESM24"),
    )


# ── Signals price-fetcher path (_series_fetch.make_signal_fetcher) ─────────


async def test_signal_fetcher_threads_end_of_month_strategy():
    """The signals fetcher must build an END_OF_MONTH roll config when the
    input's instrument carries strategy='end_of_month'."""
    svc = AsyncMock()
    svc.get_continuous = AsyncMock(return_value=_make_continuous_series())

    fetch = make_signal_fetcher(svc, date(2024, 1, 1), date(2024, 3, 31))
    inst = InstrumentContinuous(
        collection="FUT_ES",
        adjustment="ratio",
        cycle="HMUZ",
        roll_offset=0,
        strategy="end_of_month",
    )
    await fetch(inst, "close")

    svc.get_continuous.assert_awaited_once()
    config = svc.get_continuous.call_args[0][1]
    assert isinstance(config, ContinuousRollConfig)
    assert config.strategy == RollStrategy.END_OF_MONTH
    # Other params still honoured alongside the strategy.
    assert config.adjustment == AdjustmentMethod.RATIO
    assert config.cycle == "HMUZ"


async def test_signal_fetcher_front_month_unchanged():
    """Regression: the default front_month strategy still produces a
    FRONT_MONTH config (no accidental flip to END_OF_MONTH)."""
    svc = AsyncMock()
    svc.get_continuous = AsyncMock(
        return_value=_make_continuous_series(strategy=RollStrategy.FRONT_MONTH)
    )

    fetch = make_signal_fetcher(svc, date(2024, 1, 1), date(2024, 3, 31))
    inst = InstrumentContinuous(collection="FUT_ES")  # strategy defaults front_month
    await fetch(inst, "close")

    config = svc.get_continuous.call_args[0][1]
    assert config.strategy == RollStrategy.FRONT_MONTH


# ── Basket-leaf date-axis path (_date_array_for_leaf_instrument) ───────────
# This is the build_roll_config caller that runs for a BASKET containing a
# continuous-futures leg (input-overlap windowing / basket date intersection).
# r1 review NIT: it was "wired but untested" — covering it here.


async def test_basket_leaf_date_axis_threads_end_of_month_strategy():
    """A continuous leaf inside a basket must build an END_OF_MONTH roll config
    when its instrument carries strategy='end_of_month' (the _series_fetch
    basket-leaf / input-overlap path, distinct from the top-level fetcher)."""
    from tcg.core.api._series_fetch import _date_array_for_leaf_instrument

    svc = AsyncMock()
    svc.get_continuous = AsyncMock(return_value=_make_continuous_series())

    inst = InstrumentContinuous(
        collection="FUT_ES",
        adjustment="none",
        cycle="HMUZ",
        roll_offset=0,
        strategy="end_of_month",
    )
    await _date_array_for_leaf_instrument(
        inst, svc, start=date(2024, 1, 1), end=date(2024, 3, 31), err_prefix="leg 0"
    )

    svc.get_continuous.assert_awaited_once()
    config = svc.get_continuous.call_args[0][1]
    assert isinstance(config, ContinuousRollConfig)
    assert config.strategy == RollStrategy.END_OF_MONTH
    assert config.cycle == "HMUZ"


async def test_basket_leaf_date_axis_front_month_unchanged():
    """Regression: the basket-leaf path still produces FRONT_MONTH by default."""
    from tcg.core.api._series_fetch import _date_array_for_leaf_instrument

    svc = AsyncMock()
    svc.get_continuous = AsyncMock(
        return_value=_make_continuous_series(strategy=RollStrategy.FRONT_MONTH)
    )
    inst = InstrumentContinuous(collection="FUT_ES")  # default front_month
    await _date_array_for_leaf_instrument(
        inst, svc, start=date(2024, 1, 1), end=date(2024, 3, 31), err_prefix="leg 0"
    )
    config = svc.get_continuous.call_args[0][1]
    assert config.strategy == RollStrategy.FRONT_MONTH


# ── Indicators compute path (indicators.py build_roll_config call) ─────────


@pytest.fixture
async def app_client():
    app = create_app()
    mock_svc = AsyncMock()
    mock_svc.get_continuous = AsyncMock(return_value=_make_continuous_series())
    app.state.market_data = mock_svc
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, mock_svc


async def test_indicators_path_threads_end_of_month(app_client):
    """A continuous series ref with strategy='end_of_month' routed through the
    indicators /compute endpoint must reach get_continuous as END_OF_MONTH."""
    ac, mock_svc = app_client
    body = {
        # Minimal indicator code; the body just needs to be schema-valid enough
        # to reach the series-materialisation step where build_roll_config runs.
        "code": "result = series['price']",
        "params": {},
        "series": {
            "price": {
                "type": "continuous",
                "collection": "FUT_ES",
                "adjustment": "none",
                "cycle": "HMUZ",
                "rollOffset": 0,
                "strategy": "end_of_month",
            }
        },
        "start": "2024-01-01",
        "end": "2024-03-31",
    }
    resp = await ac.post("/api/indicators/compute", json=body)
    # The compute may succeed or surface a downstream data issue, but the
    # roll-config construction (the thing under test) happens first.
    assert resp.status_code in (200, 400, 422), resp.text
    mock_svc.get_continuous.assert_awaited()
    config = mock_svc.get_continuous.call_args[0][1]
    assert config.strategy == RollStrategy.END_OF_MONTH
    assert config.cycle == "HMUZ"


# ── Data endpoint path (the easy one — confirm it accepts end_of_month) ────


async def test_data_endpoint_accepts_end_of_month(app_client):
    """The Data /continuous endpoint accepts end_of_month with no endpoint
    change (strategy is validated by the RollStrategy enum constructor)."""
    ac, mock_svc = app_client
    resp = await ac.get(
        "/api/data/continuous/FUT_ES", params={"strategy": "end_of_month"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["strategy"] == "end_of_month"
    config = mock_svc.get_continuous.call_args[0][1]
    assert config.strategy == RollStrategy.END_OF_MONTH
