"""Unit tests for the pure intraday straddle engine (v2 — conditional
entry/exit modules + fully independent legs).

All synthetic — no dwh. Covers: ATM selection, DST, carry-forward marks, each
condition type (pass/fail incl. the tick-floor and two-sided requirement),
INDEPENDENT per-leg entry at DIFFERENT timestamps, entry skip reasons (no-bars
vs conditions-unmet), exit fallback-to-nearest (exit_conditions_met=False), the
BOTH-ON delta-hedge window under asymmetric fills, P&L sign long vs short,
dollarization, and the T2<=T1 guard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tcg.engine.intraday_backtest import (
    _hedge_time_gate_ok,
    _leg_delta,
    _running_extremum,
    _skip_near_extremum,
    aggregate_days,
    last_known_at_or_before,
    leg_bar_qualifies,
    net_straddle_delta,
    resolve_et_to_utc,
    scan_leg_entry,
    scan_leg_exit,
    select_atm_strike,
    simulate_day,
    snap_nearest,
    underlying_move_ok,
)
from tcg.engine.options.pricing import BS76Kernel
from tcg.types.intraday import (
    ES_OPTION_TICK_SIZE,
    es_option_tick,
    HedgeSpec,
    HedgeTargetSpec,
    HedgeTimingSpec,
    HedgeTriggers,
    IntradayBar,
    MaxSpreadCond,
    MaxUnderlyingMoveCond,
    MinPremiumCond,
    MinQuoteSizeCond,
    MinRehedgeDeltaCond,
    NetDeltaTrigger,
    PnlTrigger,
    SigmaMoveHedgeTrigger,
    SigmaMoveTrigger,
    SkipNearExtremumSpec,
    UnderlyingMoveTrigger,
)

UTC = timezone.utc


def _hspec(
    *,
    enabled: bool = True,
    interval_minutes: float | None = 15.0,
    delta_band: float | None = 0.10,
    sigma_enabled: bool = False,
    sigma_n: float = 1.0,
    conditions: tuple = (),
    mode: str = "zero",
    ratio: float = 1.0,
    instrument: str = "es_future",
    timing: HedgeTimingSpec | None = None,
) -> HedgeSpec:
    """Build a v4 engine HedgeSpec from flat kwargs (test convenience)."""
    return HedgeSpec(
        enabled=enabled,
        instrument=instrument,
        triggers=HedgeTriggers(
            interval_minutes=interval_minutes,
            delta_band=delta_band,
            sigma_move=SigmaMoveHedgeTrigger(enabled=sigma_enabled, n=sigma_n),
        ),
        conditions=tuple(conditions),
        target=HedgeTargetSpec(mode=mode, ratio=ratio),
        timing=timing or HedgeTimingSpec(),
    )


def _esq(ts: datetime, price: float, *, bid=None, ask=None, bs=None, as_=None) -> IntradayBar:
    """A quoted ES-future bar (two-sided when bid/ask supplied)."""
    return IntradayBar(ts=ts, price=price, bid=bid, ask=ask, bid_size=bs, ask_size=as_)


def _bars(base: datetime, prices: list[float], step_min: int = 1) -> list[IntradayBar]:
    return [
        IntradayBar(ts=base + timedelta(minutes=i * step_min), price=p)
        for i, p in enumerate(prices)
    ]


def _q(ts: datetime, mid: float, *, bid=None, ask=None, bs=None, as_=None) -> IntradayBar:
    """A quoted option bar (two-sided when bid/ask supplied)."""
    return IntradayBar(ts=ts, price=mid, bid=bid, ask=ask, bid_size=bs, ask_size=as_)


# --------------------------------------------------------------------------- #
# Timezone / DST
# --------------------------------------------------------------------------- #
def test_dst_winter_vs_summer():
    assert resolve_et_to_utc(date(2025, 1, 15), "10:00").hour == 15  # EST -5
    assert resolve_et_to_utc(date(2025, 7, 15), "10:00").hour == 14  # EDT -4


# --------------------------------------------------------------------------- #
# ATM selection
# --------------------------------------------------------------------------- #
def test_select_atm_nearest():
    assert select_atm_strike(5851.2, [5840, 5845, 5850, 5855]) == 5850
    assert select_atm_strike(5853.0, [5850, 5855]) == 5855


def test_select_atm_tie_prefers_lower():
    assert select_atm_strike(5852.5, [5850, 5855]) == 5850


def test_select_atm_empty_raises():
    with pytest.raises(ValueError):
        select_atm_strike(5000.0, [])


# --------------------------------------------------------------------------- #
# Snap / carry-forward
# --------------------------------------------------------------------------- #
def test_snap_within_tolerance():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    bars = _bars(base, [10.0, 11.0, 12.0])
    hit = snap_nearest(bars, base + timedelta(minutes=1, seconds=20), 10.0)
    assert hit is not None and hit.price == 11.0


def test_snap_outside_tolerance_returns_none():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    assert snap_nearest(_bars(base, [10.0]), base + timedelta(minutes=30), 10.0) is None


def test_last_known_at_or_before_no_lookahead():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    bars = _bars(base, [10.0, 11.0, 12.0])
    hit = last_known_at_or_before(bars, base + timedelta(minutes=1, seconds=50))
    assert hit is not None and hit.price == 11.0
    assert last_known_at_or_before(bars, base - timedelta(minutes=1)) is None
    assert last_known_at_or_before(bars, base + timedelta(minutes=2)).price == 12.0


# --------------------------------------------------------------------------- #
# Condition types — pass/fail
# --------------------------------------------------------------------------- #
def test_max_spread_pass_fail_and_two_sided_required():
    ts = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    tight = _q(ts, 10.0, bid=9.9, ask=10.1, bs=50, as_=50)   # spread 0.2
    wide = _q(ts, 10.0, bid=9.0, ask=11.0, bs=50, as_=50)    # spread 2.0
    lasttrade = _q(ts, 10.0)                                 # no two-sided quote
    cond = [MaxSpreadCond(pct=5.0, min_ticks=1.0)]           # floor max(0.5, 0.05)=0.5
    assert leg_bar_qualifies(tight, cond, ES_OPTION_TICK_SIZE) is True
    assert leg_bar_qualifies(wide, cond, ES_OPTION_TICK_SIZE) is False
    assert leg_bar_qualifies(lasttrade, cond, ES_OPTION_TICK_SIZE) is False


def test_max_spread_tick_floor_dominates_for_cheap_option():
    # mid=0.20, pct=1% => pct floor = 0.002, but min_ticks=2 * 0.05 = 0.10.
    # spread 0.05 (one tick) must PASS on the tick floor even though it exceeds
    # the pct floor (0.002).
    ts = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    bar = _q(ts, 0.20, bid=0.175, ask=0.225, bs=10, as_=10)  # spread 0.05
    tight = [MaxSpreadCond(pct=1.0, min_ticks=2.0)]          # floor = max(0.002,0.10)=0.10
    assert leg_bar_qualifies(bar, tight, ES_OPTION_TICK_SIZE) is True
    # With min_ticks=0 the pct floor (0.002) applies and 0.05 spread FAILS.
    assert leg_bar_qualifies(bar, [MaxSpreadCond(pct=1.0, min_ticks=0.0)],
                             ES_OPTION_TICK_SIZE) is False


def test_min_quote_size_pass_fail():
    ts = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    big = _q(ts, 10.0, bid=9.9, ask=10.1, bs=25, as_=25)
    small = _q(ts, 10.0, bid=9.9, ask=10.1, bs=25, as_=5)
    none = _q(ts, 10.0)  # sizes None
    cond = [MinQuoteSizeCond(size=10)]
    assert leg_bar_qualifies(big, cond, ES_OPTION_TICK_SIZE) is True
    assert leg_bar_qualifies(small, cond, ES_OPTION_TICK_SIZE) is False
    assert leg_bar_qualifies(none, cond, ES_OPTION_TICK_SIZE) is False


def test_min_premium_pass_fail():
    ts = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    cond = [MinPremiumCond(points=0.50)]
    assert leg_bar_qualifies(_q(ts, 0.75), cond, ES_OPTION_TICK_SIZE) is True
    assert leg_bar_qualifies(_q(ts, 0.25), cond, ES_OPTION_TICK_SIZE) is False


def test_max_underlying_move():
    cond = MaxUnderlyingMoveCond(pct=1.0, ref="day_open")
    assert underlying_move_ok(5010.0, 5000.0, cond) is True   # +0.2%
    assert underlying_move_ok(5100.0, 5000.0, cond) is False  # +2.0%
    assert underlying_move_ok(5010.0, None, cond) is False    # no ref -> fail closed


# --------------------------------------------------------------------------- #
# Independent-leg entry scanning
# --------------------------------------------------------------------------- #
def test_scan_leg_entry_first_qualifying_forward():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    # bars at +0 (fails min_premium), +2 (passes), +4 (passes) -> take +2.
    marks = [_q(base, 0.10), _q(base + timedelta(minutes=2), 1.0),
             _q(base + timedelta(minutes=4), 2.0)]
    bar, reason = scan_leg_entry(marks, [], base, 10.0, [MinPremiumCond(points=0.5)],
                                 None, None, ES_OPTION_TICK_SIZE)
    assert reason is None and bar.ts == base + timedelta(minutes=2)


def test_scan_leg_entry_no_bars_vs_conditions_unmet():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    # No bars in the forward window at all.
    bar, reason = scan_leg_entry([], [], base, 10.0, [], None, None, ES_OPTION_TICK_SIZE)
    assert bar is None and reason == "no_bars"
    # Bars exist but none pass a condition.
    marks = [_q(base + timedelta(minutes=1), 0.10)]
    bar, reason = scan_leg_entry(marks, [], base, 10.0, [MinPremiumCond(points=5.0)],
                                 None, None, ES_OPTION_TICK_SIZE)
    assert bar is None and reason == "conditions_unmet"


def test_scan_leg_exit_fallback_sets_not_met():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    # Only bar in the exit window fails min_premium -> fall back to it, not met.
    marks = [_q(base + timedelta(minutes=1), 0.10)]
    bar, met = scan_leg_exit(marks, [], base, 10.0, [MinPremiumCond(points=5.0)],
                             None, None, ES_OPTION_TICK_SIZE)
    assert bar is not None and met is False


# --------------------------------------------------------------------------- #
# simulate_day — helpers + core
# --------------------------------------------------------------------------- #
def _day(**over):
    """Well-formed day: flat ES, marks present every minute at entry & exit."""
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:30")
    n = int((exit_ - entry).total_seconds() // 60) + 1
    es = _bars(entry, [5000.0] * n)
    calls = _bars(entry, [30.0] * n)
    puts = _bars(entry, [30.0] * n)
    kwargs = dict(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=calls, put_marks=puts,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=False),
    )
    kwargs.update(over)
    return kwargs


def test_simulate_ok_when_quotes_present():
    res = simulate_day(**_day())
    assert res.status == "ok"
    assert res.entry is not None and res.exit is not None
    assert res.legs is not None
    assert res.straddle_on_ts is not None and res.straddle_off_ts is not None


def test_simulate_skips_no_quote_within_tolerance():
    res = simulate_day(**_day(call_marks=[], put_marks=[]))
    assert res.status == "skipped" and res.skip_reason == "no_quote_within_tolerance"


def test_simulate_skips_entry_conditions_unmet():
    # Bars exist but none pass min_premium -> entry_conditions_unmet (NOT no_quote).
    kw = _day(entry_conditions=[MinPremiumCond(points=999.0)])
    res = simulate_day(**kw)
    assert res.status == "skipped" and res.skip_reason == "entry_conditions_unmet"


def test_no_quote_dominates_conditions_unmet():
    # Call has NO bars (no_bars); put has bars that fail conditions. The day's
    # reason is no_quote_within_tolerance (no-bars dominates).
    kw = _day(call_marks=[], entry_conditions=[MinPremiumCond(points=999.0)])
    res = simulate_day(**kw)
    assert res.skip_reason == "no_quote_within_tolerance"


# --------------------------------------------------------------------------- #
# INDEPENDENT legs at DIFFERENT timestamps (the t vs t+3 case)
# --------------------------------------------------------------------------- #
def test_independent_legs_fill_at_different_timestamps():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:20")
    es = _bars(entry, [5000.0] * 25)
    # CALL prints a two-sided quote at t; PUT's first two-sided quote at t+3.
    calls = [_q(entry, 30.0, bid=29.9, ask=30.1, bs=50, as_=50),
             _q(exit_, 30.0, bid=29.9, ask=30.1, bs=50, as_=50)]
    puts = [_q(entry, 30.0),  # last-trade-only -> fails max_spread
            _q(entry + timedelta(minutes=3), 30.0, bid=29.9, ask=30.1, bs=50, as_=50),
            _q(exit_, 30.0, bid=29.9, ask=30.1, bs=50, as_=50)]
    cond = [MaxSpreadCond(pct=5.0, min_ticks=1.0)]
    res = simulate_day(**_day(call_marks=calls, put_marks=puts, es_bars=es,
                              exit_ts=exit_, entry_conditions=cond, exit_conditions=cond))
    assert res.status == "ok"
    assert res.legs.call.entry_ts == entry
    assert res.legs.put.entry_ts == entry + timedelta(minutes=3)
    # straddle_on = max(call, put) = the later put fill.
    assert res.straddle_on_ts == entry + timedelta(minutes=3)


def test_snap_tolerance_bounds_leg_gap_skips_day():
    # PUT's first qualifying quote arrives AFTER its snap window -> can't enter
    # -> SKIP (never hold a naked leg).
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:20")
    es = _bars(entry, [5000.0] * 25)
    cond = [MaxSpreadCond(pct=5.0, min_ticks=1.0)]
    calls = [_q(entry, 30.0, bid=29.9, ask=30.1, bs=50, as_=50)]
    # Only two-sided put quote is at t+8, beyond a 5-min snap window.
    puts = [_q(entry, 30.0), _q(entry + timedelta(minutes=8), 30.0,
                                bid=29.9, ask=30.1, bs=50, as_=50)]
    res = simulate_day(**_day(call_marks=calls, put_marks=puts, es_bars=es,
                              exit_ts=exit_, entry_tol=5.0, entry_conditions=cond))
    assert res.status == "skipped"
    assert res.skip_reason == "entry_conditions_unmet"  # put had bars, none qualified in window


# --------------------------------------------------------------------------- #
# Exit fallback to nearest with exit_conditions_met=False
# --------------------------------------------------------------------------- #
def test_exit_fallback_nearest_not_met():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:20")
    es = _bars(entry, [5000.0] * 25)
    # Entry OK (no conditions). Exit requires a premium >= 999 -> never met,
    # both legs fall back to nearest bar in the exit window.
    calls = _bars(entry, [30.0] * 25)
    puts = _bars(entry, [30.0] * 25)
    res = simulate_day(**_day(call_marks=calls, put_marks=puts, es_bars=es,
                              exit_ts=exit_, exit_conditions=[MinPremiumCond(points=999.0)]))
    assert res.status == "ok"  # must still close
    assert res.legs.call.exit_conditions_met is False
    assert res.legs.put.exit_conditions_met is False


# --------------------------------------------------------------------------- #
# P&L sign long vs short + dollarization + per-leg pnl_pts
# --------------------------------------------------------------------------- #
def test_pnl_sign_long_vs_short_and_dollarization():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:05")
    es = _bars(entry, [5000.0] * 6)
    calls = [IntradayBar(entry, 30.0), IntradayBar(exit_, 50.0)]
    puts = [IntradayBar(entry, 30.0), IntradayBar(exit_, 5.0)]
    common = dict(
        date_int=20250303, strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=calls, put_marks=puts,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=False),
    )
    long_res = simulate_day(side="long", **common)
    short_res = simulate_day(side="short", **common)
    # Straddle 60 -> 55: long loses 5, short gains 5.
    assert long_res.pnl.option_pnl_pts == pytest.approx(-5.0)
    assert short_res.pnl.option_pnl_pts == pytest.approx(5.0)
    assert long_res.pnl.total_pnl_usd == pytest.approx(-250.0)
    assert short_res.pnl.total_pnl_usd == pytest.approx(250.0)
    # Per-leg pnl_pts (points): call +20, put -25 for long; option = call+put.
    assert long_res.legs.call.pnl_pts == pytest.approx(20.0)
    assert long_res.legs.put.pnl_pts == pytest.approx(-25.0)
    assert (long_res.legs.call.pnl_pts + long_res.legs.put.pnl_pts) == pytest.approx(
        long_res.pnl.option_pnl_pts
    )


# --------------------------------------------------------------------------- #
# Delta-hedge triggers: interval AND band (both-on window)
# --------------------------------------------------------------------------- #
def test_hedge_triggers_on_interval():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=5)
    es = _bars(entry, [5000.0] * 6)
    marks = _bars(entry, [30.0] * 6)
    res = simulate_day(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=marks, put_marks=marks,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=True, interval_minutes=1.0, delta_band=10.0),
    )
    assert res.status == "ok"
    assert len(res.hedge_trades) >= 4  # entry + one per minute (not at off_ts)


def test_hedge_triggers_on_band():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=2)
    # Jump at +1 is INTERIOR to the both-on window [entry, entry+2].
    es = [IntradayBar(entry, 5000.0), IntradayBar(entry + timedelta(minutes=1), 5200.0),
          IntradayBar(entry + timedelta(minutes=2), 5200.0)]
    marks = _bars(entry, [30.0, 30.0, 30.0])
    res = simulate_day(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=marks, put_marks=marks,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=True, interval_minutes=999.0, delta_band=0.20),
    )
    assert res.status == "ok"
    assert len(res.hedge_trades) == 2  # entry + band rehedge at the +200 bar


class _FixedKernel:
    """Pins net delta (call +0.6, put -0.1 => +0.5 long) so hedge P&L decomposes."""

    def implied_vol(self, price, F, K, T, r, flag):
        return 0.2

    def delta(self, F, K, T, r, sigma, flag):
        return 0.6 if flag == "c" else -0.1


def _monotone_es_day(prices: list[float]):
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=len(prices) - 1)
    es = _bars(entry, prices)
    marks = _bars(entry, [30.0] * len(prices))
    return dict(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=marks, put_marks=marks,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=True, interval_minutes=999.0, delta_band=10.0),
        kernel=_FixedKernel(),
    )


def test_hedge_pnl_numerical_sign_and_magnitude():
    up = simulate_day(**_monotone_es_day([5000.0, 5004.0, 5008.0, 5010.0]))
    assert up.status == "ok" and len(up.hedge_trades) == 1
    assert up.hedge_trades[0].hedge_qty == pytest.approx(-0.5)
    assert up.pnl.hedge_pnl_pts == pytest.approx(-5.0)   # -0.5 * (+10)
    assert up.pnl.option_pnl_pts == pytest.approx(0.0)
    assert up.pnl.total_pnl_usd == pytest.approx(-250.0)

    down = simulate_day(**_monotone_es_day([5010.0, 5006.0, 5002.0, 5000.0]))
    assert down.pnl.hedge_pnl_pts == pytest.approx(5.0)  # -0.5 * (-10)


def test_no_hedge_when_disabled():
    res = simulate_day(**_day(hedge=_hspec(enabled=False)))
    assert res.hedge_trades == () and res.pnl.hedge_pnl_pts == 0.0


# --------------------------------------------------------------------------- #
# Both-on hedge window under asymmetric fills
# --------------------------------------------------------------------------- #
def test_hedge_window_starts_at_straddle_on_ts_when_legs_asymmetric():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:20")
    es = _bars(entry, [5000.0] * 25)
    cond = [MaxSpreadCond(pct=5.0, min_ticks=1.0)]
    calls = [_q(entry, 30.0, bid=29.9, ask=30.1, bs=50, as_=50),
             _q(exit_, 30.0, bid=29.9, ask=30.1, bs=50, as_=50)]
    puts = [_q(entry, 30.0),  # fails max_spread
            _q(entry + timedelta(minutes=3), 30.0, bid=29.9, ask=30.1, bs=50, as_=50),
            _q(exit_, 30.0, bid=29.9, ask=30.1, bs=50, as_=50)]
    res = simulate_day(**_day(call_marks=calls, put_marks=puts, es_bars=es,
                              exit_ts=exit_, entry_conditions=cond, exit_conditions=cond,
                              hedge=_hspec(enabled=True, interval_minutes=1.0, delta_band=10.0)))
    assert res.status == "ok"
    assert res.hedge_trades  # hedged
    on_ts = res.straddle_on_ts
    off_ts = res.straddle_off_ts
    assert res.hedge_trades[0].ts == on_ts            # first hedge at both-on start
    assert all(on_ts <= h.ts <= off_ts for h in res.hedge_trades)  # within window


def test_no_hedge_when_both_on_window_empty():
    # Construct straddle_off_ts <= straddle_on_ts: the guard must skip hedging.
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=2)
    es = _bars(entry, [5000.0] * 10)
    # CALL enters at entry, exits at entry+2. PUT's only bar is at entry+5:
    # it becomes BOTH the put entry (earliest >= entry) and put exit (earliest
    # >= exit) => put_entry == put_exit == entry+5. straddle_on = max(entry,
    # entry+5) = entry+5; straddle_off = min(entry+2, entry+5) = entry+2 < on.
    calls = [IntradayBar(entry, 30.0), IntradayBar(exit_, 30.0)]
    puts = [IntradayBar(entry + timedelta(minutes=5), 30.0)]
    res = simulate_day(**_day(call_marks=calls, put_marks=puts, es_bars=es,
                              exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
                              hedge=_hspec(enabled=True, interval_minutes=1.0, delta_band=10.0)))
    assert res.status == "ok"
    assert res.straddle_off_ts <= res.straddle_on_ts
    assert res.hedge_trades == ()          # no hedge over an empty window
    assert res.pnl.hedge_pnl_pts == 0.0


def test_net_delta_atm_near_zero():
    nd = net_straddle_delta(BS76Kernel(), 5000.0, 5000.0, 1.0 / 365.0, 30.0, 30.0, side_sign=1)
    assert abs(nd) < 0.2


# --------------------------------------------------------------------------- #
# Real-kernel delta path (98arch-02): pin _leg_delta to known ATM values with
# the REAL BS76Kernel so a call/put leg-flag swap fails (call ~ +0.5, put ~ -0.5).
# --------------------------------------------------------------------------- #
def test_leg_delta_real_kernel_atm_call_plus_put_minus_half():
    k = BS76Kernel()
    F = K = 5000.0
    T = 1.0 / 365.0
    sigma = 0.20
    cm = k.price_call(F, K, T, 0.0, sigma)  # a genuine invertible ATM mark
    pm = k.price_put(F, K, T, 0.0, sigma)
    dc = _leg_delta(k, F, K, T, cm, "c", 0.0)
    dp = _leg_delta(k, F, K, T, pm, "p", 0.0)
    # Round-trips to the kernel's own delta at the recovered vol.
    assert dc == pytest.approx(k.delta(F, K, T, 0.0, sigma, "c"), abs=1e-6)
    assert dp == pytest.approx(k.delta(F, K, T, 0.0, sigma, "p"), abs=1e-6)
    # ATM: call ~ +0.5, put ~ -0.5 — a leg swap would flip these signs.
    assert 0.45 < dc < 0.55
    assert -0.55 < dp < -0.45
    assert dc > 0.0 > dp


class _RaisingIVKernel(BS76Kernel):
    """Real BS76 delta, but implied_vol always raises — forces the fallback."""

    def implied_vol(self, *args, **kwargs):  # noqa: D401 - test stub
        raise ValueError("forced inversion failure")


def test_leg_delta_fallback_is_smooth_not_step_on_inversion_failure():
    # eng-02: an IV-inversion failure near ATM must yield a SMOOTH Black-76 delta
    # (~+/-0.5), never the old discontinuous 0<->|1| step across the strike.
    k = _RaisingIVKernel()
    F, K, T = 5000.2, 5000.0, 1.0 / 365.0  # a hair ITM for the call (near ATM)
    dc = _leg_delta(k, F, K, T, mark=0.0, flag="c", rate=0.0, fallback_iv=0.20)
    dp = _leg_delta(k, F, K, T, mark=0.0, flag="p", rate=0.0, fallback_iv=0.20)
    # Old step returned call=+1.0, put=0.0 here; the fix returns ~+/-0.5.
    assert 0.4 < dc < 0.6
    assert -0.6 < dp < -0.4
    # Even with NO carried IV the last-resort _FALLBACK_IV keeps it smooth.
    dc_none = _leg_delta(k, F, K, T, mark=0.0, flag="c", rate=0.0, fallback_iv=None)
    assert 0.4 < dc_none < 0.6
    # Net straddle delta near ATM stays ~0 (not the ~+1 the step produced).
    nd = net_straddle_delta(k, F, K, T, 0.0, 0.0, side_sign=1, fallback_iv=0.20)
    assert abs(nd) < 0.2


# --------------------------------------------------------------------------- #
# Tier-aware ES-option tick at the max_spread floor (98iv-01): 0.05 for premium
# <= 5.00, 0.25 above — so a typical (>5.00) ATM leg quoted one true tick wide
# (0.25) is not spuriously rejected, while a cheap (<=5.00) leg still is.
# --------------------------------------------------------------------------- #
def test_max_spread_tick_tier_high_vs_low_premium():
    ts = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    cond = [MaxSpreadCond(pct=0.5, min_ticks=1.0)]
    # >5.00 leg, one true CME tick wide (0.25): floor=max(0.5%*15=0.075, 0.25)=0.25
    # >= spread 0.25 -> PASS (old single-tier 0.05 gave floor 0.075 -> reject).
    high = _q(ts, 15.0, bid=15.0, ask=15.25, bs=50, as_=50)  # spread 0.25
    assert leg_bar_qualifies(high, cond, ES_OPTION_TICK_SIZE) is True
    # <=5.00 leg genuinely ticks at 0.05: a 0.25 spread is 5 ticks -> FAIL.
    cheap = _q(ts, 3.0, bid=2.875, ask=3.125, bs=50, as_=50)  # spread 0.25
    assert leg_bar_qualifies(cheap, cond, ES_OPTION_TICK_SIZE) is False
    # Tier boundary is inclusive at 5.00.
    assert es_option_tick(5.00) == 0.05
    assert es_option_tick(5.01) == 0.25


# --------------------------------------------------------------------------- #
# T2 <= T1 guard
# --------------------------------------------------------------------------- #
def test_exit_not_after_entry_raises():
    kw = _day()
    kw["exit_ts"] = kw["entry_ts"]
    with pytest.raises(ValueError):
        simulate_day(**kw)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_aggregate_counts_and_winrate():
    r1 = simulate_day(**_day())  # flat -> ~0 pnl
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:30")
    r2 = simulate_day(**_day(
        date_int=20250304,
        call_marks=[IntradayBar(entry, 30.0), IntradayBar(exit_, 50.0)],
        put_marks=[IntradayBar(entry, 30.0), IntradayBar(exit_, 30.0)],
    ))
    skipped = simulate_day(**_day(date_int=20250305, call_marks=[], put_marks=[]))
    agg = aggregate_days([r1, r2, skipped])
    assert agg.n_days == 3 and agg.n_traded == 2 and agg.n_skipped == 1
    assert 0.0 <= agg.win_rate <= 1.0
    assert len(agg.equity_curve) == 2


# --------------------------------------------------------------------------- #
# v3 — Early-exit TRIGGERS
# --------------------------------------------------------------------------- #
class _PinnedVolKernel:
    """IV pinned to 0.2 (so IV_entry is deterministic); delta 0 (net_delta
    inert) — isolates the sigma_move path."""

    def implied_vol(self, price, F, K, T, r, flag):
        return 0.2

    def delta(self, F, K, T, r, sigma, flag):
        return 0.0


class _MoneynessKernel:
    """Delta rises linearly with (F-K): long-straddle net delta ~ 2*(F-K)/50."""

    def implied_vol(self, price, F, K, T, r, flag):
        return 0.2

    def delta(self, F, K, T, r, sigma, flag):
        m = (F - K) / 50.0
        d = max(-1.0, min(1.0, 0.5 + m))
        return d if flag == "c" else d - 1.0


def test_trigger_underlying_move_points_closes_early():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    # Flat 5000 then a +20 jump at minute 5 (>= 15pt threshold).
    es = _bars(entry, [5000.0] * 5 + [5020.0] * 26)
    res = simulate_day(**_day(
        es_bars=es,
        exit_triggers=[UnderlyingMoveTrigger(amount=15.0, unit="points")],
    ))
    assert res.status == "ok"
    assert res.exit_trigger is not None
    assert res.exit_trigger.type == "underlying_move"
    assert res.exit_trigger.ts == entry + timedelta(minutes=5)
    assert res.exit_trigger.value == pytest.approx(20.0)
    # Whole straddle closed at the trigger bar (nearest per leg).
    assert res.straddle_off_ts == entry + timedelta(minutes=5)
    assert res.legs.call.exit_ts == entry + timedelta(minutes=5)
    assert res.legs.put.exit_ts == entry + timedelta(minutes=5)


def test_trigger_underlying_move_percent_unit():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    # 0.3% of 5000 = 15pt; a +20 jump at minute 4 fires. value in percent.
    es = _bars(entry, [5000.0] * 4 + [5020.0] * 27)
    res = simulate_day(**_day(
        es_bars=es,
        exit_triggers=[UnderlyingMoveTrigger(amount=0.3, unit="percent")],
    ))
    assert res.exit_trigger is not None
    assert res.exit_trigger.ts == entry + timedelta(minutes=4)
    assert res.exit_trigger.value == pytest.approx(20.0 / 5000.0 * 100.0)


def test_trigger_sigma_move_shrinks_intraday():
    # 0DTE: sigma_bar = 5000*0.2*sqrt(T_bar) shrinks toward the 16:00 expiry.
    # A fixed +15 move is BELOW sigma early (~26pt) and only fires once sigma
    # has decayed below it later in the session.
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "15:45")
    n = int((exit_ - entry).total_seconds() // 60) + 1
    es = _bars(entry, [5000.0] + [5015.0] * (n - 1))  # +15 from minute 1
    marks = _bars(entry, [30.0] * n)
    kw = dict(
        date_int=20250303, side="long", strike=5000.0, expiry=day,  # 0DTE
        es_bars=es, call_marks=marks, put_marks=marks,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=False),
        kernel=_PinnedVolKernel(),
        exit_triggers=[SigmaMoveTrigger(n=1.0)],
    )
    res = simulate_day(**kw)
    assert res.exit_trigger is not None and res.exit_trigger.type == "sigma_move"
    # Did NOT fire at minute 1 (sigma still > 15); fired only after it shrank.
    assert res.exit_trigger.ts > entry + timedelta(minutes=30)


def test_trigger_sigma_move_big_move_fires_immediately():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "15:45")
    n = int((exit_ - entry).total_seconds() // 60) + 1
    es = _bars(entry, [5000.0] + [5200.0] * (n - 1))  # +200 >> sigma_entry
    marks = _bars(entry, [30.0] * n)
    res = simulate_day(
        date_int=20250303, side="long", strike=5000.0, expiry=day,
        es_bars=es, call_marks=marks, put_marks=marks,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=False),
        kernel=_PinnedVolKernel(), exit_triggers=[SigmaMoveTrigger(n=1.0)],
    )
    assert res.exit_trigger is not None
    assert res.exit_trigger.ts == entry + timedelta(minutes=1)


def test_trigger_net_delta_fires_when_delta_grows():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    # ES steps +5/min: net delta ~2*(F-5000)/50 crosses 0.3 at F=5010 (minute 2).
    es = _bars(entry, [5000.0 + 5.0 * i for i in range(31)])
    marks = _bars(entry, [30.0] * 31)
    res = simulate_day(**_day(
        es_bars=es, call_marks=marks, put_marks=marks,
        kernel=_MoneynessKernel(),
        exit_triggers=[NetDeltaTrigger(threshold=0.3)],
    ))
    assert res.exit_trigger is not None and res.exit_trigger.type == "net_delta"
    assert res.exit_trigger.ts == entry + timedelta(minutes=2)
    assert res.exit_trigger.value == pytest.approx(0.4)


def _pnl_base():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:20")
    n = 21
    es = _bars(entry, [5000.0] * n)
    put = _bars(entry, [30.0] * n)  # P_entry = 60
    base = dict(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, put_marks=put, entry_ts=entry, exit_ts=exit_,
        entry_tol=10.0, exit_tol=10.0, hedge=_hspec(enabled=False),
    )
    return base, entry, n


def test_trigger_pnl_profit_vs_loss_vs_both():
    base, entry, n = _pnl_base()
    up = _bars(entry, [30.0] * 3 + [50.0] * (n - 3))    # straddle +20 => +1000usd
    dn = _bars(entry, [30.0] * 3 + [10.0] * (n - 3))    # straddle -20 => -1000usd

    # profit: fires on the up move at minute 3, value = +1000 usd.
    r = simulate_day(call_marks=up, exit_triggers=[
        PnlTrigger(amount=500, unit="usd", direction="profit")], **base)
    assert r.exit_trigger is not None and r.exit_trigger.type == "pnl"
    assert r.exit_trigger.ts == entry + timedelta(minutes=3)
    assert r.exit_trigger.value == pytest.approx(1000.0)

    # profit direction never fires on an up move's loss trigger -> time exit.
    r = simulate_day(call_marks=up, exit_triggers=[
        PnlTrigger(amount=500, unit="usd", direction="loss")], **base)
    assert r.exit_trigger is None

    # loss: fires on the down move, value = -1000 usd (signed).
    r = simulate_day(call_marks=dn, exit_triggers=[
        PnlTrigger(amount=500, unit="usd", direction="loss")], **base)
    assert r.exit_trigger is not None
    assert r.exit_trigger.value == pytest.approx(-1000.0)

    # both: fires on the down move too.
    r = simulate_day(call_marks=dn, exit_triggers=[
        PnlTrigger(amount=500, unit="usd", direction="both")], **base)
    assert r.exit_trigger is not None
    assert r.exit_trigger.ts == entry + timedelta(minutes=3)


def test_trigger_pnl_percent_and_points_units():
    base, entry, n = _pnl_base()
    up = _bars(entry, [30.0] * 3 + [50.0] * (n - 3))  # +20 pts on 60 premium
    # points: +20 >= 15 fires; value in points.
    r = simulate_day(call_marks=up, exit_triggers=[
        PnlTrigger(amount=15, unit="points", direction="profit")], **base)
    assert r.exit_trigger is not None
    assert r.exit_trigger.value == pytest.approx(20.0)
    # percent of P_entry (60): 20/60*100 = 33.3% >= 30% fires.
    r = simulate_day(call_marks=up, exit_triggers=[
        PnlTrigger(amount=30, unit="percent", direction="profit")], **base)
    assert r.exit_trigger is not None
    assert r.exit_trigger.value == pytest.approx(20.0 / 60.0 * 100.0)


def test_first_of_several_triggers_wins_earliest_bar():
    # pnl fires at minute 3; underlying_move at minute 5 -> earliest (pnl) wins.
    base, entry, n = _pnl_base()
    up = _bars(entry, [30.0] * 3 + [50.0] * (n - 3))
    es = _bars(entry, [5000.0] * 5 + [5030.0] * (n - 5))  # +30 move at minute 5
    base = dict(base)
    base["es_bars"] = es
    r = simulate_day(call_marks=up, exit_triggers=[
        UnderlyingMoveTrigger(amount=15.0, unit="points"),
        PnlTrigger(amount=500, unit="usd", direction="profit"),
    ], **base)
    assert r.exit_trigger is not None and r.exit_trigger.type == "pnl"
    assert r.exit_trigger.ts == entry + timedelta(minutes=3)


def test_same_bar_tie_resolves_by_list_order():
    # Both fire at minute 3; list order decides: underlying_move listed first.
    base, entry, n = _pnl_base()
    up = _bars(entry, [30.0] * 3 + [50.0] * (n - 3))       # pnl at minute 3
    es = _bars(entry, [5000.0] * 3 + [5030.0] * (n - 3))   # +30 move at minute 3
    base = dict(base)
    base["es_bars"] = es
    r = simulate_day(call_marks=up, exit_triggers=[
        UnderlyingMoveTrigger(amount=15.0, unit="points"),
        PnlTrigger(amount=500, unit="usd", direction="profit"),
    ], **base)
    assert r.exit_trigger is not None and r.exit_trigger.type == "underlying_move"
    assert r.exit_trigger.ts == entry + timedelta(minutes=3)


def test_triggered_exit_bypasses_exit_conditions():
    # Impossible exit condition would force a not-met fallback on a time exit;
    # a TRIGGERED exit ignores conditions and fills cleanly at the trigger bar.
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    es = _bars(entry, [5000.0] * 5 + [5020.0] * 26)
    res = simulate_day(**_day(
        es_bars=es,
        exit_conditions=[MinPremiumCond(points=999.0)],
        exit_triggers=[UnderlyingMoveTrigger(amount=15.0, unit="points")],
    ))
    assert res.status == "ok"
    assert res.exit_trigger is not None
    assert res.straddle_off_ts == entry + timedelta(minutes=5)
    # Bypassed conditions -> clean fill within tolerance, not a degraded fallback.
    assert res.legs.call.exit_conditions_met is True
    assert res.legs.put.exit_conditions_met is True


def test_no_trigger_fire_falls_back_to_time_exit_unchanged():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:30")
    es = _bars(entry, [5000.0] * 5 + [5020.0] * 26)
    calls = _bars(entry, [30.0] * 31)
    puts = _bars(entry, [30.0] * 31)
    common = dict(
        es_bars=es, call_marks=calls, put_marks=puts,
        hedge=_hspec(enabled=True, interval_minutes=5.0, delta_band=0.2),
    )
    # A never-firing trigger must produce the SAME result as no triggers at all.
    baseline = simulate_day(**_day(**common))
    with_trig = simulate_day(**_day(
        exit_triggers=[UnderlyingMoveTrigger(amount=99999.0, unit="points")],
        **common,
    ))
    assert with_trig.exit_trigger is None
    assert with_trig.straddle_off_ts == baseline.straddle_off_ts == exit_
    assert with_trig.pnl.total_pnl_usd == pytest.approx(baseline.pnl.total_pnl_usd)


def test_no_triggers_field_is_v2_time_exit():
    # The v2 path (no exit_triggers arg) is unchanged: exit at exit.time.
    res = simulate_day(**_day())
    assert res.exit_trigger is None
    assert res.status == "ok"


# --------------------------------------------------------------------------- #
# v4 — Hedge as a configurable module (triggers-OR / conditions-AND / target)
# --------------------------------------------------------------------------- #
def _hedge_day(es_bars, hedge, kernel=None, minutes=4):
    """A day whose both-on window spans ``minutes`` with dense option marks."""
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=minutes)
    n = minutes + 1
    marks = _bars(entry, [30.0] * n)
    return dict(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es_bars, call_marks=marks, put_marks=marks,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=hedge, kernel=kernel or _MoneynessKernel(),
    ), entry


def test_hedge_target_zero_band_edge_ratio():
    # _FixedKernel pins net delta to +0.5 (constant), so target math is exact.
    es = _bars(resolve_et_to_utc(date(2025, 3, 3), "10:00"), [5000.0] * 5)
    kw_z, _ = _hedge_day(es, _hspec(interval_minutes=999.0, delta_band=10.0,
                                    mode="zero"), kernel=_FixedKernel())
    z = simulate_day(**kw_z)
    assert z.hedge_trades[0].hedge_qty == pytest.approx(-0.5)      # -net_delta

    kw_r, _ = _hedge_day(es, _hspec(interval_minutes=999.0, delta_band=10.0,
                                    mode="ratio", ratio=0.5), kernel=_FixedKernel())
    r = simulate_day(**kw_r)
    assert r.hedge_trades[0].hedge_qty == pytest.approx(-0.25)     # -ratio*net_delta

    kw_b, _ = _hedge_day(es, _hspec(interval_minutes=999.0, delta_band=0.2,
                                    mode="band_edge"), kernel=_FixedKernel())
    b = simulate_day(**kw_b)
    ht = b.hedge_trades[0]
    assert ht.hedge_qty == pytest.approx(-0.3)                     # -0.5 + sign*0.2
    # residual delta left on the book == +band (sign of net_delta).
    assert ht.net_delta + ht.hedge_qty == pytest.approx(0.2)


def _es_quoted_ramp(entry, half_spread, size, n=5, step=25.0):
    """ES bars ramping +step/min, each two-sided with the given half-spread/size."""
    return [
        _esq(entry + timedelta(minutes=i), 5000.0 + step * i,
             bid=5000.0 + step * i - half_spread, ask=5000.0 + step * i + half_spread,
             bs=size, as_=size)
        for i in range(n)
    ]


def test_hedge_condition_max_spread_defers_on_es_bar():
    entry = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    cond = (MaxSpreadCond(pct=0.001, min_ticks=1.0),)  # floor = max(~0.05, 0.25)=0.25
    hspec = _hspec(interval_minutes=1.0, delta_band=10.0, conditions=cond)
    # Tight ES quote (spread 0.2 <= 0.25) -> every interior rehedge EXECUTES.
    kw_t, _ = _hedge_day(_es_quoted_ramp(entry, 0.1, 50), hspec)
    tight = simulate_day(**kw_t)
    # Wide ES quote (spread 2.0 > 0.25) -> every interior rehedge DEFERS.
    kw_w, _ = _hedge_day(_es_quoted_ramp(entry, 1.0, 50), hspec)
    wide = simulate_day(**kw_w)
    assert len(tight.hedge_trades) == 4   # entry + 3 interior (minutes 1,2,3)
    assert len(wide.hedge_trades) == 1    # only the unconditional entry hedge


def test_hedge_condition_min_quote_size_defers_on_es_bar():
    entry = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    cond = (MinQuoteSizeCond(size=10),)
    hspec = _hspec(interval_minutes=1.0, delta_band=10.0, conditions=cond)
    kw_big, _ = _hedge_day(_es_quoted_ramp(entry, 0.1, 50), hspec)   # sizes 50 >= 10
    kw_small, _ = _hedge_day(_es_quoted_ramp(entry, 0.1, 5), hspec)  # sizes 5 < 10
    assert len(simulate_day(**kw_big).hedge_trades) == 4
    assert len(simulate_day(**kw_small).hedge_trades) == 1


def test_hedge_condition_min_rehedge_delta_defers():
    entry = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    # +5/min keeps _MoneynessKernel net delta UNSATURATED so it changes each bar
    # (|delta to remove| ~0.2/min) — isolating the threshold gate, not saturation.
    es = _bars(entry, [5000.0 + 5.0 * i for i in range(5)])  # no ES quotes needed
    # threshold 5.0 >> any |delta to remove| (<=~1) -> all interior rehedges DEFER.
    kw_defer, _ = _hedge_day(es, _hspec(interval_minutes=1.0, delta_band=10.0,
                                        conditions=(MinRehedgeDeltaCond(threshold=5.0),)))
    # threshold 0.01 -> interior rehedges EXECUTE (delta actually moves).
    kw_exec, _ = _hedge_day(es, _hspec(interval_minutes=1.0, delta_band=10.0,
                                       conditions=(MinRehedgeDeltaCond(threshold=0.01),)))
    assert len(simulate_day(**kw_defer).hedge_trades) == 1
    assert len(simulate_day(**kw_exec).hedge_trades) == 4


def test_hedge_sigma_move_trigger_waits_for_one_sigma():
    # Only sigma_move enabled (interval + band OFF): "wait for 1 sigma before
    # rehedging". _FixedKernel pins net delta (+0.5) and IV (0.2) so sigma is
    # deterministic. sigma_bar ~ 5000*0.2*sqrt(T~30h) ~ 58 pts; a +70 jump at
    # minute 4 is the FIRST move that clears 1 sigma from the entry hedge.
    entry = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    es = _bars(entry, [5000.0] * 4 + [5070.0] * 3)  # flat, then +70 at minute 4
    kw, _ = _hedge_day(
        es,
        _hspec(interval_minutes=None, delta_band=None, sigma_enabled=True, sigma_n=1.0),
        kernel=_FixedKernel(), minutes=6,
    )
    res = simulate_day(**kw)
    assert res.status == "ok"
    # entry hedge + exactly ONE sigma-triggered rehedge at the +70 bar.
    assert len(res.hedge_trades) == 2
    assert res.hedge_trades[1].ts == entry + timedelta(minutes=4)
    assert res.hedge_trades[1].underlying == pytest.approx(5070.0)


def test_hedge_disabled_via_spec_no_trades():
    res = simulate_day(**_day(hedge=_hspec(enabled=False, interval_minutes=1.0)))
    assert res.hedge_trades == () and res.pnl.hedge_pnl_pts == 0.0


def test_hedge_default_none_is_disabled():
    # Omitting the hedge kwarg entirely -> disabled (no hedge, hedge_pnl 0).
    kw = _day()
    kw.pop("hedge")
    res = simulate_day(**kw)
    assert res.hedge_trades == () and res.pnl.hedge_pnl_pts == 0.0
    assert res.status == "ok"


# --------------------------------------------------------------------------- #
# P0.2 — half-spread transaction-cost model (adverse crossing from bbba)
# --------------------------------------------------------------------------- #
from tcg.engine.intraday_backtest import crossing_fill_price  # noqa: E402
from tcg.types.intraday import COST_DISABLED, CostModel  # noqa: E402


def test_crossing_fill_price_buy_pays_half_sell_receives_half():
    # A BUY crosses UP (mid + half); a SELL crosses DOWN (mid - half).
    assert crossing_fill_price(30.0, 0.4, is_buy=True) == pytest.approx(30.4)
    assert crossing_fill_price(30.0, 0.4, is_buy=False) == pytest.approx(29.6)
    # Zero half-spread => the mid, either direction (no cost).
    assert crossing_fill_price(30.0, 0.0, is_buy=True) == 30.0
    assert crossing_fill_price(30.0, 0.0, is_buy=False) == 30.0


def _cost_option_day(*, side="long", cost=None):
    """A no-hedge day with EXPLICIT two-sided entry/exit option quotes so the
    half-spread per fill is known: call 30->50, put 30->5."""
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:05")
    es = _bars(entry, [5000.0] * 6)
    # call: entry mid 30 (half 0.5), exit mid 50 (half 0.4)
    calls = [_q(entry, 30.0, bid=29.5, ask=30.5, bs=10, as_=10),
             _q(exit_, 50.0, bid=49.6, ask=50.4, bs=10, as_=10)]
    # put: entry mid 30 (half 0.2), exit mid 5 (half 0.1)
    puts = [_q(entry, 30.0, bid=29.8, ask=30.2, bs=10, as_=10),
            _q(exit_, 5.0, bid=4.9, ask=5.1, bs=10, as_=10)]
    kw = dict(
        date_int=20250303, side=side, strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=calls, put_marks=puts,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=False),
    )
    if cost is not None:
        kw["cost"] = cost
    return kw


def test_cost_off_reproduces_mid_fill_pnl():
    # Cost OFF (default AND explicit disabled) must reproduce the mid-fill P&L
    # bit-for-bit — the regression guard for the 113 baseline.
    base = simulate_day(**_cost_option_day())  # no cost kwarg
    off = simulate_day(**_cost_option_day(cost=COST_DISABLED))
    assert off.pnl.option_pnl_pts == base.pnl.option_pnl_pts == pytest.approx(-5.0)
    assert off.pnl.total_pnl_pts == base.pnl.total_pnl_pts == pytest.approx(-5.0)
    assert off.pnl.total_pnl_usd == base.pnl.total_pnl_usd == pytest.approx(-250.0)
    assert off.pnl.cost_pts == 0.0 and off.pnl.n_fallback_fills == 0


def test_long_straddle_roundtrip_cost_is_sum_of_four_half_spreads():
    res = simulate_day(**_cost_option_day(cost=CostModel(enabled=True)))
    # Gross option P&L unchanged (mid marks): call +20, put -25 => -5.
    assert res.pnl.option_pnl_pts == pytest.approx(-5.0)
    # Cost = call_entry 0.5 + call_exit 0.4 + put_entry 0.2 + put_exit 0.1 = 1.2.
    assert res.pnl.cost_pts == pytest.approx(1.2)
    assert res.pnl.n_fallback_fills == 0
    # Net P&L is reduced by the cost; USD nets too.
    assert res.pnl.total_pnl_pts == pytest.approx(-6.2)
    assert res.pnl.total_pnl_usd == pytest.approx(-310.0)
    assert res.pnl.cost_usd == pytest.approx(60.0)


def test_short_straddle_cost_is_symmetric_and_reduces_pnl():
    res = simulate_day(**_cost_option_day(side="short", cost=CostModel(enabled=True)))
    # Gross short option P&L = +5 (straddle 60 -> 55). Same 1.2 cost still REDUCES.
    assert res.pnl.option_pnl_pts == pytest.approx(5.0)
    assert res.pnl.cost_pts == pytest.approx(1.2)
    assert res.pnl.total_pnl_pts == pytest.approx(3.8)
    assert res.pnl.n_fallback_fills == 0


def test_one_sided_bar_uses_fixed_fallback_and_counts_it():
    # Quote-less (last-trade-only) option bars: half-spread undefined => the fixed
    # per-side fallback (0.3 pts) is charged on each of the 4 fills and counted.
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:05")
    es = _bars(entry, [5000.0] * 6)
    calls = [IntradayBar(entry, 30.0), IntradayBar(exit_, 50.0)]  # no bid/ask
    puts = [IntradayBar(entry, 30.0), IntradayBar(exit_, 5.0)]
    res = simulate_day(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=calls, put_marks=puts,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=False),
        cost=CostModel(enabled=True, fallback_cost_pts=0.3),
    )
    assert res.pnl.option_pnl_pts == pytest.approx(-5.0)  # gross unchanged
    assert res.pnl.n_fallback_fills == 4                  # all 4 fills fell back
    assert res.pnl.cost_pts == pytest.approx(1.2)         # 4 * 0.3
    assert res.pnl.total_pnl_pts == pytest.approx(-6.2)


def test_fallback_zero_default_costs_nothing_but_still_counts():
    # fallback default 0.0: quote-less fills add no cost, but coverage is still
    # measurable (the fills are counted).
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:05")
    es = _bars(entry, [5000.0] * 6)
    calls = [IntradayBar(entry, 30.0), IntradayBar(exit_, 50.0)]
    puts = [IntradayBar(entry, 30.0), IntradayBar(exit_, 5.0)]
    res = simulate_day(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=calls, put_marks=puts,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=False),
        cost=CostModel(enabled=True),  # fallback_cost_pts=0.0
    )
    assert res.pnl.cost_pts == 0.0
    assert res.pnl.n_fallback_fills == 4
    assert res.pnl.total_pnl_pts == pytest.approx(-5.0)


def _cost_hedge_day():
    """A hedged day with TWO-SIDED ES quotes (half_es=0.25) and zero-spread
    option quotes (so option cost is 0), real kernel + moving ES so several
    rehedges re-trade the ES position."""
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    prices = [5000.0, 5010.0, 5020.0, 5030.0, 5040.0]
    exit_ = entry + timedelta(minutes=len(prices) - 1)
    es = [_esq(entry + timedelta(minutes=i), p, bid=p - 0.25, ask=p + 0.25, bs=50, as_=50)
          for i, p in enumerate(prices)]
    # Option marks: two-sided but ZERO spread (half 0) at entry and exit bars so
    # the option leg contributes no cost and no fallback.
    n = len(prices)
    calls = [_q(entry + timedelta(minutes=i), 30.0, bid=30.0, ask=30.0, bs=10, as_=10)
             for i in range(n)]
    puts = [_q(entry + timedelta(minutes=i), 30.0, bid=30.0, ask=30.0, bs=10, as_=10)
            for i in range(n)]
    return dict(
        date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=calls, put_marks=puts,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=_hspec(enabled=True, interval_minutes=1.0, delta_band=1e9),
    )


def test_hedge_cost_accrues_per_rehedge_at_es_half_spread():
    kw = _cost_hedge_day()
    off = simulate_day(**kw)
    on = simulate_day(**kw, cost=CostModel(enabled=True))
    assert len(on.hedge_trades) >= 2  # entry + >=1 interior rehedge
    # Reconstruct the expected hedge cost: 0.25 (half_es) * sum of |trade size|,
    # trade size = change in ES position at each executed rehedge (from 0).
    prev = 0.0
    sum_abs_dq = 0.0
    for h in on.hedge_trades:
        sum_abs_dq += abs(h.hedge_qty - prev)
        prev = h.hedge_qty
    assert on.pnl.cost_pts == pytest.approx(0.25 * sum_abs_dq)
    assert on.pnl.n_fallback_fills == 0        # every ES bar is two-sided
    # Cost strictly reduces P&L vs the cost-off run; gross legs are unchanged.
    assert on.pnl.hedge_pnl_pts == pytest.approx(off.pnl.hedge_pnl_pts)
    assert on.pnl.total_pnl_pts == pytest.approx(
        off.pnl.total_pnl_pts - on.pnl.cost_pts
    )


def test_aggregate_sums_cost_and_fallback_across_days():
    d1 = simulate_day(**_cost_option_day(cost=CostModel(enabled=True)))
    d2 = simulate_day(
        date_int=20250304, side="long", strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=_bars(resolve_et_to_utc(date(2025, 3, 4), "10:00"), [5000.0] * 6),
        call_marks=[IntradayBar(resolve_et_to_utc(date(2025, 3, 4), "10:00"), 30.0),
                    IntradayBar(resolve_et_to_utc(date(2025, 3, 4), "10:05"), 50.0)],
        put_marks=[IntradayBar(resolve_et_to_utc(date(2025, 3, 4), "10:00"), 30.0),
                   IntradayBar(resolve_et_to_utc(date(2025, 3, 4), "10:05"), 5.0)],
        entry_ts=resolve_et_to_utc(date(2025, 3, 4), "10:00"),
        exit_ts=resolve_et_to_utc(date(2025, 3, 4), "10:05"),
        entry_tol=10.0, exit_tol=10.0, hedge=_hspec(enabled=False),
        cost=CostModel(enabled=True, fallback_cost_pts=0.3),
    )
    agg = aggregate_days([d1, d2])
    # d1: cost 1.2 pts, 0 fallback; d2: cost 1.2 pts (4*0.3), 4 fallback.
    assert agg.total_cost_usd == pytest.approx((1.2 + 1.2) * 50.0)
    assert agg.n_fallback_fills == 4


# --------------------------------------------------------------------------- #
# W2/P1 — Hedge-timing gates: F1.1 (time-anchored) + F1.2 (skip-near-extremum)
# --------------------------------------------------------------------------- #
class _LinearDeltaKernel:
    """Per-leg delta tracks ES linearly so the net straddle delta — and hence the
    hedge trade DIRECTION — is fully controllable: call & put delta both
    ``(F-5000)/1000`` => ``dc+dp = (F-5000)/500``. For a SHORT straddle
    (side_sign=-1) net delta is ``-(F-5000)/500``: ES ABOVE 5000 => net delta < 0
    => hedge BUYS ES (into the high); ES BELOW 5000 => hedge SELLS ES (into the
    low). That lets a rising-ES day exercise the "BUY near the running high" skip
    and a falling-ES day the symmetric "SELL near the running low" skip."""

    def implied_vol(self, price, F, K, T, r, flag):
        return 0.2

    def delta(self, F, K, T, r, sigma, flag):
        return (F - 5000.0) / 1000.0


def _short_day(prices, *, hedge, side="short"):
    """A SHORT straddle over 10:00..10:00+N-1 with the linear-delta kernel."""
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=len(prices) - 1)
    es = _bars(entry, prices)
    marks = _bars(entry, [30.0] * len(prices))
    return dict(
        date_int=20250303, side=side, strike=5000.0, expiry=date(2025, 3, 4),
        es_bars=es, call_marks=marks, put_marks=marks,
        entry_ts=entry, exit_ts=exit_, entry_tol=10.0, exit_tol=10.0,
        hedge=hedge, kernel=_LinearDeltaKernel(),
    )


# --- pure gate helpers ------------------------------------------------------ #
def test_hedge_time_gate_ok_unit():
    base = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    close = base + timedelta(minutes=30)
    assert _hedge_time_gate_ok(HedgeTimingSpec(), base, close) is True  # None => on
    on = HedgeTimingSpec(only_within_minutes_before_close=10.0)
    assert _hedge_time_gate_ok(on, base, close) is False               # 30 min out
    assert _hedge_time_gate_ok(on, close - timedelta(minutes=10), close) is True
    assert _hedge_time_gate_ok(on, close - timedelta(minutes=5), close) is True


def test_skip_near_extremum_unit():
    base = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    close = base + timedelta(minutes=30)
    ts = close - timedelta(minutes=5)  # inside a 10-min late window
    spec = HedgeTimingSpec(skip_near_extremum=SkipNearExtremumSpec(
        enabled=True, window_minutes=10.0, tolerance=2.0))
    kw = dict(ts=ts, close_ts=close, running_high=5100.0, running_low=5000.0)
    # BUY within tolerance of the running high -> suppress; far from it -> allow.
    assert _skip_near_extremum(spec, es_price=5099.0, delta_change=+0.1, **kw) is True
    assert _skip_near_extremum(spec, es_price=5090.0, delta_change=+0.1, **kw) is False
    # SELL within tolerance of the running low -> suppress; near the high -> allow.
    assert _skip_near_extremum(spec, es_price=5001.0, delta_change=-0.1, **kw) is True
    assert _skip_near_extremum(spec, es_price=5099.0, delta_change=-0.1, **kw) is False
    # Zero-size trade has no direction -> never suppressed.
    assert _skip_near_extremum(spec, es_price=5100.0, delta_change=0.0, **kw) is False
    # Outside the late window -> inert.
    early = dict(kw, ts=close - timedelta(minutes=20))
    assert _skip_near_extremum(spec, es_price=5099.0, delta_change=+0.1, **early) is False
    # Disabled spec -> never suppress.
    assert _skip_near_extremum(HedgeTimingSpec(), es_price=5099.0,
                               delta_change=+0.1, **kw) is False
    # Missing session context -> fail OPEN (do not block the hedge).
    nohl = dict(kw, running_high=None, running_low=None)
    assert _skip_near_extremum(spec, es_price=5099.0, delta_change=+0.1, **nohl) is False
    # percent tolerance: 0.1% of 5099 ~= 5.1 pts; high-es = 1 <= 5.1 -> suppress.
    pct = HedgeTimingSpec(skip_near_extremum=SkipNearExtremumSpec(
        enabled=True, window_minutes=10.0, tolerance=0.1, tolerance_unit="percent"))
    assert _skip_near_extremum(pct, es_price=5099.0, delta_change=+0.1, **kw) is True


def test_running_extremum_no_lookahead():
    base = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    es = _bars(base, [5000.0, 5010.0, 5005.0, 5200.0, 4900.0])
    # Up to minute 1: high=5010, low=5000 — the LATER 5200/4900 are excluded.
    assert _running_extremum(es, base + timedelta(minutes=1)) == (5010.0, 5000.0)
    # Up to minute 3: the 5200 spike is now in the past+present; 4900 still future.
    assert _running_extremum(es, base + timedelta(minutes=3)) == (5200.0, 5000.0)
    # Before the first bar -> no context.
    assert _running_extremum(es, base - timedelta(minutes=1)) == (None, None)


# --- F1.1 time-anchored window --------------------------------------------- #
def test_f11_restricts_hedging_to_final_window():
    entry = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    prices = [5000.0] * 31  # flat, 10:00..10:30 (off_ts = 10:30)
    marks = _bars(entry, [30.0] * 31)
    kw = dict(date_int=20250303, side="long", strike=5000.0, expiry=date(2025, 3, 4),
              es_bars=_bars(entry, prices), call_marks=marks, put_marks=marks,
              entry_ts=entry, exit_ts=entry + timedelta(minutes=30),
              entry_tol=10.0, exit_tol=10.0)
    base = simulate_day(**kw, hedge=_hspec(interval_minutes=1.0, delta_band=None))
    assert base.hedge_trades[0].ts == entry  # ungated: entry hedge at 10:00

    gated = simulate_day(**kw, hedge=_hspec(
        interval_minutes=1.0, delta_band=None,
        timing=HedgeTimingSpec(only_within_minutes_before_close=10.0)))
    open_ts = gated.straddle_off_ts - timedelta(minutes=10)  # 10:20
    assert gated.hedge_trades  # some fire inside the final-10-min window
    assert all(h.ts >= open_ts for h in gated.hedge_trades)   # nothing earlier
    assert gated.hedge_trades[0].ts > entry                   # entry hedge suppressed
    assert len(gated.hedge_trades) < len(base.hedge_trades)   # strictly fewer


def test_f11_unset_matches_baseline():
    entry = resolve_et_to_utc(date(2025, 3, 3), "10:00")
    prices = [5000.0, 5010.0, 5020.0, 5015.0, 5030.0, 5025.0]
    kw = _short_day(prices, hedge=_hspec(interval_minutes=1.0, delta_band=0.005))
    plain = simulate_day(**kw)
    # An EXPLICIT default (all-neutral) timing must reproduce the result exactly.
    kw2 = _short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=0.005, timing=HedgeTimingSpec()))
    explicit = simulate_day(**kw2)
    assert explicit.hedge_trades == plain.hedge_trades
    assert explicit.pnl.total_pnl_pts == pytest.approx(plain.pnl.total_pnl_pts)


# --- F1.2 skip-near-extremum ----------------------------------------------- #
def _rising():  # strictly rising ES 10:00..10:20 => every bar is a new high
    return [5010.0 + 10.0 * i for i in range(21)]


def _falling():  # strictly falling ES 10:00..10:20 => every bar is a new low
    return [5210.0 - 10.0 * i for i in range(21)]


def test_f12_suppresses_buy_near_running_high():
    prices = _rising()
    late_open = resolve_et_to_utc(date(2025, 3, 3), "10:10")  # off(10:20) - 10min
    # OFF: BUY hedges fire throughout, including the final 10 min.
    off = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None)))
    assert any(h.ts >= late_open for h in off.hedge_trades)
    # ON: every late bar's ES == the running high (strictly rising), so each
    # BUY-near-high is suppressed. NB monotone-rising also guards no-look-ahead:
    # a peek at the (higher) future would put ES *below* the high and NOT suppress.
    on = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None,
        timing=HedgeTimingSpec(skip_near_extremum=SkipNearExtremumSpec(
            enabled=True, window_minutes=10.0, tolerance=0.0)))))
    assert on.hedge_trades  # early (pre-window) BUYs still fire
    assert all(h.ts < late_open for h in on.hedge_trades)  # none in the late window
    # Each executed trade RAISED the ES position (a BUY): resulting qty is strictly
    # increasing across the surviving trades.
    q = [h.hedge_qty for h in on.hedge_trades]
    assert all(b > a for a, b in zip(q, q[1:]))


def test_f12_suppresses_sell_near_running_low():
    prices = _falling()
    late_open = resolve_et_to_utc(date(2025, 3, 3), "10:10")
    off = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None)))
    assert any(h.ts >= late_open for h in off.hedge_trades)
    on = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None,
        timing=HedgeTimingSpec(skip_near_extremum=SkipNearExtremumSpec(
            enabled=True, window_minutes=10.0, tolerance=0.0)))))
    assert on.hedge_trades
    assert all(h.ts < late_open for h in on.hedge_trades)   # late SELLs suppressed
    # Each executed trade LOWERED the ES position (a SELL): resulting qty is
    # strictly decreasing across the surviving trades.
    q = [h.hedge_qty for h in on.hedge_trades]
    assert all(b < a for a, b in zip(q, q[1:]))


def test_f12_not_suppressed_when_outside_tolerance():
    late_open = resolve_et_to_utc(date(2025, 3, 3), "10:10")
    # High of 5300 is set early (idx3); the late window rises 5110->5210, staying
    # far below that running high, so late BUYs are NOT near it.
    prices = ([5010.0, 5100.0, 5200.0, 5300.0, 5250.0, 5200.0,
               5150.0, 5120.0, 5110.0, 5105.0]
              + [5110.0 + 10.0 * i for i in range(11)])  # 21 bars total
    tight = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None,
        timing=HedgeTimingSpec(skip_near_extremum=SkipNearExtremumSpec(
            enabled=True, window_minutes=10.0, tolerance=2.0)))))
    # ES sits >=90 pts below the 5300 running high -> BUYs still fire late.
    assert any(h.ts >= late_open for h in tight.hedge_trades)
    # A huge tolerance re-captures those same BUYs as "near the high" -> suppressed.
    wide = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None,
        timing=HedgeTimingSpec(skip_near_extremum=SkipNearExtremumSpec(
            enabled=True, window_minutes=10.0, tolerance=1000.0)))))
    assert all(h.ts < late_open for h in wide.hedge_trades)


def test_f12_unset_matches_baseline():
    prices = _rising()
    plain = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None)))
    disabled = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None,
        timing=HedgeTimingSpec(skip_near_extremum=SkipNearExtremumSpec(
            enabled=False, window_minutes=10.0, tolerance=0.0)))))
    assert disabled.hedge_trades == plain.hedge_trades
    assert disabled.pnl.total_pnl_pts == pytest.approx(plain.pnl.total_pnl_pts)


# --- F1.1 + F1.2 compose (AND of gates) ------------------------------------ #
def test_f11_and_f12_compose():
    prices = _rising()  # strictly rising, off_ts = 10:20
    w_open = resolve_et_to_utc(date(2025, 3, 3), "10:05")   # F1.1 opens (off-15)
    w_close = resolve_et_to_utc(date(2025, 3, 3), "10:10")  # F1.2 closes it (off-10)
    res = simulate_day(**_short_day(prices, hedge=_hspec(
        interval_minutes=1.0, delta_band=None,
        timing=HedgeTimingSpec(
            only_within_minutes_before_close=15.0,
            skip_near_extremum=SkipNearExtremumSpec(
                enabled=True, window_minutes=10.0, tolerance=0.0)))))
    # F1.1 suppresses < 10:05; F1.2 suppresses the buy-high >= 10:10. Only the
    # [10:05, 10:10) band survives.
    assert res.hedge_trades
    assert all(w_open <= h.ts < w_close for h in res.hedge_trades)
