"""Unit tests for the intraday-backtest request validation / day resolution.

Pure — exercises ``resolve_day_plans`` / ``_pick_expiry`` directly, no DB.
Covers: out-of-window rejection, T2<=T1 rejection, inverted range, weekday
expansion, exception-date flagging, per-date overrides, and expiry resolution.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from tcg.core.api.intraday_backtest import (
    HedgeConfig,
    RunRequest,
    DateOverride,
    _pick_expiry,
    resolve_day_plans,
)


def _req(**over) -> RunRequest:
    base = dict(
        start_date="2025-02-03",
        end_date="2025-02-07",
        entry_time="10:00",
        exit_time="15:45",
    )
    base.update(over)
    return RunRequest(**base)


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
        resolve_day_plans(_req(entry_time="15:00", exit_time="10:00"))
    assert ei.value.status_code == 400


def test_weekday_expansion_and_exclusions():
    plans = resolve_day_plans(
        _req(exception_dates=["2025-02-05"])
    )
    # 2025-02-03 (Mon) .. 02-07 (Fri) => 5 weekdays.
    assert [p.date_int for p in plans] == [20250203, 20250204, 20250205, 20250206, 20250207]
    excluded = [p for p in plans if p.excluded]
    assert len(excluded) == 1 and excluded[0].date_int == 20250205


def test_date_override_changes_times():
    plans = resolve_day_plans(
        _req(date_overrides=[DateOverride(date="2025-02-04", entry_time="11:00", exit_time="14:00")])
    )
    d4 = next(p for p in plans if p.date_int == 20250204)
    # 11:00 ET winter -> 16:00Z; 14:00 ET -> 19:00Z.
    assert d4.entry_ts.hour == 16
    assert d4.exit_ts.hour == 19


def test_weekend_only_range_has_no_days():
    with pytest.raises(HTTPException):
        # 2025-02-08 Sat, 02-09 Sun.
        resolve_day_plans(_req(start_date="2025-02-08", end_date="2025-02-09"))


def test_pick_expiry_0dte():
    exps = [date(2025, 2, 3), date(2025, 2, 5), date(2025, 2, 7)]
    assert _pick_expiry(exps, date(2025, 2, 3), "0DTE", 0) == date(2025, 2, 3)
    assert _pick_expiry(exps, date(2025, 2, 4), "0DTE", 0) is None


def test_pick_expiry_ndte():
    exps = [date(2025, 2, 3), date(2025, 2, 10), date(2025, 2, 20)]
    # dte=5 from 2025-02-03 => nearest expiry >= +5 days = 2025-02-10.
    assert _pick_expiry(exps, date(2025, 2, 3), "NDTE", 5) == date(2025, 2, 10)
    # dte=0 => nearest expiry on/after day.
    assert _pick_expiry(exps, date(2025, 2, 4), "NDTE", 0) == date(2025, 2, 10)


def test_hedge_config_defaults():
    r = _req()
    assert r.hedge == HedgeConfig(enabled=True, interval_minutes=15.0, delta_band=0.10)
    assert r.snap_tolerance_minutes == 10.0
