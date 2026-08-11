"""Unit tests for the pure intraday ATM-straddle + delta-hedge engine.

All synthetic — no dwh. Covers: ATM selection, snap/skip, delta-hedge triggers
(interval AND band), P&L sign long vs short, dollarization, DST/timezone, and
the T2<=T1 guard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tcg.engine.intraday_backtest import (
    aggregate_days,
    last_known_at_or_before,
    net_straddle_delta,
    resolve_et_to_utc,
    select_atm_strike,
    simulate_day,
    snap_nearest,
)
from tcg.engine.options.pricing import BS76Kernel
from tcg.types.intraday import IntradayBar

UTC = timezone.utc


def _bars(base: datetime, prices: list[float], step_min: int = 1) -> list[IntradayBar]:
    return [
        IntradayBar(ts=base + timedelta(minutes=i * step_min), price=p)
        for i, p in enumerate(prices)
    ]


# --------------------------------------------------------------------------- #
# Timezone / DST
# --------------------------------------------------------------------------- #
def test_dst_winter_vs_summer():
    # EST = UTC-5 (winter): 10:00 ET -> 15:00Z. EDT = UTC-4 (summer): -> 14:00Z.
    assert resolve_et_to_utc(date(2025, 1, 15), "10:00").hour == 15
    assert resolve_et_to_utc(date(2025, 7, 15), "10:00").hour == 14


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
# Snap / skip
# --------------------------------------------------------------------------- #
def test_snap_within_tolerance():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    bars = _bars(base, [10.0, 11.0, 12.0])
    target = base + timedelta(minutes=1, seconds=20)
    hit = snap_nearest(bars, target, 10.0)
    assert hit is not None and hit.price == 11.0  # nearest to +1:20 is the +1m bar


def test_snap_outside_tolerance_returns_none():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    bars = _bars(base, [10.0])
    target = base + timedelta(minutes=30)
    assert snap_nearest(bars, target, 10.0) is None


def test_last_known_at_or_before_no_lookahead():
    base = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)
    bars = _bars(base, [10.0, 11.0, 12.0])  # at +0, +1, +2 min
    # A target between the +1 and +2 bars must carry-forward the +1 bar,
    # NEVER peek at the closer-but-future +2 bar.
    target = base + timedelta(minutes=1, seconds=50)
    hit = last_known_at_or_before(bars, target)
    assert hit is not None and hit.price == 11.0
    # Before the first bar => nothing known yet.
    assert last_known_at_or_before(bars, base - timedelta(minutes=1)) is None
    # Exactly on a bar ts => that bar (<= is inclusive).
    assert last_known_at_or_before(bars, base + timedelta(minutes=2)).price == 12.0


def _synthetic_day(**over):
    """A well-formed day: ATM straddle, flat ES, marks present at entry+exit."""
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:30")
    # ES flat at 5000 every minute across the window.
    n = int((exit_ - entry).total_seconds() // 60) + 3
    es = _bars(entry - timedelta(minutes=1), [5000.0] * n)
    calls = _bars(entry - timedelta(minutes=1), [30.0] * n)
    puts = _bars(entry - timedelta(minutes=1), [30.0] * n)
    kwargs = dict(
        date_int=20250303,
        side="long",
        strike=5000.0,
        expiry=date(2025, 3, 4),
        es_bars=es,
        call_marks=calls,
        put_marks=puts,
        entry_ts=entry,
        exit_ts=exit_,
        snap_tolerance_minutes=10.0,
        hedge_enabled=False,
        interval_minutes=15.0,
        delta_band=0.10,
    )
    kwargs.update(over)
    return kwargs


def test_simulate_skips_when_no_quote_within_tolerance():
    kw = _synthetic_day(call_marks=[], put_marks=[])
    res = simulate_day(**kw)
    assert res.status == "skipped"
    assert res.skip_reason == "no_quote_within_tolerance"


def test_simulate_ok_when_quotes_present():
    res = simulate_day(**_synthetic_day())
    assert res.status == "ok"
    assert res.entry is not None and res.exit is not None


# --------------------------------------------------------------------------- #
# P&L sign long vs short + dollarization
# --------------------------------------------------------------------------- #
def test_pnl_sign_long_vs_short_and_dollarization():
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = resolve_et_to_utc(day, "10:05")
    es = _bars(entry - timedelta(minutes=1), [5000.0] * 8)
    # Straddle worth 60 at entry, 55 at exit (call 50 + put 5).
    calls = [IntradayBar(entry, 30.0), IntradayBar(exit_, 50.0)]
    puts = [IntradayBar(entry, 30.0), IntradayBar(exit_, 5.0)]
    common = dict(
        date_int=20250303,
        strike=5000.0,
        expiry=date(2025, 3, 4),
        es_bars=es,
        call_marks=calls,
        put_marks=puts,
        entry_ts=entry,
        exit_ts=exit_,
        snap_tolerance_minutes=10.0,
        hedge_enabled=False,
        interval_minutes=15.0,
        delta_band=0.10,
    )
    long_res = simulate_day(side="long", **common)
    short_res = simulate_day(side="short", **common)
    # Straddle fell 60 -> 55: long loses 5 pts, short gains 5 pts.
    assert long_res.pnl.option_pnl_pts == pytest.approx(-5.0)
    assert short_res.pnl.option_pnl_pts == pytest.approx(5.0)
    # Dollarization x50.
    assert long_res.pnl.total_pnl_usd == pytest.approx(-250.0)
    assert short_res.pnl.total_pnl_usd == pytest.approx(250.0)


# --------------------------------------------------------------------------- #
# Delta-hedge triggers: interval AND band
# --------------------------------------------------------------------------- #
def test_hedge_triggers_on_interval():
    # ES flat + constant ATM marks => net delta ~constant => band never fires.
    # interval=1min over a 5-min window => rehedge at entry + each minute.
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=5)
    es = _bars(entry, [5000.0] * 6)  # entry..entry+5
    marks = _bars(entry, [30.0] * 6)
    res = simulate_day(
        date_int=20250303,
        side="long",
        strike=5000.0,
        expiry=date(2025, 3, 4),
        es_bars=es,
        call_marks=marks,
        put_marks=marks,
        entry_ts=entry,
        exit_ts=exit_,
        snap_tolerance_minutes=10.0,
        hedge_enabled=True,
        interval_minutes=1.0,
        delta_band=10.0,  # huge => never band-triggers
    )
    assert res.status == "ok"
    # entry + one per minute at t=1,2,3,4 (exit at t=5 is not rehedged).
    assert len(res.hedge_trades) >= 4


def test_hedge_triggers_on_band():
    # interval huge => only the band can trigger. A big ES jump drives net
    # delta well past the band => a second rehedge appears.
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=2)
    es = [IntradayBar(entry, 5000.0), IntradayBar(entry + timedelta(minutes=1), 5200.0)]
    calls = [IntradayBar(entry, 30.0), IntradayBar(entry + timedelta(minutes=1), 30.0)]
    puts = [IntradayBar(entry, 30.0), IntradayBar(entry + timedelta(minutes=1), 30.0)]
    res = simulate_day(
        date_int=20250303,
        side="long",
        strike=5000.0,
        expiry=date(2025, 3, 4),
        es_bars=es,
        call_marks=calls,
        put_marks=puts,
        entry_ts=entry,
        exit_ts=exit_,
        snap_tolerance_minutes=10.0,
        hedge_enabled=True,
        interval_minutes=999.0,  # never interval-triggers
        delta_band=0.20,
    )
    assert res.status == "ok"
    # entry hedge + one band-triggered rehedge at the +200pt bar.
    assert len(res.hedge_trades) == 2


class _FixedKernel:
    """Deterministic kernel stub: pins the straddle net delta so the futures-leg
    P&L decomposition can be asserted numerically (call delta +0.6, put -0.1 =>
    net +0.5 long). ``implied_vol`` returns a finite sigma so the engine takes
    the model-delta branch (not the moneyness fallback)."""

    def implied_vol(self, price, F, K, T, r, flag):  # noqa: D401
        return 0.2

    def delta(self, F, K, T, r, sigma, flag):  # noqa: D401
        return 0.6 if flag == "c" else -0.1


def _monotone_es_day(prices: list[float]):
    day = date(2025, 3, 3)
    entry = resolve_et_to_utc(day, "10:00")
    exit_ = entry + timedelta(minutes=len(prices) - 1)
    es = _bars(entry, prices)  # one bar per minute, monotone
    marks = _bars(entry, [30.0] * len(prices))  # flat straddle => 0 option P&L
    return dict(
        date_int=20250303,
        side="long",
        strike=5000.0,
        expiry=date(2025, 3, 4),
        es_bars=es,
        call_marks=marks,
        put_marks=marks,
        entry_ts=entry,
        exit_ts=exit_,
        snap_tolerance_minutes=10.0,
        hedge_enabled=True,
        interval_minutes=999.0,  # single entry rehedge => qty held fixed
        delta_band=10.0,  # never band-triggers => qty held fixed
        kernel=_FixedKernel(),
    )


def test_hedge_pnl_numerical_sign_and_magnitude():
    # Net delta pinned at +0.5 (long) => entry hedge shorts 0.5 futures.
    # qty is held fixed (one rehedge), so hedge P&L telescopes to
    #   qty * (ES_exit - ES_entry) = -0.5 * dES.
    # ES rises 5000 -> 5010 (dES=+10): a short-futures hedge LOSES => -5.0 pts.
    up = simulate_day(**_monotone_es_day([5000.0, 5004.0, 5008.0, 5010.0]))
    assert up.status == "ok"
    assert len(up.hedge_trades) == 1  # entry only
    assert up.hedge_trades[0].hedge_qty == pytest.approx(-0.5)
    assert up.pnl.hedge_pnl_pts < 0
    assert up.pnl.hedge_pnl_pts == pytest.approx(-5.0)  # -0.5 * (+10)
    assert up.pnl.option_pnl_pts == pytest.approx(0.0)  # flat marks
    assert up.pnl.total_pnl_usd == pytest.approx(-250.0)  # -5 pts * 50

    # ES falls 5010 -> 5000 (dES=-10): the same short-futures hedge GAINS => +5.0.
    down = simulate_day(**_monotone_es_day([5010.0, 5006.0, 5002.0, 5000.0]))
    assert down.pnl.hedge_pnl_pts > 0
    assert down.pnl.hedge_pnl_pts == pytest.approx(5.0)  # -0.5 * (-10)


def test_no_hedge_when_disabled():
    res = simulate_day(**_synthetic_day(hedge_enabled=False))
    assert res.hedge_trades == ()
    assert res.pnl.hedge_pnl_pts == 0.0


def test_net_delta_atm_near_zero():
    # ATM straddle: call delta ~ +0.5, put ~ -0.5 => net ~ 0.
    nd = net_straddle_delta(
        BS76Kernel(), 5000.0, 5000.0, 1.0 / 365.0, 30.0, 30.0, side_sign=1
    )
    assert abs(nd) < 0.2


# --------------------------------------------------------------------------- #
# T2 <= T1 guard
# --------------------------------------------------------------------------- #
def test_exit_not_after_entry_raises():
    kw = _synthetic_day()
    kw["exit_ts"] = kw["entry_ts"]
    with pytest.raises(ValueError):
        simulate_day(**kw)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_aggregate_counts_and_winrate():
    r1 = simulate_day(**_synthetic_day())  # flat -> ~0 pnl
    day2 = _synthetic_day(
        date_int=20250304,
        call_marks=[IntradayBar(resolve_et_to_utc(date(2025, 3, 3), "10:00"), 30.0),
                    IntradayBar(resolve_et_to_utc(date(2025, 3, 3), "10:30"), 50.0)],
        put_marks=[IntradayBar(resolve_et_to_utc(date(2025, 3, 3), "10:00"), 30.0),
                   IntradayBar(resolve_et_to_utc(date(2025, 3, 3), "10:30"), 30.0)],
    )
    r2 = simulate_day(**day2)  # straddle rose -> long gains
    skipped = simulate_day(**_synthetic_day(date_int=20250305, call_marks=[], put_marks=[]))
    agg = aggregate_days([r1, r2, skipped])
    assert agg.n_days == 3
    assert agg.n_traded == 2
    assert agg.n_skipped == 1
    assert 0.0 <= agg.win_rate <= 1.0
    assert len(agg.equity_curve) == 2
