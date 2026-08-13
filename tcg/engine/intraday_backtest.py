"""Pure engine for the intraday ATM-straddle + delta-hedge backtest.

One round-trip per trading day: at T1 open an ATM straddle (call+put) on the
CME ES option complex, delta-hedge with the ES future from T1..T2, close at T2,
flat overnight. All functions here are PURE (no I/O, no async): the data layer
(:mod:`tcg.data._sql.intraday_v2`) fetches the 1m bars and the API layer
(:mod:`tcg.core.api.intraday_backtest`) orchestrates; this module only computes.

Conventions
-----------
* Entry/exit are **America/New_York (ET)**, DST-aware via :mod:`zoneinfo`, and
  converted per date to UTC here (:func:`resolve_et_to_utc`).
* Marks are a trade-driven, IRREGULAR event series (recon §1). A required fill
  (T1 open, T2 close) uses the nearest bar within ``snap_tolerance_minutes``;
  outside tolerance the day is SKIPPED (never fabricate a fill). Intraday delta
  marks (for hedging) carry-forward the nearest known bar (an estimate, not a
  fill), so a thin strike still yields a delta.
* Delta is model-computed: back IV out of the option mark via Black-76
  (reusing :class:`tcg.engine.options.pricing.BS76Kernel`), then its delta.
* P&L is in index points and dollars (points x multiplier, ES = $50/pt).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from tcg.engine.options.pricing import BS76Kernel
from tcg.engine.options.pricing.protocol import PricingKernel
from tcg.types.intraday import (
    AggregateResult,
    DayPnl,
    DayResult,
    EquityPoint,
    ES_MULTIPLIER,
    ES_OPTION_TICK_SIZE,
    HedgeTrade,
    IntradayBar,
    LegResult,
    MarkSnapshot,
    MaxSpreadCond,
    MaxUnderlyingMoveCond,
    MinPremiumCond,
    MinQuoteSizeCond,
    StraddleLegs,
)

_ET = "America/New_York"
_TRADING_DAYS_PER_YEAR = 252.0
_YEAR_SECONDS = 365.0 * 24.0 * 3600.0
# Floor on time-to-expiry (1 minute, in years) so a 0DTE straddle held to the
# bell never divides by a zero/negative T in the Black-76 kernel.
_MIN_T_YEARS = 60.0 / _YEAR_SECONDS
# ES options settle on the underlying-future close; model expiry at 16:00 ET.
_EXPIRY_TIME_ET = time(16, 0)


# --------------------------------------------------------------------------- #
# Timezone / time helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse ``"HH:MM"`` into ``(hour, minute)``; raise ``ValueError`` if bad."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be 'HH:MM', got {value!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time out of range: {value!r}")
    return h, m


def resolve_et_to_utc(day: date, hhmm: str, tz: str = _ET) -> datetime:
    """Resolve an ET wall-clock ``HH:MM`` on *day* to an aware UTC datetime.

    DST-aware: ``zoneinfo`` applies the correct UTC offset for the date, so
    10:00 ET is 14:00Z in winter and 15:00Z under daylight time automatically.
    """
    h, m = parse_hhmm(hhmm)
    local = datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc)


def _expiry_utc(expiry: date, tz: str = _ET) -> datetime:
    local = datetime.combine(expiry, _EXPIRY_TIME_ET, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc)


def _year_fraction(now_utc: datetime, expiry_utc: datetime) -> float:
    secs = (expiry_utc - now_utc).total_seconds()
    return max(secs / _YEAR_SECONDS, _MIN_T_YEARS)


# --------------------------------------------------------------------------- #
# Snap + selection (pure)
# --------------------------------------------------------------------------- #
def snap_nearest(
    bars: list[IntradayBar],
    target_ts: datetime,
    tolerance_minutes: float | None,
) -> IntradayBar | None:
    """Return the bar nearest *target_ts*; ``None`` if none within tolerance.

    ``tolerance_minutes=None`` disables the tolerance gate (used for the
    carry-forward delta marks, which are estimates rather than fills).
    """
    if not bars:
        return None
    best: IntradayBar | None = None
    best_gap = math.inf
    for bar in bars:
        gap = abs((bar.ts - target_ts).total_seconds())
        if gap < best_gap:
            best_gap = gap
            best = bar
    if best is None:
        return None
    if tolerance_minutes is not None and best_gap > tolerance_minutes * 60.0:
        return None
    return best


def last_known_at_or_before(
    bars: list[IntradayBar], target_ts: datetime
) -> IntradayBar | None:
    """Most recent bar with ``bar.ts <= target_ts`` (carry-forward), else ``None``.

    Used for the intraday hedge/delta marks so a rehedge at time ``ts`` never
    peeks at a quote printed AFTER ``ts`` (a backtest look-ahead). Assumes
    ``bars`` is ascending in ``ts`` (the reader returns them sorted). DESIGN
    §Sparse-quote: "carry-forward the last known bar".
    """
    best: IntradayBar | None = None
    for bar in bars:
        if bar.ts <= target_ts:
            best = bar
        else:
            break
    return best


def select_atm_strike(underlying: float, strikes: list[float]) -> float:
    """Nearest listed strike to *underlying* (ties -> lower strike)."""
    if not strikes:
        raise ValueError("no strikes to select from")
    return min(strikes, key=lambda k: (abs(k - underlying), k))


# --------------------------------------------------------------------------- #
# Conditional entry/exit modules (v2) — pure evaluation + independent-leg scan
# --------------------------------------------------------------------------- #
def _split_conditions(
    conditions: tuple | list,
) -> tuple[list, MaxUnderlyingMoveCond | None]:
    """Partition a condition list into per-leg conditions and the (single)
    ES-level ``max_underlying_move`` condition (or ``None``)."""
    leg = [c for c in conditions if not isinstance(c, MaxUnderlyingMoveCond)]
    move = next(
        (c for c in conditions if isinstance(c, MaxUnderlyingMoveCond)), None
    )
    return leg, move


def leg_bar_qualifies(
    bar: IntradayBar, leg_conditions: list, tick_size: float
) -> bool:
    """True iff *bar* passes ALL per-leg conditions (AND-ed).

    ``max_spread`` and ``min_quote_size`` REQUIRE a two-sided quote; a
    last-trade-only bar (bid/ask or sizes ``None``) fails them. ``min_premium``
    reads the bar mark (``bar.price``).
    """
    for c in leg_conditions:
        if isinstance(c, MaxSpreadCond):
            if bar.bid is None or bar.ask is None:
                return False
            spread = bar.ask - bar.bid
            floor = max(c.pct / 100.0 * bar.price, c.min_ticks * tick_size)
            if spread > floor:
                return False
        elif isinstance(c, MinQuoteSizeCond):
            if bar.bid_size is None or bar.ask_size is None:
                return False
            if not (bar.bid_size >= c.size and bar.ask_size >= c.size):
                return False
        elif isinstance(c, MinPremiumCond):
            if bar.price < c.points:
                return False
    return True


def underlying_move_ok(
    es_price: float, es_ref: float | None, cond: MaxUnderlyingMoveCond
) -> bool:
    """True iff ``abs(es_price - es_ref)/es_ref*100 <= cond.pct``.

    Fails closed if the reference is missing/zero (cannot evaluate the move ->
    the bar does not qualify rather than silently passing).
    """
    if es_ref is None or es_ref == 0.0:
        return False
    return abs(es_price - es_ref) / abs(es_ref) * 100.0 <= cond.pct


def _bar_qualifies_full(
    bar: IntradayBar,
    es_bars: list[IntradayBar],
    leg_conditions: list,
    move_cond: MaxUnderlyingMoveCond | None,
    es_ref: float | None,
    tick_size: float,
) -> bool:
    """Per-leg conditions AND the ES-level move check (ES carried-forward to
    the bar's ts — no look-ahead)."""
    if not leg_bar_qualifies(bar, leg_conditions, tick_size):
        return False
    if move_cond is not None:
        es = last_known_at_or_before(es_bars, bar.ts)
        if es is None or not underlying_move_ok(es.price, es_ref, move_cond):
            return False
    return True


def scan_leg_entry(
    marks: list[IntradayBar],
    es_bars: list[IntradayBar],
    entry_ts: datetime,
    tolerance_minutes: float,
    leg_conditions: list,
    move_cond: MaxUnderlyingMoveCond | None,
    es_ref: float | None,
    tick_size: float,
) -> tuple[IntradayBar | None, str | None]:
    """Independent per-leg entry: first qualifying bar in ``[entry_ts,
    entry_ts+tol]`` scanned FORWARD.

    Returns ``(bar, None)`` on a fill, else ``(None, reason)`` where reason is
    ``"no_bars"`` (no bars at all in the window -> the day's
    ``no_quote_within_tolerance``) or ``"conditions_unmet"`` (bars existed, none
    qualified -> the day's ``entry_conditions_unmet``).
    """
    hi = entry_ts + timedelta(minutes=tolerance_minutes)
    window = [b for b in marks if entry_ts <= b.ts <= hi]
    if not window:
        return None, "no_bars"
    for bar in window:  # ascending ts -> first qualifying = earliest
        if _bar_qualifies_full(
            bar, es_bars, leg_conditions, move_cond, es_ref, tick_size
        ):
            return bar, None
    return None, "conditions_unmet"


def scan_leg_exit(
    marks: list[IntradayBar],
    es_bars: list[IntradayBar],
    exit_ts: datetime,
    tolerance_minutes: float,
    leg_conditions: list,
    move_cond: MaxUnderlyingMoveCond | None,
    es_ref: float | None,
    tick_size: float,
) -> tuple[IntradayBar | None, bool]:
    """Independent per-leg exit (MUST close): first qualifying bar in
    ``[exit_ts, exit_ts+tol]``; else FALL BACK to the nearest available bar
    (any, no tolerance) with ``exit_conditions_met=False``.

    Returns ``(bar, met)``. ``bar`` is ``None`` only if the leg has no marks at
    all (cannot happen once its entry filled — the day still guards it).
    """
    hi = exit_ts + timedelta(minutes=tolerance_minutes)
    window = [b for b in marks if exit_ts <= b.ts <= hi]
    for bar in window:
        if _bar_qualifies_full(
            bar, es_bars, leg_conditions, move_cond, es_ref, tick_size
        ):
            return bar, True
    # No qualifying bar in the window: must still close -> nearest available bar.
    return snap_nearest(marks, exit_ts, None), False


# --------------------------------------------------------------------------- #
# Delta (pure, model-computed)
# --------------------------------------------------------------------------- #
def _leg_delta(
    kernel: PricingKernel,
    forward: float,
    strike: float,
    t_years: float,
    mark: float,
    flag: str,
    rate: float,
) -> float:
    """Black-76 delta for one leg: invert IV from the mark, then delta.

    On an IV-inversion failure (mark below intrinsic / above max, a stale or
    one-sided quote) fall back to a moneyness delta so the hedge still has a
    number rather than dropping the leg silently.
    """
    try:
        sigma = kernel.implied_vol(mark, forward, strike, t_years, rate, flag)
        if math.isfinite(sigma) and sigma > 0.0:
            d = kernel.delta(forward, strike, t_years, rate, sigma, flag)
            if math.isfinite(d):
                return d
    except Exception:  # noqa: BLE001 - py_vollib raises Below/AboveException
        pass
    # Fallback: intrinsic moneyness delta.
    if flag == "c":
        return 1.0 if forward > strike else (0.5 if forward == strike else 0.0)
    return -1.0 if forward < strike else (-0.5 if forward == strike else 0.0)


def net_straddle_delta(
    kernel: PricingKernel,
    forward: float,
    strike: float,
    t_years: float,
    call_mark: float,
    put_mark: float,
    side_sign: int,
    rate: float = 0.0,
) -> float:
    """Side-signed net delta of the straddle (long call+put => dc+dp)."""
    dc = _leg_delta(kernel, forward, strike, t_years, call_mark, "c", rate)
    dp = _leg_delta(kernel, forward, strike, t_years, put_mark, "p", rate)
    return float(side_sign) * (dc + dp)


# --------------------------------------------------------------------------- #
# Per-day simulation (pure)
# --------------------------------------------------------------------------- #
def _es_at(es_bars: list[IntradayBar], ts: datetime) -> IntradayBar | None:
    """ES bar at ``ts``: last known at/before (no look-ahead), else nearest."""
    return last_known_at_or_before(es_bars, ts) or snap_nearest(es_bars, ts, None)


def _run_hedge(
    *,
    es_bars: list[IntradayBar],
    call_marks: list[IntradayBar],
    put_marks: list[IntradayBar],
    on_ts: datetime,
    off_ts: datetime,
    on_price: float,
    off_price: float,
    strike: float,
    expiry_utc: datetime,
    side_sign: int,
    interval_minutes: float,
    delta_band: float,
    rate: float,
    call_fallback: float,
    put_fallback: float,
    kernel: PricingKernel,
) -> tuple[list[HedgeTrade], float]:
    """Delta-hedge the COMBINED net delta over the BOTH-ON window
    ``[on_ts, off_ts]`` (single-leg legging windows are UNHEDGED, per DESIGN).

    Returns ``(hedge_trades, hedge_pnl_pts)``.
    """
    hedge_trades: list[HedgeTrade] = []
    hedge_pnl_pts = 0.0

    path: list[tuple[datetime, float]] = [(on_ts, on_price)]
    for bar in es_bars:
        if on_ts < bar.ts < off_ts:
            path.append((bar.ts, bar.price))
    path.append((off_ts, off_price))

    hedged_qty = 0.0
    last_rehedge_ts = on_ts
    for i, (ts, es_price) in enumerate(path):
        # Delta marks carry-forward: LAST KNOWN option quote at/before ``ts`` —
        # never a quote printed after ``ts`` (no look-ahead).
        cm = last_known_at_or_before(call_marks, ts)
        pm = last_known_at_or_before(put_marks, ts)
        t_years = _year_fraction(ts, expiry_utc)
        net_delta = net_straddle_delta(
            kernel,
            es_price,
            strike,
            t_years,
            cm.price if cm else call_fallback,
            pm.price if pm else put_fallback,
            side_sign,
            rate,
        )
        is_entry = i == 0
        residual = net_delta + hedged_qty
        interval_hit = (ts - last_rehedge_ts).total_seconds() >= (
            interval_minutes * 60.0
        )
        band_hit = abs(residual) > delta_band
        # Never rehedge exactly at off_ts (position is being closed there).
        if is_entry or (ts < off_ts and (interval_hit or band_hit)):
            hedged_qty = -net_delta
            last_rehedge_ts = ts
            hedge_trades.append(
                HedgeTrade(
                    ts=ts,
                    underlying=es_price,
                    net_delta=net_delta,
                    hedge_qty=hedged_qty,
                )
            )
        if i + 1 < len(path):
            next_price = path[i + 1][1]
            hedge_pnl_pts += hedged_qty * (next_price - es_price)

    return hedge_trades, hedge_pnl_pts


def simulate_day(
    *,
    date_int: int,
    side: str,
    strike: float,
    expiry: date,
    es_bars: list[IntradayBar],
    call_marks: list[IntradayBar],
    put_marks: list[IntradayBar],
    entry_ts: datetime,
    exit_ts: datetime,
    entry_tol: float,
    exit_tol: float,
    entry_conditions: tuple | list = (),
    exit_conditions: tuple | list = (),
    tick_size: float = ES_OPTION_TICK_SIZE,
    hedge_enabled: bool,
    interval_minutes: float,
    delta_band: float,
    es_day_open: float | None = None,
    multiplier: float = ES_MULTIPLIER,
    rate: float = 0.0,
    tz: str = _ET,
    kernel: PricingKernel | None = None,
) -> DayResult:
    """Simulate one trading day with INDEPENDENT per-leg entry/exit (v2).

    Each leg (call, put) finds its OWN first qualifying bar scanning forward
    from the target time within its snap tolerance. The straddle is BOTH-ON
    over ``[straddle_on_ts=max(entries), straddle_off_ts=min(exits)]``; only
    that window is delta-hedged (legging-in/out is single-leg, unhedged).
    Returns an ``ok`` or ``skipped`` DayResult.
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long'|'short', got {side!r}")
    if exit_ts <= entry_ts:
        raise ValueError("exit_ts must be after entry_ts")
    kernel = kernel or BS76Kernel()
    side_sign = 1 if side == "long" else -1

    # ES "day open" reference for max_underlying_move (recon: first ES bar of
    # the day). Falls back to the first bar in the provided window.
    es_ref = es_day_open if es_day_open is not None else (
        es_bars[0].price if es_bars else None
    )
    entry_leg_conds, entry_move = _split_conditions(entry_conditions)
    exit_leg_conds, exit_move = _split_conditions(exit_conditions)

    # --- INDEPENDENT per-leg ENTRY -------------------------------------- #
    c_entry, c_reason = scan_leg_entry(
        call_marks, es_bars, entry_ts, entry_tol,
        entry_leg_conds, entry_move, es_ref, tick_size,
    )
    p_entry, p_reason = scan_leg_entry(
        put_marks, es_bars, entry_ts, entry_tol,
        entry_leg_conds, entry_move, es_ref, tick_size,
    )
    if c_entry is None or p_entry is None:
        # no_bars (a leg had NO bars in window) dominates conditions_unmet.
        reasons = [r for r in (c_reason, p_reason) if r is not None]
        skip = (
            "no_quote_within_tolerance"
            if "no_bars" in reasons
            else "entry_conditions_unmet"
        )
        return DayResult(
            date=date_int,
            status="skipped",
            skip_reason=skip,
            expiry=expiry,
            strike=strike,
        )

    # --- INDEPENDENT per-leg EXIT (must close) -------------------------- #
    c_exit, c_met = scan_leg_exit(
        call_marks, es_bars, exit_ts, exit_tol,
        exit_leg_conds, exit_move, es_ref, tick_size,
    )
    p_exit, p_met = scan_leg_exit(
        put_marks, es_bars, exit_ts, exit_tol,
        exit_leg_conds, exit_move, es_ref, tick_size,
    )
    # A leg that entered has marks, so nearest-fallback always returns a bar.
    if c_exit is None or p_exit is None:  # pragma: no cover - defensive
        return DayResult(
            date=date_int,
            status="skipped",
            skip_reason="no_quote_within_tolerance",
            expiry=expiry,
            strike=strike,
        )

    # --- Per-leg P&L (points, side-signed) ------------------------------ #
    call_pnl = side_sign * (c_exit.price - c_entry.price)
    put_pnl = side_sign * (p_exit.price - p_entry.price)
    option_pnl_pts = call_pnl + put_pnl

    straddle_on_ts = max(c_entry.ts, p_entry.ts)
    straddle_off_ts = min(c_exit.ts, p_exit.ts)

    legs = StraddleLegs(
        call=LegResult(
            entry_ts=c_entry.ts, entry_price=c_entry.price,
            exit_ts=c_exit.ts, exit_price=c_exit.price,
            exit_conditions_met=c_met, pnl_pts=call_pnl,
        ),
        put=LegResult(
            entry_ts=p_entry.ts, entry_price=p_entry.price,
            exit_ts=p_exit.ts, exit_price=p_exit.price,
            exit_conditions_met=p_met, pnl_pts=put_pnl,
        ),
    )

    # Straddle-level summary: ts = both-on boundary, price = call+put fills.
    es_on = _es_at(es_bars, straddle_on_ts)
    es_off = _es_at(es_bars, straddle_off_ts)
    entry_snap = MarkSnapshot(
        straddle_on_ts,
        es_on.price if es_on else float("nan"),
        c_entry.price, p_entry.price, c_entry.price + p_entry.price,
    )
    exit_snap = MarkSnapshot(
        straddle_off_ts,
        es_off.price if es_off else float("nan"),
        c_exit.price, p_exit.price, c_exit.price + p_exit.price,
    )

    # --- Delta hedge over the BOTH-ON window ---------------------------- #
    expiry_utc = _expiry_utc(expiry, tz)
    hedge_trades: list[HedgeTrade] = []
    hedge_pnl_pts = 0.0
    if hedge_enabled and straddle_off_ts > straddle_on_ts and es_on and es_off:
        hedge_trades, hedge_pnl_pts = _run_hedge(
            es_bars=es_bars,
            call_marks=call_marks,
            put_marks=put_marks,
            on_ts=straddle_on_ts,
            off_ts=straddle_off_ts,
            on_price=es_on.price,
            off_price=es_off.price,
            strike=strike,
            expiry_utc=expiry_utc,
            side_sign=side_sign,
            interval_minutes=interval_minutes,
            delta_band=delta_band,
            rate=rate,
            call_fallback=c_entry.price,
            put_fallback=p_entry.price,
            kernel=kernel,
        )

    total_pnl_pts = option_pnl_pts + hedge_pnl_pts
    pnl = DayPnl(
        option_pnl_pts=option_pnl_pts,
        hedge_pnl_pts=hedge_pnl_pts,
        total_pnl_pts=total_pnl_pts,
        total_pnl_usd=total_pnl_pts * multiplier,
    )
    return DayResult(
        date=date_int,
        status="ok",
        skip_reason=None,
        expiry=expiry,
        strike=strike,
        entry=entry_snap,
        exit=exit_snap,
        hedge_trades=tuple(hedge_trades),
        pnl=pnl,
        legs=legs,
        straddle_on_ts=straddle_on_ts,
        straddle_off_ts=straddle_off_ts,
    )


# --------------------------------------------------------------------------- #
# Aggregation (pure)
# --------------------------------------------------------------------------- #
def aggregate_days(results: list[DayResult]) -> AggregateResult:
    """Aggregate per-day results into the summary block.

    Sharpe / max-drawdown are computed on the daily-P&L (USD) series — the same
    annualization constant (252) as :mod:`tcg.engine.metrics`; drawdown is on the
    cumulative-P&L curve (a dollar series that can cross zero, so it is computed
    on levels, not returns).
    """
    n_days = len(results)
    traded = [r for r in results if r.status == "ok" and r.pnl is not None]
    n_traded = len(traded)
    n_skipped = n_days - n_traded

    pnls = np.array([r.pnl.total_pnl_usd for r in traded], dtype=np.float64)
    total = float(pnls.sum()) if n_traded else 0.0
    mean = float(pnls.mean()) if n_traded else 0.0
    win_rate = float(np.mean(pnls > 0.0)) if n_traded else None

    if n_traded > 1:
        std = float(np.std(pnls, ddof=1))
        sharpe = (
            float(mean / std * math.sqrt(_TRADING_DAYS_PER_YEAR)) if std > 0 else 0.0
        )
    else:
        sharpe = 0.0

    # Equity curve + max drawdown on cumulative dollar P&L.
    equity: list[EquityPoint] = []
    cum = 0.0
    # Drawdown is measured on the P&L-from-0 baseline (cum starts at 0, so peak
    # inits at 0): max_dd is the worst trough of cumulative dollars below the
    # running peak of that same from-0 curve, not below a notional capital base.
    peak = 0.0
    max_dd = 0.0
    for r in traded:
        cum += r.pnl.total_pnl_usd
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        equity.append(EquityPoint(date=r.date, cum_pnl_usd=cum))

    return AggregateResult(
        n_days=n_days,
        n_traded=n_traded,
        n_skipped=n_skipped,
        total_pnl_usd=total,
        mean_daily_pnl_usd=mean,
        win_rate=win_rate,
        sharpe=sharpe,
        max_drawdown_usd=max_dd,
        equity_curve=tuple(equity),
    )
