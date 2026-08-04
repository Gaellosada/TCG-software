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
from tcg.data._sql.instruments_v2 import V2_SCHEMA
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
async def test_front_closes_are_daily_and_one_per_date(svc):
    """The moneyness spot must come from daily bars, not minute bars.

    ``FUT_SP_500`` carries both ``bar:daily`` and ``bar:1m`` series. Without an
    ``s.freq = 'daily'`` filter this query also scans fact_bar's minute rows:
    over a wide window it times out, and per date it returns thousands of rows
    whose first entry may be a 00:00 minute bar — which
    ``_front_close_by_date`` (first row per date wins) would then report as the
    front close. On 2026-03-10 that is 6771.0 (00:00 minute bar) instead of
    6797.0 (daily close), and the two share the same ``ts`` so ``ORDER BY
    f.ts, c.expiration`` cannot even break the tie deterministically.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    fut_id = objs["FUT_SP_500"]["object_id"]
    rows = await svc._reader.fetch_future_front_closes(
        fut_id, start=date(2026, 3, 1), end=date(2026, 3, 31)
    )
    assert rows, "expected daily front-close rows for FUT_SP_500 in March 2026"
    # At daily grain there is exactly one row per (date, expiration), so a date
    # carries as many rows as it has live contracts — single digits for ES.
    # Minute rows give hundreds to thousands per date. Note it has to be the
    # *row* count: the set of distinct expirations per date is identical at
    # either grain, so counting expirations would not discriminate at all.
    per_date: dict[int, int] = {}
    for r in rows:
        per_date[r["ts_int"]] = per_date.get(r["ts_int"], 0) + 1
    worst = max(per_date.values())
    assert worst < 20, f"{worst} rows on one date — minute bars leaked in"
    pairs = [(r["ts_int"], r["expiration_int"]) for r in rows]
    assert len(set(pairs)) == len(pairs), "duplicate (date, expiration) rows"


@pytest.mark.integration
async def test_future_contract_bars_are_daily_with_unique_dates(svc):
    """The continuous-futures feed must read daily bars only.

    Same root cause as ``test_front_closes_are_daily_and_one_per_date``, one
    method over: ``FUT_SP_500`` carries both ``bar:daily`` (69 contracts,
    17 146 rows) and ``bar:1m`` (12 contracts, 980 194 rows) series, and this
    method is called with an unbounded ``ts`` range. Without an
    ``s.freq = 'daily'`` filter it scans the whole minute history — 60 s
    statement timeout — and, because ``PriceSeries.dates`` are ``YYYYMMDD``
    ints, every surviving minute row collapses onto a duplicate date inside its
    contract bucket, handing the ``ContinuousSeriesBuilder`` a series with
    repeated dates. Corrupt input, not just slow input.

    The pin is lossless: measured on the live warehouse, all 12 contracts with
    ``1m`` series also have a ``daily`` series, no contract has ``1m`` facts
    without ``daily`` facts, and all 634 distinct ``1m`` dates appear among the
    5 012 ``daily`` dates — so no contract and no date is dropped. That
    losslessness is not assumed here, it is re-derived against the dimension on
    every run (see below), because the pin's one real failure mode is a
    contract that exists *only* at another grain.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    fut_id = objs["FUT_SP_500"]["object_id"]

    # Losslessness, exactly and without a threshold: every contract the
    # type-only filter would see must also carry a `daily` bar serie. A
    # threshold ("more than N contracts survived") cannot do this job — the
    # realistic regression is the minute feed for a newly listed contract going
    # live before its daily backfill lands, which drops exactly one contract,
    # and that one is the front contract the roller stitches onto. This is a
    # dimension-only scan (no fact join), so it costs well under a second.
    async with svc._reader._pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT c.contract_code,
                           bool_or(s.freq = 'daily') AS has_daily
                    FROM {V2_SCHEMA}.serie s
                    JOIN {V2_SCHEMA}.contract c
                      ON c.contract_id = s.contract_id
                    WHERE s.object_id = %s
                      AND s.type = 'bar'
                      AND c.expiration IS NOT NULL
                    GROUP BY c.contract_id, c.contract_code""",
                (fut_id,),
            )
            grain = await cur.fetchall()
    assert grain, "expected bar-carrying contracts for FUT_SP_500"
    grain_only = sorted(r["contract_code"] for r in grain if not r["has_daily"])
    assert not grain_only, (
        f"{len(grain_only)} of {len(grain)} contracts have bar series but none "
        f"at freq='daily', so the pin drops them from the roller entirely: "
        f"{grain_only}"
    )

    contracts = await svc._reader.fetch_future_contract_bars(fut_id, "quarterly")
    assert contracts, "expected contract bars for FUT_SP_500"

    for cpd in contracts:
        dates = list(cpd.prices.dates)
        assert len(set(dates)) == len(dates), (
            f"{cpd.contract_id}: duplicate dates — minute rows collapsed "
            f"onto YYYYMMDD ints ({len(dates)} rows, "
            f"{len(set(dates))} distinct)"
        )


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
