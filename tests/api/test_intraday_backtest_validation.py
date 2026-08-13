"""Unit tests for the intraday-backtest request validation / day resolution (v2).

Pure — exercises ``resolve_day_plans`` / ``count_trading_days`` / ``_pick_expiry``
directly, no DB. Covers: out-of-window rejection, T2<=T1 rejection, inverted
range, weekday expansion, the unified ``custom_days`` control (exclude flag +
FULL per-day entry/exit override modules), condition parsing + validation of bad
condition params/types, bad time-format rejection, and expiry resolution.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tcg.core.api.intraday_backtest import (
    CustomDay,
    EntryExitModule,
    EntryExitOverride,
    HedgeConfig,
    RunRequest,
    _pick_expiry,
    _to_engine_hedge,
    count_trading_days,
    resolve_day_plans,
)
from tcg.types.intraday import (
    HedgeSpec,
    MaxSpreadCond,
    MaxUnderlyingMoveCond,
    MinPremiumCond,
    MinQuoteSizeCond,
    MinRehedgeDeltaCond,
    NetDeltaTrigger,
    PnlTrigger,
    SigmaMoveTrigger,
    UnderlyingMoveTrigger,
)


def _req(**over) -> RunRequest:
    base = dict(start_date="2025-02-03", end_date="2025-02-07")
    base.update(over)
    return RunRequest(**base)


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_default_entry_exit_modules():
    r = _req()
    assert r.entry.time == "10:00" and r.entry.snap_tolerance_minutes == 10.0
    assert r.exit.time == "15:45" and r.exit.snap_tolerance_minutes == 10.0
    assert r.entry.conditions == [] and r.exit.conditions == []
    # v4 hedge module defaults: enabled es_future, interval+band triggers on,
    # sigma off, no conditions, target zero.
    assert r.hedge.enabled is True
    assert r.hedge.instrument == "es_future"
    assert r.hedge.triggers.interval_minutes == 15.0
    assert r.hedge.triggers.delta_band == 0.10
    assert r.hedge.triggers.sigma_move.enabled is False
    assert r.hedge.conditions == []
    assert r.hedge.target.mode == "zero"
    assert r.custom_days == []


# --------------------------------------------------------------------------- #
# Window / range / times
# --------------------------------------------------------------------------- #
def test_out_of_window_rejected():
    with pytest.raises(HTTPException) as ei:
        resolve_day_plans(_req(start_date="2024-12-01", end_date="2025-01-05"))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException):
        resolve_day_plans(_req(start_date="2026-07-01", end_date="2026-08-15"))


def test_inverted_range_rejected():
    with pytest.raises(HTTPException) as ei:
        resolve_day_plans(_req(start_date="2025-03-10", end_date="2025-03-01"))
    assert ei.value.status_code == 400


def test_exit_not_after_entry_rejected():
    with pytest.raises(HTTPException) as ei:
        resolve_day_plans(_req(entry={"time": "15:00"}, exit={"time": "10:00"}))
    assert ei.value.status_code == 400


def test_weekend_only_range_has_no_days():
    with pytest.raises(HTTPException):
        resolve_day_plans(_req(start_date="2025-02-08", end_date="2025-02-09"))


# --------------------------------------------------------------------------- #
# Weekday expansion + exclusions
# --------------------------------------------------------------------------- #
def test_weekday_expansion_and_exclusions():
    plans = resolve_day_plans(_req(custom_days=[{"date": "2025-02-05", "exclude": True}]))
    assert [p.date_int for p in plans] == [20250203, 20250204, 20250205, 20250206, 20250207]
    excluded = [p for p in plans if p.excluded]
    assert len(excluded) == 1 and excluded[0].date_int == 20250205


def test_carries_default_tolerances_and_conditions():
    plans = resolve_day_plans(
        _req(entry={"time": "10:00", "snap_tolerance_minutes": 7.0,
                    "conditions": [{"type": "min_premium", "points": 0.5}]})
    )
    p = plans[0]
    assert p.entry_tol == 7.0
    assert p.entry_conditions == [MinPremiumCond(points=0.5)]
    assert p.exit_tol == 10.0 and p.exit_conditions == []


# --------------------------------------------------------------------------- #
# custom_days FULL per-day override (entry/exit partial + exclude)
# --------------------------------------------------------------------------- #
def test_custom_day_full_override_time_tol_and_conditions():
    plans = resolve_day_plans(_req(custom_days=[{
        "date": "2025-02-04",
        "entry": {"time": "11:00", "snap_tolerance_minutes": 3.0,
                  "conditions": [{"type": "max_spread", "pct": 5.0, "min_ticks": 2}]},
        "exit": {"time": "14:00"},
    }]))
    d4 = next(p for p in plans if p.date_int == 20250204)
    assert d4.entry_ts.hour == 16 and d4.exit_ts.hour == 19  # 11:00/14:00 ET winter
    assert d4.entry_tol == 3.0
    assert d4.entry_conditions == [MaxSpreadCond(pct=5.0, min_ticks=2.0)]
    # Untouched day keeps global defaults.
    d3 = next(p for p in plans if p.date_int == 20250203)
    assert d3.entry_ts.hour == 15 and not d3.excluded


def test_custom_day_partial_override_inherits_absent_fields():
    # Override only entry.time; exit + entry tolerance/conditions inherit global.
    plans = resolve_day_plans(_req(
        entry={"time": "10:00", "snap_tolerance_minutes": 8.0},
        custom_days=[{"date": "2025-02-04", "entry": {"time": "11:00"}}],
    ))
    d4 = next(p for p in plans if p.date_int == 20250204)
    assert d4.entry_ts.hour == 16       # overridden
    assert d4.entry_tol == 8.0          # inherited
    assert d4.exit_ts.hour == 20 and d4.exit_ts.minute == 45  # inherited 15:45 ET


def test_exclude_wins_over_override():
    plans = resolve_day_plans(_req(custom_days=[{
        "date": "2025-02-05", "exclude": True,
        "entry": {"time": "15:00"}, "exit": {"time": "10:00"},  # contradictory, ignored
    }]))
    d5 = next(p for p in plans if p.date_int == 20250205)
    assert d5.excluded


def test_custom_days_mixed_exclude_and_override():
    plans = resolve_day_plans(_req(custom_days=[
        {"date": "2025-02-05", "exclude": True},
        {"date": "2025-02-04", "entry": {"time": "11:00"}, "exit": {"time": "14:00"}},
    ]))
    assert next(p for p in plans if p.date_int == 20250205).excluded
    d4 = next(p for p in plans if p.date_int == 20250204)
    assert not d4.excluded and d4.entry_ts.hour == 16 and d4.exit_ts.hour == 19


# --------------------------------------------------------------------------- #
# Condition + time validation (422 on bad input)
# --------------------------------------------------------------------------- #
def test_bad_time_format_rejected():
    with pytest.raises(ValidationError):
        EntryExitModule(time="25:99")
    with pytest.raises(ValidationError):
        EntryExitOverride(time="1100")


def test_unknown_condition_type_rejected():
    with pytest.raises(ValidationError):
        EntryExitModule(conditions=[{"type": "totally_unknown", "x": 1}])


def test_unknown_underlying_ref_rejected():
    with pytest.raises(ValidationError):
        EntryExitModule(conditions=[{"type": "max_underlying_move", "pct": 1.0, "ref": "prev_close"}])


def test_bad_condition_params_rejected():
    # Negative / non-positive params rejected by Field constraints.
    with pytest.raises(ValidationError):
        EntryExitModule(conditions=[{"type": "max_spread", "pct": -1.0}])
    with pytest.raises(ValidationError):
        EntryExitModule(conditions=[{"type": "min_quote_size", "size": 0}])
    with pytest.raises(ValidationError):
        EntryExitModule(conditions=[{"type": "min_premium", "points": -0.5}])
    with pytest.raises(ValidationError):
        EntryExitModule(conditions=[{"type": "max_spread", "pct": 5.0, "min_ticks": -1}])


def test_all_condition_types_accepted():
    m = EntryExitModule(conditions=[
        {"type": "max_spread", "pct": 5.0, "min_ticks": 1},
        {"type": "min_quote_size", "size": 10},
        {"type": "min_premium", "points": 0.5},
        {"type": "max_underlying_move", "pct": 1.0, "ref": "day_open"},
    ])
    assert len(m.conditions) == 4


# --------------------------------------------------------------------------- #
# count_trading_days
# --------------------------------------------------------------------------- #
def test_count_trading_days_excludes_excluded():
    assert count_trading_days(
        _req(custom_days=[{"date": "2025-02-05", "exclude": True}])
    ) == 4
    assert count_trading_days(_req()) == 5


# --------------------------------------------------------------------------- #
# Expiry resolution
# --------------------------------------------------------------------------- #
def test_pick_expiry_0dte():
    exps = [date(2025, 2, 3), date(2025, 2, 5), date(2025, 2, 7)]
    assert _pick_expiry(exps, date(2025, 2, 3), "0DTE", 0) == date(2025, 2, 3)
    assert _pick_expiry(exps, date(2025, 2, 4), "0DTE", 0) is None


def test_pick_expiry_ndte():
    exps = [date(2025, 2, 3), date(2025, 2, 10), date(2025, 2, 20)]
    assert _pick_expiry(exps, date(2025, 2, 3), "NDTE", 5) == date(2025, 2, 10)
    assert _pick_expiry(exps, date(2025, 2, 4), "NDTE", 0) == date(2025, 2, 10)


# --------------------------------------------------------------------------- #
# v3 — Early-exit TRIGGERS (exit module only)
# --------------------------------------------------------------------------- #
def test_exit_triggers_all_types_accepted():
    m = EntryExitModule(triggers=[
        {"type": "underlying_move", "amount": 15, "unit": "points"},
        {"type": "sigma_move", "n": 1.0},
        {"type": "net_delta", "threshold": 0.3},
        {"type": "pnl", "amount": 500, "unit": "usd", "direction": "both"},
    ])
    assert len(m.triggers) == 4


def test_bad_trigger_params_rejected():
    for bad in (
        {"type": "underlying_move", "amount": 0},
        {"type": "underlying_move", "amount": -1},
        {"type": "sigma_move", "n": 0},
        {"type": "sigma_move", "n": -2.0},
        {"type": "net_delta", "threshold": 0},
        {"type": "net_delta", "threshold": -0.1},
        {"type": "pnl", "amount": 0, "unit": "usd", "direction": "both"},
        {"type": "pnl", "amount": -5, "unit": "usd", "direction": "both"},
    ):
        with pytest.raises(ValidationError):
            EntryExitModule(triggers=[bad])


def test_bad_trigger_enums_rejected():
    with pytest.raises(ValidationError):
        EntryExitModule(triggers=[{"type": "underlying_move", "amount": 15, "unit": "ticks"}])
    with pytest.raises(ValidationError):
        EntryExitModule(triggers=[{"type": "pnl", "amount": 500, "unit": "usd", "direction": "sideways"}])
    with pytest.raises(ValidationError):
        EntryExitModule(triggers=[{"type": "pnl", "amount": 500, "unit": "furlongs", "direction": "both"}])
    with pytest.raises(ValidationError):
        EntryExitModule(triggers=[{"type": "totally_unknown_trigger", "x": 1}])


def test_entry_triggers_rejected():
    # Triggers are EXIT-only; entry (global or per-day) must reject them.
    with pytest.raises(ValidationError):
        _req(entry={"time": "10:00", "triggers": [{"type": "net_delta", "threshold": 0.3}]})
    with pytest.raises(ValidationError):
        _req(custom_days=[{
            "date": "2025-02-04",
            "entry": {"triggers": [{"type": "net_delta", "threshold": 0.3}]},
        }])


def test_exit_triggers_carried_into_plan():
    plans = resolve_day_plans(_req(exit={
        "time": "15:45",
        "triggers": [{"type": "underlying_move", "amount": 15, "unit": "points"}],
    }))
    assert plans[0].exit_triggers == [UnderlyingMoveTrigger(amount=15.0, unit="points")]


def test_exit_trigger_per_day_override_and_inherit():
    plans = resolve_day_plans(_req(custom_days=[{
        "date": "2025-02-04",
        "exit": {"triggers": [{"type": "net_delta", "threshold": 0.3}]},
    }]))
    d4 = next(p for p in plans if p.date_int == 20250204)
    assert d4.exit_triggers == [NetDeltaTrigger(threshold=0.3)]
    # Untouched day inherits the global (empty) trigger list.
    d3 = next(p for p in plans if p.date_int == 20250203)
    assert d3.exit_triggers == []


def test_all_trigger_types_convert_to_engine_dataclasses():
    plans = resolve_day_plans(_req(exit={"time": "15:45", "triggers": [
        {"type": "underlying_move", "amount": 15, "unit": "percent"},
        {"type": "sigma_move", "n": 2.0},
        {"type": "net_delta", "threshold": 0.5},
        {"type": "pnl", "amount": 300, "unit": "points", "direction": "loss"},
    ]}))
    assert plans[0].exit_triggers == [
        UnderlyingMoveTrigger(amount=15.0, unit="percent"),
        SigmaMoveTrigger(n=2.0),
        NetDeltaTrigger(threshold=0.5),
        PnlTrigger(amount=300.0, unit="points", direction="loss"),
    ]


# --------------------------------------------------------------------------- #
# v4 — Hedge as a configurable module (triggers / conditions / target)
# --------------------------------------------------------------------------- #
def test_hedge_module_full_shape_accepted():
    h = HedgeConfig(
        enabled=True,
        instrument="es_future",
        triggers={"interval_minutes": 15, "delta_band": 0.1,
                  "sigma_move": {"enabled": True, "n": 1.5}},
        conditions=[
            {"type": "max_spread", "pct": 5.0, "min_ticks": 1},
            {"type": "min_quote_size", "size": 10},
            {"type": "min_rehedge_delta", "threshold": 0.05},
        ],
        target={"mode": "ratio", "ratio": 0.5},
    )
    assert h.triggers.sigma_move.enabled is True and h.triggers.sigma_move.n == 1.5
    assert len(h.conditions) == 3
    assert h.target.mode == "ratio" and h.target.ratio == 0.5


def test_hedge_triggers_off_via_null():
    h = HedgeConfig(triggers={"interval_minutes": None, "delta_band": None,
                              "sigma_move": {"enabled": True, "n": 1.0}})
    assert h.triggers.interval_minutes is None and h.triggers.delta_band is None


def test_hedge_unknown_instrument_rejected_422():
    with pytest.raises(ValidationError):
        HedgeConfig(instrument="spx_future")
    with pytest.raises(ValidationError):
        RunRequest(start_date="2025-02-03", end_date="2025-02-07",
                   hedge={"instrument": "vix_future"})


def test_hedge_unknown_condition_type_rejected():
    # min_premium / max_underlying_move are NOT valid hedge conditions.
    with pytest.raises(ValidationError):
        HedgeConfig(conditions=[{"type": "min_premium", "points": 0.5}])
    with pytest.raises(ValidationError):
        HedgeConfig(conditions=[{"type": "max_underlying_move", "pct": 1.0}])
    with pytest.raises(ValidationError):
        HedgeConfig(conditions=[{"type": "totally_unknown", "x": 1}])


def test_hedge_bad_condition_params_rejected():
    with pytest.raises(ValidationError):
        HedgeConfig(conditions=[{"type": "min_rehedge_delta", "threshold": 0}])
    with pytest.raises(ValidationError):
        HedgeConfig(conditions=[{"type": "min_rehedge_delta", "threshold": -0.1}])


def test_hedge_target_mode_and_ratio_validation():
    with pytest.raises(ValidationError):
        HedgeConfig(target={"mode": "half"})            # unknown mode
    with pytest.raises(ValidationError):
        HedgeConfig(target={"mode": "ratio", "ratio": 0})    # ratio must be > 0
    with pytest.raises(ValidationError):
        HedgeConfig(target={"mode": "ratio", "ratio": 1.5})  # ratio must be <= 1


def test_hedge_band_edge_requires_delta_band():
    # band_edge with delta_band disabled (null) -> rejected.
    with pytest.raises(ValidationError):
        HedgeConfig(triggers={"delta_band": None}, target={"mode": "band_edge"})
    # band_edge WITH a delta_band set -> accepted.
    ok = HedgeConfig(triggers={"delta_band": 0.1}, target={"mode": "band_edge"})
    assert ok.target.mode == "band_edge"


def test_to_engine_hedge_mirrors_module():
    h = HedgeConfig(
        enabled=True,
        triggers={"interval_minutes": 20, "delta_band": 0.2,
                  "sigma_move": {"enabled": True, "n": 2.0}},
        conditions=[
            {"type": "max_spread", "pct": 5.0, "min_ticks": 2},
            {"type": "min_quote_size", "size": 25},
            {"type": "min_rehedge_delta", "threshold": 0.07},
        ],
        target={"mode": "ratio", "ratio": 0.75},
    )
    spec = _to_engine_hedge(h)
    assert isinstance(spec, HedgeSpec)
    assert spec.enabled and spec.instrument == "es_future"
    assert spec.triggers.interval_minutes == 20 and spec.triggers.delta_band == 0.2
    assert spec.triggers.sigma_move.enabled is True and spec.triggers.sigma_move.n == 2.0
    assert spec.conditions == (
        MaxSpreadCond(pct=5.0, min_ticks=2.0),
        MinQuoteSizeCond(size=25),
        MinRehedgeDeltaCond(threshold=0.07),
    )
    assert spec.target.mode == "ratio" and spec.target.ratio == 0.75
