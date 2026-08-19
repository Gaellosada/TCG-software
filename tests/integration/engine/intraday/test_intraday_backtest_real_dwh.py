"""Live-dwh integration test for the intraday ATM-straddle backtest.

Gated by ``--run-integration`` (tests/integration/conftest.py) AND by dwh
reachability (``load_dwh_config`` / ``pool.connect()`` -> skip on failure).

Proves the end-to-end data wiring on a SMALL real window (2 trading days in
2025): the real ``IntradayV2Reader`` (full-timestamptz 1m reads over
``tcg_instruments_v2``, read-only ``tcg_read``) feeding the pure engine via the
router's ``run_backtest``. No mocks in the exercised path.
"""

from __future__ import annotations

import pytest

from tcg.core.api.intraday_backtest import CostModelConfig, RunRequest, run_backtest
from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.intraday_v2 import IntradayV2Reader
from tcg.engine.intraday_backtest import resolve_et_to_utc
from datetime import date

pytestmark = pytest.mark.integration

# A small window in mid-2025, well inside the intraday window (2025-01..2026-07).
_START = "2025-06-16"  # Monday
_END = "2025-06-17"  # Tuesday


@pytest.fixture
async def reader():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001 - retry once (rotating egress IP)
        try:
            await pool.connect()
        except Exception as exc2:  # noqa: BLE001
            pytest.skip(f"dwh not reachable (retried once): {exc2}")
    try:
        yield IntradayV2Reader(pool)
    finally:
        await pool.close()


async def test_option_roots_present(reader):
    roots = await reader.list_option_roots()
    assert roots, "expected ES option roots (OPT_SP_500_*) in tcg_instruments_v2"
    assert all(r["symbol"].startswith("OPT_SP_500") for r in roots)


async def test_es_future_intraday_bars_present(reader):
    day = date(2025, 6, 16)
    start_ts = resolve_et_to_utc(day, "09:30")
    end_ts = resolve_et_to_utc(day, "16:00")
    bars = await reader.fetch_es_future_1m(start_ts, end_ts, on_or_after=day)
    assert bars, "expected front ES-future 1m bars for a 2025 trading day"
    # Full timestamptz preserved (not truncated to midnight).
    assert any(b.ts.hour != 0 for b in bars)
    assert all(b.price > 0 for b in bars)


async def test_es_future_bars_carry_quotes(reader):
    # v4: the ES future bbo-1m serie must surface bid/ask/sizes on the front
    # contract (the fields the hedge max_spread / min_quote_size conditions read).
    day = date(2025, 6, 16)
    start_ts = resolve_et_to_utc(day, "09:30")
    end_ts = resolve_et_to_utc(day, "16:00")
    bars = await reader.fetch_es_future_1m(start_ts, end_ts, on_or_after=day)
    quoted = [b for b in bars if b.bid is not None and b.ask is not None]
    assert quoted, (
        "expected some ES-future bars to carry two-sided bbo quotes "
        "(bid/ask) for the hedge conditions"
    )
    b = quoted[0]
    assert b.ask >= b.bid > 0
    assert b.bid_size is not None and b.ask_size is not None
    # ES-future tick constant exposed via the getter (documented 0.25 pts).
    assert await reader.get_es_future_tick_size() == 0.25


async def test_run_backtest_end_to_end(reader):
    # v2 request shape: entry/exit rule MODULES (time + snap tolerance +
    # conditions). A light max_spread condition exercises the conditional path
    # end-to-end on real bbba quotes without over-filtering thin strikes.
    req = RunRequest(
        start_date=_START,
        end_date=_END,
        entry={"time": "10:00", "snap_tolerance_minutes": 30.0,
               "conditions": [{"type": "max_spread", "pct": 50.0, "min_ticks": 4}]},
        exit={"time": "15:00", "snap_tolerance_minutes": 30.0},
        expiry_mode="NDTE",
        dte=0,
        straddle_side="long",
        # v4 hedge module: interval + band triggers, an ES-bar spread guard, and
        # a band-edge target — exercises the configurable hedge on real ES bbo.
        hedge={
            "enabled": True,
            "instrument": "es_future",
            "triggers": {"interval_minutes": 15, "delta_band": 0.1,
                         "sigma_move": {"enabled": False, "n": 1.0}},
            "conditions": [{"type": "max_spread", "pct": 50.0, "min_ticks": 8}],
            "target": {"mode": "band_edge", "ratio": 1.0},
        },
    )
    result = await run_backtest(reader, req)

    days = result["days"]
    assert len(days) == 2, "two weekdays in the range"
    assert result["aggregate"]["n_days"] == 2

    ok_days = [d for d in days if d["status"] == "ok"]
    assert ok_days, (
        "no day traded on real marks; days="
        + ", ".join(f"{d['date']}:{d['status']}/{d['skip_reason']}" for d in days)
    )
    d0 = ok_days[0]
    assert d0["strike"] and d0["strike"] > 0
    assert d0["entry"]["underlying"] > 0
    assert d0["entry"]["straddle_price"] > 0
    assert d0["exit"]["straddle_price"] > 0
    assert d0["pnl"]["total_pnl_usd"] == pytest.approx(
        d0["pnl"]["total_pnl_pts"] * 50.0
    )
    # v2 response additions: independent legs + both-on window bounds.
    assert d0["legs"]["call"]["entry_ts"] and d0["legs"]["put"]["entry_ts"]
    assert d0["straddle_on_ts"] and d0["straddle_off_ts"]
    # option P&L reconciles with the two per-leg pnl_pts.
    leg_sum = d0["legs"]["call"]["pnl_pts"] + d0["legs"]["put"]["pnl_pts"]
    assert d0["pnl"]["option_pnl_pts"] == pytest.approx(leg_sum)
    # P0.2: cost OFF (default) => zero cost, net == gross, and the new coverage
    # fields are present in the wire shape.
    assert d0["pnl"]["cost_pts"] == 0.0 and d0["pnl"]["n_fallback_fills"] == 0
    assert d0["pnl"]["total_pnl_pts"] == pytest.approx(
        d0["pnl"]["option_pnl_pts"] + d0["pnl"]["hedge_pnl_pts"]
    )
    assert result["aggregate"]["total_cost_usd"] == 0.0
    assert result["aggregate"]["n_fallback_fills"] == 0

    # P0.2: the SAME request with the half-spread cost model ON must charge a
    # non-negative cost on the REAL bbba spreads and reduce net P&L accordingly.
    # NOTE: Pydantic v2 `model_copy(update=...)` does NOT re-validate, so the
    # update value must already be a `CostModelConfig` (a raw dict would leave
    # `req.cost` a dict and break `_to_engine_cost`). Production builds RunRequest
    # via JSON-body validation, which coerces the nested dict correctly.
    req_cost = req.model_copy(
        update={"cost": CostModelConfig(enabled=True, fallback_cost_pts=0.0)}
    )
    result_cost = await run_backtest(reader, req_cost)
    dc = [d for d in result_cost["days"] if d["status"] == "ok"][0]
    assert dc["pnl"]["cost_pts"] >= 0.0
    # gross legs unchanged vs the cost-off run; net is gross minus the cost.
    assert dc["pnl"]["option_pnl_pts"] == pytest.approx(d0["pnl"]["option_pnl_pts"])
    assert dc["pnl"]["total_pnl_pts"] == pytest.approx(
        dc["pnl"]["option_pnl_pts"] + dc["pnl"]["hedge_pnl_pts"] - dc["pnl"]["cost_pts"]
    )
    assert result_cost["aggregate"]["total_cost_usd"] >= 0.0
