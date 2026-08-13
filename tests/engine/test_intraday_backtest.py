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
    IntradayBar,
    MaxSpreadCond,
    MaxUnderlyingMoveCond,
    MinPremiumCond,
    MinQuoteSizeCond,
    NetDeltaTrigger,
    PnlTrigger,
    SigmaMoveTrigger,
    UnderlyingMoveTrigger,
)

UTC = timezone.utc


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
        hedge_enabled=False, interval_minutes=15.0, delta_band=0.10,
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
    tight = dict(bid=None)  # unused
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
        hedge_enabled=False, interval_minutes=15.0, delta_band=0.10,
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
        hedge_enabled=True, interval_minutes=1.0, delta_band=10.0,
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
        hedge_enabled=True, interval_minutes=999.0, delta_band=0.20,
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
        hedge_enabled=True, interval_minutes=999.0, delta_band=10.0,
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
    res = simulate_day(**_day(hedge_enabled=False))
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
                              hedge_enabled=True, interval_minutes=1.0, delta_band=10.0))
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
                              hedge_enabled=True, interval_minutes=1.0, delta_band=10.0))
    assert res.status == "ok"
    assert res.straddle_off_ts <= res.straddle_on_ts
    assert res.hedge_trades == ()          # no hedge over an empty window
    assert res.pnl.hedge_pnl_pts == 0.0


def test_net_delta_atm_near_zero():
    nd = net_straddle_delta(BS76Kernel(), 5000.0, 5000.0, 1.0 / 365.0, 30.0, 30.0, side_sign=1)
    assert abs(nd) < 0.2


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
        hedge_enabled=False, interval_minutes=15.0, delta_band=0.1,
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
        hedge_enabled=False, interval_minutes=15.0, delta_band=0.1,
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
        entry_tol=10.0, exit_tol=10.0, hedge_enabled=False,
        interval_minutes=15.0, delta_band=0.1,
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
        hedge_enabled=True, interval_minutes=5.0, delta_band=0.2,
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
