"""Live-dwh integration tests for the Database v2 backend.

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by the
``DWH_*`` connection variables being present (``load_dwh_config`` raises
otherwise -> skip). Reads the 5 live v2 objects (RATE_US_CMT_1M, RATE_US_SOFR_ON,
IND_SP_500, FUT_SP_500, OPT_SP_500_EW3) READ-ONLY via ``tcg_read``.

Verifies:
  * every live object lists with the right kind;
  * fact-table dispatch reads the index bar series and a rate value series;
  * futures continuous on FUT_SP_500 stitches a multi-contract series;
  * options continuous on OPT_SP_500_EW3 selects by strike and by moneyness,
    and rejects delta with a clean ValidationError.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data.service_v2 import DefaultMarketDataServiceV2
from tcg.types.errors import ValidationError
from tcg.types.market import AdjustmentMethod, ContinuousRollConfig, RollStrategy


@pytest.fixture
async def svc():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield DefaultMarketDataServiceV2(pool)
    await pool.close()


@pytest.mark.integration
async def test_lists_five_live_objects(svc):
    objs = await svc.list_objects()
    by_symbol = {o["symbol"]: o for o in objs}
    for sym in (
        "RATE_US_CMT_1M",
        "RATE_US_SOFR_ON",
        "IND_SP_500",
        "FUT_SP_500",
        "OPT_SP_500_EW3",
    ):
        assert sym in by_symbol, f"missing live object {sym}"
    assert by_symbol["IND_SP_500"]["kind"] == "index"
    assert by_symbol["FUT_SP_500"]["kind"] == "future"
    assert by_symbol["OPT_SP_500_EW3"]["kind"] == "option"
    assert by_symbol["RATE_US_CMT_1M"]["kind"] == "rate"
    # Derivative -> underlying wiring.
    assert (
        by_symbol["FUT_SP_500"]["underlying_object_id"]
        == by_symbol["IND_SP_500"]["object_id"]
    )


@pytest.mark.integration
async def test_object_detail_and_series_dispatch(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    ind_id = objs["IND_SP_500"]["object_id"]
    detail = await svc.get_object_detail(ind_id)
    assert detail["object"]["symbol"] == "IND_SP_500"
    # index has one non-contract bar series.
    bar_series = [s for s in detail["series"] if s["type"] == "bar"]
    assert bar_series
    serie_id = bar_series[0]["serie_id"]
    result = await svc.get_series(
        serie_id, start=date(2020, 1, 1), end=date(2020, 12, 31)
    )
    assert result["type"] == "bar"
    assert "close" in result["fields"]
    assert len(result["points"]["ts"]) > 100  # a year of index bars
    assert len(result["points"]["close"]) == len(result["points"]["ts"])


@pytest.mark.integration
async def test_rate_value_series(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    rate_id = objs["RATE_US_CMT_1M"]["object_id"]
    detail = await svc.get_object_detail(rate_id)
    serie_id = detail["series"][0]["serie_id"]
    result = await svc.get_series(serie_id)
    assert result["type"] == "value"
    assert result["fields"] == ["value"]
    assert len(result["points"]["value"]) > 0


@pytest.mark.integration
async def test_futures_continuous_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    fut_id = objs["FUT_SP_500"]["object_id"]
    cfg = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.RATIO
    )
    series = await svc.get_continuous_future(fut_id, cfg)
    assert series is not None
    assert len(series.prices) > 1000  # ES 2010.. -> thousands of bars
    assert len(series.contracts) > 10  # many quarterly contracts stitched
    assert len(series.roll_dates) > 10
    cycles = await svc.get_future_cycles(fut_id)
    assert cycles == ["quarterly"]


@pytest.mark.integration
async def test_options_continuous_strike_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW3"]["object_id"]
    res = await svc.get_continuous_options(
        opt_id,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert res.dates  # at least the 2024-06-18 settlement date
    assert all(v > 0 for v in res.values)  # false-zero guard held
    # The selected contract must be a 5000-strike put around that expiry.
    assert any("5000" in c for c in res.contracts)


@pytest.mark.integration
async def test_options_continuous_moneyness_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW3"]["object_id"]
    res = await svc.get_continuous_options(
        opt_id,
        criterion="moneyness",
        target=1.0,
        option_type="put",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert res.dates
    assert all(v > 0 for v in res.values)


@pytest.mark.integration
async def test_options_continuous_delta_rejected_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW3"]["object_id"]
    with pytest.raises(ValidationError, match="greeks"):
        await svc.get_continuous_options(
            opt_id, criterion="delta", target=0.1, option_type="put"
        )
