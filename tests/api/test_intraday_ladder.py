"""Tests for F4.1 laddered multi-entry (schema + schedule + cache + the W4
fold-in). Deterministic, NO dwh.

The per-entry SIMULATION loop (``_process_day``) + serializer are covered in
``test_intraday_ladder_run.py``; this file covers the pure, DB-free surface:
* schema defaults off + round-trip + validation;
* ``resolve_day_plans`` schedule generation (interval + cutoff + exit bound;
  first_entry default; per-day custom-entry override; off => single entry);
* the W4 fold-in guard (allowlist-active exclude of a non-allowlisted day 400s);
* cache participation (off hashes identically incl. equals a pre-feature body;
  on participates) + version bump;
* the params_echo fold-in for the inert ladder block.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tcg.core.api import intraday_backtest as ib


def _body(**over):
    base = {"start_date": "2025-02-03", "end_date": "2025-02-14"}
    base.update(over)
    return base


def _plan(req: ib.RunRequest, day: date) -> ib.DayPlan:
    return next(p for p in ib.resolve_day_plans(req) if p.day == day)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_ladder_defaults_off_and_round_trips() -> None:
    r = ib.RunRequest(**_body())
    assert r.ladder.enabled is False
    assert r.ladder.is_active is False
    assert r.ladder.interval_minutes == 30.0
    assert r.ladder.max_concurrent == 0
    assert r.ladder.sizing.mode == "equal_contracts"
    assert r.ladder.sizing.contracts == 1.0

    r2 = ib.RunRequest(
        **_body(
            ladder={
                "enabled": True,
                "interval_minutes": 15,
                "first_entry": "10:00",
                "last_entry_cutoff": "15:00",
                "max_concurrent": 3,
                "sizing": {"mode": "equal_notional", "contracts": 2.0},
            }
        )
    )
    assert r2.ladder.is_active is True
    assert r2.ladder.sizing.mode == "equal_notional"
    assert r2.ladder.first_entry == "10:00"


def test_ladder_rejects_bad_values() -> None:
    with pytest.raises(ValidationError):
        ib.RunRequest(**_body(ladder={"interval_minutes": 0}))  # ge=1
    with pytest.raises(ValidationError):
        ib.RunRequest(**_body(ladder={"first_entry": "nope"}))  # bad HH:MM
    with pytest.raises(ValidationError):
        ib.RunRequest(**_body(ladder={"max_concurrent": -1}))  # ge=0
    with pytest.raises(ValidationError):
        ib.RunRequest(**_body(ladder={"sizing": {"contracts": 0}}))  # gt=0


# --------------------------------------------------------------------------- #
# Schedule generation (resolve_day_plans)
# --------------------------------------------------------------------------- #
def test_ladder_off_single_entry_baseline() -> None:
    # Ladder off => exactly one entry at the entry module's time.
    req = ib.RunRequest(**_body(entry={"time": "10:00"}, exit={"time": "15:45"}))
    plan = _plan(req, date(2025, 2, 3))
    assert len(plan.entry_tss) == 1
    assert plan.entry_tss[0] == plan.entry_ts
    # 10:00 ET winter == 15:00Z.
    assert plan.entry_ts.hour == 15 and plan.entry_ts.minute == 0


def test_ladder_schedule_interval_and_cutoff() -> None:
    req = ib.RunRequest(
        **_body(
            entry={"time": "10:00"},
            exit={"time": "16:00"},
            ladder={
                "enabled": True,
                "interval_minutes": 60,
                "last_entry_cutoff": "13:00",
            },
        )
    )
    plan = _plan(req, date(2025, 2, 3))
    # 10:00, 11:00, 12:00, 13:00 (<= cutoff and < exit). 4 rungs.
    mins = [(t.hour, t.minute) for t in plan.entry_tss]
    assert mins == [(15, 0), (16, 0), (17, 0), (18, 0)]  # ET+5h in winter
    # first rung is the day anchor.
    assert plan.entry_ts == plan.entry_tss[0]


def test_ladder_cutoff_defaults_to_exit_and_excludes_exit_itself() -> None:
    req = ib.RunRequest(
        **_body(
            entry={"time": "15:00"},
            exit={"time": "16:00"},
            ladder={"enabled": True, "interval_minutes": 30},
        )
    )
    plan = _plan(req, date(2025, 2, 3))
    # 15:00, 15:30 — 16:00 is the exit and is strictly excluded.
    assert [(t.hour, t.minute) for t in plan.entry_tss] == [(20, 0), (20, 30)]


def test_ladder_first_entry_default_is_entry_time_and_override_tracks_custom_day() -> None:
    # first_entry unset => uses the entry module time; a custom_days entry
    # override for that day shifts the ladder start (per-day resolution).
    req = ib.RunRequest(
        **_body(
            entry={"time": "10:00"},
            exit={"time": "16:00"},
            ladder={"enabled": True, "interval_minutes": 120},
            custom_days=[{"date": "2025-02-04", "entry": {"time": "12:00"}}],
        )
    )
    d3 = _plan(req, date(2025, 2, 3))
    d4 = _plan(req, date(2025, 2, 4))
    assert [(t.hour, t.minute) for t in d3.entry_tss] == [(15, 0), (17, 0), (19, 0)]
    # 2025-02-04 starts at 12:00 ET (17:00Z), stepping 120 min.
    assert [(t.hour, t.minute) for t in d4.entry_tss] == [(17, 0), (19, 0)]


def test_ladder_explicit_first_entry_wins() -> None:
    req = ib.RunRequest(
        **_body(
            entry={"time": "10:00"},
            exit={"time": "16:00"},
            ladder={
                "enabled": True,
                "interval_minutes": 60,
                "first_entry": "14:00",
            },
        )
    )
    plan = _plan(req, date(2025, 2, 3))
    assert [(t.hour, t.minute) for t in plan.entry_tss] == [(19, 0), (20, 0)]


def test_ladder_no_rung_before_exit_raises_400() -> None:
    req = ib.RunRequest(
        **_body(
            entry={"time": "10:00"},
            exit={"time": "16:00"},
            ladder={
                "enabled": True,
                "interval_minutes": 30,
                "first_entry": "15:59",
                "last_entry_cutoff": "15:59",
            },
        )
    )
    # 15:59 rung: is it < 16:00 exit? yes -> actually one rung. Force none: cutoff
    # before first is handled; here use a first at/after exit.
    req2 = ib.RunRequest(
        **_body(
            entry={"time": "10:00"},
            exit={"time": "16:00"},
            ladder={"enabled": True, "interval_minutes": 30, "first_entry": "16:00"},
        )
    )
    # first rung 16:00 == exit -> excluded -> no rung -> 400.
    with pytest.raises(HTTPException) as exc:
        ib.resolve_day_plans(req2)
    assert exc.value.status_code == 400
    assert "no entry before the exit" in exc.value.detail
    # req (15:59) yields exactly one rung, no error.
    assert len(_plan(req, date(2025, 2, 3)).entry_tss) == 1


def test_ladder_too_many_entries_raises_400() -> None:
    # 09:30..16:00 == 390 min; 1-min interval => 390 rungs (< cap 500, ok). Use a
    # tiny fractional interval to blow the cap.
    req = ib.RunRequest(
        **_body(
            entry={"time": "09:30"},
            exit={"time": "16:00"},
            ladder={"enabled": True, "interval_minutes": 1},
        )
    )
    plan = _plan(req, date(2025, 2, 3))
    assert len(plan.entry_tss) == 390  # 09:30..15:59 inclusive


# --------------------------------------------------------------------------- #
# W4 fold-in: allowlist-active exclude of a non-allowlisted day
# --------------------------------------------------------------------------- #
def test_foldin_exclude_of_non_allowlisted_day_raises_400() -> None:
    req = ib.RunRequest(
        **_body(
            allowlist={"mode": "allowlist", "dates": ["2025-02-04"]},
            custom_days=[{"date": "2025-02-05", "exclude": True}],  # not allowlisted
        )
    )
    with pytest.raises(HTTPException) as exc:
        ib.resolve_day_plans(req)
    assert exc.value.status_code == 400
    assert "not in the active allowlist" in exc.value.detail


def test_foldin_exclude_of_allowlisted_day_still_works() -> None:
    # An exclude that IS in the allowlist is emitted as excluded (unchanged).
    req = ib.RunRequest(
        **_body(
            allowlist={"mode": "allowlist", "dates": ["2025-02-04", "2025-02-05"]},
            custom_days=[{"date": "2025-02-05", "exclude": True}],
        )
    )
    plans = {p.day: p for p in ib.resolve_day_plans(req)}
    assert plans[date(2025, 2, 5)].excluded is True


def test_foldin_exclude_no_allowlist_unchanged() -> None:
    # With no allowlist active, any in-range weekday exclude is fine (baseline).
    req = ib.RunRequest(
        **_body(custom_days=[{"date": "2025-02-05", "exclude": True}])
    )
    plans = {p.day: p for p in ib.resolve_day_plans(req)}
    assert plans[date(2025, 2, 5)].excluded is True


# --------------------------------------------------------------------------- #
# Cache participation + version + echo
# --------------------------------------------------------------------------- #
def test_cache_key_ladder_off_ignores_inert_subconfig() -> None:
    a = ib.RunRequest(**_body())
    b = ib.RunRequest(
        **_body(ladder={"enabled": False, "interval_minutes": 5,
                        "sizing": {"mode": "equal_notional"}})
    )
    assert ib._intraday_cache_key(a) == ib._intraday_cache_key(b)


def test_cache_key_ladder_off_equals_pre_feature_body() -> None:
    from tcg.core.cache import canonical_hash

    req = ib.RunRequest(**_body())
    payload = ib._strip_use_cache(req.model_dump(mode="json"))
    payload.pop("regime", None)
    payload.pop("allowlist", None)
    payload.pop("ladder", None)  # a pre-F4.1 body had no ladder key
    pre_feature = canonical_hash({"_cv": ib.INTRADAY_COMPUTE_VERSION, "body": payload})
    assert ib._intraday_cache_key(req) == pre_feature


def test_cache_key_ladder_on_participates() -> None:
    off = ib.RunRequest(**_body())
    on = ib.RunRequest(**_body(ladder={"enabled": True, "interval_minutes": 30}))
    assert ib._intraday_cache_key(off) != ib._intraday_cache_key(on)
    on2 = ib.RunRequest(**_body(ladder={"enabled": True, "interval_minutes": 15}))
    assert ib._intraday_cache_key(on) != ib._intraday_cache_key(on2)
    on_sz = ib.RunRequest(
        **_body(ladder={"enabled": True, "interval_minutes": 30,
                        "sizing": {"mode": "equal_notional"}})
    )
    assert ib._intraday_cache_key(on) != ib._intraday_cache_key(on_sz)


def test_compute_version_bumped_for_ladder() -> None:
    # 0.7.0 -> 0.8.0: the 0DTE gap-closure (settlement-intrinsic exit + default
    # exit_mode="auto", leg-sync, strict-> front selection, crossed-quote
    # exclusion) changes compute output, so the cache namespace must be bumped.
    assert ib.INTRADAY_COMPUTE_VERSION == "0.8.0"


def test_echo_omits_inert_ladder_keeps_active() -> None:
    assert "ladder" not in ib._echo_params(ib.RunRequest(**_body()))
    echo = ib._echo_params(
        ib.RunRequest(**_body(ladder={"enabled": True, "interval_minutes": 30}))
    )
    assert echo["ladder"]["enabled"] is True
