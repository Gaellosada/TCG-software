"""Tests for the F3.2 date-allowlist entry mode (+ the params_echo fold-in).

Deterministic, NO dwh. Covers:
* schema defaults off + round-trip + bad-date rejection;
* ``resolve_day_plans``: allowlist OFF == baseline day set (regression); explicit
  dates trade only those; event_types resolve via the F3.1 calendar; union of the
  two; lenient handling of out-of-range / weekend explicit dates; empty-active 400;
* composition with ``custom_days`` exclude (exclude still removes from the
  allowlisted set) and with F2.2 regime side (allowlist filters WHICH days; regime
  decides the side on those that remain);
* cache participation (off hashes identically incl. equals a pre-feature body;
  on participates) + version bump;
* the params_echo fold-in: an off-run omits the inert regime/allowlist blocks; an
  active block is echoed in full.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from tcg.core.api import intraday_backtest as ib
from tcg.types.daily_series import DailySeries, DailySeriesPoint


# --------------------------------------------------------------------------- #
# Stubs (mirror the regime-test harness)
# --------------------------------------------------------------------------- #
class _StubDailyReader:
    def __init__(self, data: dict[str, dict[int, float]]) -> None:
        self._data = data
        self.calls: list[dict[str, Any]] = []

    async def read_series(
        self, symbol: str, *, start: Any = None, end: Any = None, field: str = "close"
    ) -> DailySeries:
        self.calls.append({"symbol": symbol, "start": start, "end": end})
        series = self._data.get(symbol, {})
        points = tuple(
            DailySeriesPoint(date=d, value=v) for d, v in sorted(series.items())
        )
        return DailySeries(symbol=symbol, field=field, points=points)


class _NoTradeReader:
    async def list_option_roots(self) -> list[dict[str, Any]]:
        return []

    async def list_expirations(self, oids: Any, start: Any) -> list[Any]:
        return []

    async def get_option_tick_size(self) -> float:
        return 0.05

    async def get_es_future_tick_size(self) -> float:
        return 0.25

    async def fetch_es_future_1m(self, *a: Any, **k: Any) -> list[Any]:
        return []


# Mon 2025-02-03 .. Fri 2025-02-14 => 10 weekdays. In range: 2025-02-07 (NFP),
# 2025-02-12 (CPI) are real curated event dates.
def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"start_date": "2025-02-03", "end_date": "2025-02-14"}
    base.update(over)
    return base


def _plan_days(req: ib.RunRequest) -> list[date]:
    return [p.day for p in ib.resolve_day_plans(req)]


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_allowlist_defaults_off_and_round_trips() -> None:
    r = ib.RunRequest(**_body())
    assert r.allowlist.mode == "off"
    assert r.allowlist.is_active is False
    assert r.allowlist.dates == [] and r.allowlist.event_types == []

    r2 = ib.RunRequest(
        **_body(allowlist={"mode": "allowlist", "dates": ["2025-02-04"],
                           "event_types": ["FOMC", "CPI"]})
    )
    assert r2.allowlist.is_active is True
    assert r2.allowlist.event_types == ["FOMC", "CPI"]


def test_allowlist_rejects_bad_date_and_bad_type() -> None:
    with pytest.raises(ValidationError):
        ib.RunRequest(**_body(allowlist={"mode": "allowlist", "dates": ["nope"]}))
    with pytest.raises(ValidationError):
        ib.RunRequest(**_body(allowlist={"mode": "allowlist", "event_types": ["PPI"]}))


# --------------------------------------------------------------------------- #
# resolve_day_plans — the day-set filter
# --------------------------------------------------------------------------- #
def test_allowlist_off_baseline_day_set_unchanged() -> None:
    # Regression: default-off produces exactly the weekday expansion.
    baseline = _plan_days(ib.RunRequest(**_body()))
    assert baseline == [
        date(2025, 2, d) for d in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)
    ]
    # An explicit mode='off' with dates present is still inert.
    inert = _plan_days(
        ib.RunRequest(**_body(allowlist={"mode": "off", "dates": ["2025-02-04"]}))
    )
    assert inert == baseline


def test_allowlist_explicit_dates_only_those_trade() -> None:
    days = _plan_days(
        ib.RunRequest(
            **_body(allowlist={"mode": "allowlist",
                               "dates": ["2025-02-04", "2025-02-11"]})
        )
    )
    assert days == [date(2025, 2, 4), date(2025, 2, 11)]


def test_allowlist_event_types_resolve_via_calendar() -> None:
    # NFP 2025-02-07 and CPI 2025-02-12 are the only in-range curated events.
    nfp = _plan_days(
        ib.RunRequest(**_body(allowlist={"mode": "allowlist", "event_types": ["NFP"]}))
    )
    assert nfp == [date(2025, 2, 7)]

    both = _plan_days(
        ib.RunRequest(
            **_body(allowlist={"mode": "allowlist", "event_types": ["NFP", "CPI"]})
        )
    )
    assert both == [date(2025, 2, 7), date(2025, 2, 12)]


def test_allowlist_unions_explicit_and_event_types() -> None:
    days = _plan_days(
        ib.RunRequest(
            **_body(allowlist={"mode": "allowlist", "dates": ["2025-02-04"],
                               "event_types": ["CPI"]})
        )
    )
    assert days == [date(2025, 2, 4), date(2025, 2, 12)]


def test_allowlist_lenient_on_out_of_range_and_weekend_dates() -> None:
    # 2025-02-08 is a Saturday; 2025-03-01 is out of range. Both simply never
    # match a weekday plan (no error) — 2025-02-04 still trades.
    days = _plan_days(
        ib.RunRequest(
            **_body(allowlist={"mode": "allowlist",
                               "dates": ["2025-02-04", "2025-02-08", "2025-03-01"]})
        )
    )
    assert days == [date(2025, 2, 4)]


def test_allowlist_active_but_empty_resolution_raises_400() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        ib.resolve_day_plans(ib.RunRequest(**_body(allowlist={"mode": "allowlist"})))
    assert exc.value.status_code == 400
    assert "allowlist" in exc.value.detail


# --------------------------------------------------------------------------- #
# Composition with custom_days exclude
# --------------------------------------------------------------------------- #
def test_allowlist_composes_with_custom_days_exclude() -> None:
    plans = ib.resolve_day_plans(
        ib.RunRequest(
            **_body(
                allowlist={"mode": "allowlist", "dates": ["2025-02-04", "2025-02-05"]},
                custom_days=[{"date": "2025-02-05", "exclude": True}],
            )
        )
    )
    by_day = {p.day: p for p in plans}
    # Both allowlisted days are emitted; the excluded one carries excluded=True.
    assert set(by_day) == {date(2025, 2, 4), date(2025, 2, 5)}
    assert by_day[date(2025, 2, 4)].excluded is False
    assert by_day[date(2025, 2, 5)].excluded is True


# --------------------------------------------------------------------------- #
# Composition with F2.2 regime side (allowlist filters days; regime sets side)
# --------------------------------------------------------------------------- #
async def test_allowlist_composes_with_regime_side() -> None:
    # Allowlist restricts to one date; regime_driven (fallback path, no reader)
    # still resolves that day's side => the filtered day carries a regime decision.
    req = ib.RunRequest(
        **_body(
            allowlist={"mode": "allowlist", "dates": ["2025-02-04"]},
            straddle_side="short",
            regime={"side_mode": "regime_driven", "rv_windows": [20, 30, 100]},
        )
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=None)
    assert [d["date"] for d in result["days"]] == ["2025-02-04"]
    day = result["days"][0]
    assert day["regime"] is not None
    # Fallback (no reader) => static short side on the allowlisted day.
    assert day["regime"]["side"] == "short"


async def test_allowlist_run_filters_emitted_days() -> None:
    req = ib.RunRequest(
        **_body(allowlist={"mode": "allowlist", "event_types": ["NFP", "CPI"]})
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=None)
    assert [d["date"] for d in result["days"]] == ["2025-02-07", "2025-02-12"]


async def test_allowlist_off_run_emits_full_day_set_and_no_allowlist_key() -> None:
    req = ib.RunRequest(**_body())
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=None)
    assert len(result["days"]) == 10
    assert "allowlist" not in result["params_echo"]  # fold-in: inert block omitted


# --------------------------------------------------------------------------- #
# Cache participation + version
# --------------------------------------------------------------------------- #
def test_cache_key_allowlist_off_ignores_inert_subconfig() -> None:
    a = ib.RunRequest(**_body())
    b = ib.RunRequest(**_body(allowlist={"mode": "off", "dates": ["2025-02-04"],
                                         "event_types": ["FOMC"]}))
    assert ib._intraday_cache_key(a) == ib._intraday_cache_key(b)


def test_cache_key_allowlist_off_equals_pre_feature_body() -> None:
    from tcg.core.cache import canonical_hash

    req = ib.RunRequest(**_body())
    payload = ib._strip_use_cache(req.model_dump(mode="json"))
    payload.pop("regime", None)
    payload.pop("allowlist", None)  # a pre-F3.2 body had no allowlist key
    payload.pop("ladder", None)  # a pre-F4.1 body had no ladder key
    pre_feature = canonical_hash({"_cv": ib.INTRADAY_COMPUTE_VERSION, "body": payload})
    assert ib._intraday_cache_key(req) == pre_feature


def test_cache_key_allowlist_on_participates() -> None:
    off = ib.RunRequest(**_body())
    on = ib.RunRequest(**_body(allowlist={"mode": "allowlist", "dates": ["2025-02-04"]}))
    assert ib._intraday_cache_key(off) != ib._intraday_cache_key(on)
    on2 = ib.RunRequest(**_body(allowlist={"mode": "allowlist", "dates": ["2025-02-05"]}))
    assert ib._intraday_cache_key(on) != ib._intraday_cache_key(on2)
    on_ev = ib.RunRequest(**_body(allowlist={"mode": "allowlist", "event_types": ["FOMC"]}))
    assert ib._intraday_cache_key(on) != ib._intraday_cache_key(on_ev)


def test_compute_version_bumped() -> None:
    # 0.7.0 -> 0.8.0 for the 0DTE gap-closure (settlement-intrinsic exit +
    # default exit_mode="auto" + leg-sync + strict-> + crossed-quote exclusion):
    # compute output shifts, so stale cache must not be served.
    assert ib.INTRADAY_COMPUTE_VERSION == "0.8.0"


# --------------------------------------------------------------------------- #
# params_echo fold-in
# --------------------------------------------------------------------------- #
def test_echo_params_omits_inert_regime_and_allowlist() -> None:
    echo = ib._echo_params(ib.RunRequest(**_body()))
    assert "regime" not in echo
    assert "allowlist" not in echo
    # Other fields still present.
    assert echo["start_date"] == "2025-02-03"
    assert "cost" in echo


def test_echo_params_keeps_active_blocks() -> None:
    req = ib.RunRequest(
        **_body(
            allowlist={"mode": "allowlist", "dates": ["2025-02-04"]},
            regime={"emit_signals": True},
        )
    )
    echo = ib._echo_params(req)
    assert echo["allowlist"]["mode"] == "allowlist"
    assert echo["regime"]["emit_signals"] is True


# --------------------------------------------------------------------------- #
# F3.1 read endpoint (frontend exposure path for A3)
# --------------------------------------------------------------------------- #
async def test_event_calendar_endpoint_shape_and_content() -> None:
    payload = await ib.get_event_calendar()
    assert payload["event_types"] == ["FOMC", "NFP", "CPI"]
    # Grouped-by-type with tentative flags.
    fomc = payload["events"]["FOMC"]
    assert {"date": "2025-01-29", "tentative": False} in fomc
    # A real NFP + CPI date surfaces.
    nfp_dates = [e["date"] for e in payload["events"]["NFP"]]
    cpi_dates = [e["date"] for e in payload["events"]["CPI"]]
    assert "2025-08-01" in nfp_dates
    assert "2025-10-24" in cpi_dates  # shutdown-rescheduled Sep CPI
    # Flat union is de-duplicated + sorted and covers every typed date.
    all_dates = payload["all_dates"]
    assert all_dates == sorted(set(all_dates))
    assert set(all_dates) == set(nfp_dates) | set(cpi_dates) | {
        e["date"] for e in fomc
    }
    # No tentative dates shipped currently.
    assert payload["tentative_dates"] == []
