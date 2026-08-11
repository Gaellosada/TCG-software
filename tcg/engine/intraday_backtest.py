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
from datetime import date, datetime, time, timezone
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
    HedgeTrade,
    IntradayBar,
    MarkSnapshot,
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
    snap_tolerance_minutes: float,
    hedge_enabled: bool,
    interval_minutes: float,
    delta_band: float,
    multiplier: float = ES_MULTIPLIER,
    rate: float = 0.0,
    tz: str = _ET,
    kernel: PricingKernel | None = None,
) -> DayResult:
    """Simulate one trading day; return an ``ok`` or ``skipped`` DayResult."""
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long'|'short', got {side!r}")
    if exit_ts <= entry_ts:
        raise ValueError("exit_ts must be after entry_ts")
    kernel = kernel or BS76Kernel()
    side_sign = 1 if side == "long" else -1
    tol = snap_tolerance_minutes

    # --- Required fills: nearest bar within tolerance, else SKIP the day. ---
    es1 = snap_nearest(es_bars, entry_ts, tol)
    c1 = snap_nearest(call_marks, entry_ts, tol)
    p1 = snap_nearest(put_marks, entry_ts, tol)
    es2 = snap_nearest(es_bars, exit_ts, tol)
    c2 = snap_nearest(call_marks, exit_ts, tol)
    p2 = snap_nearest(put_marks, exit_ts, tol)
    if None in (es1, c1, p1, es2, c2, p2):
        return DayResult(
            date=date_int,
            status="skipped",
            skip_reason="no_quote_within_tolerance",
            expiry=expiry,
            strike=strike,
        )

    s1 = c1.price + p1.price
    s2 = c2.price + p2.price
    option_pnl_pts = side_sign * (s2 - s1)

    entry_snap = MarkSnapshot(entry_ts, es1.price, c1.price, p1.price, s1)
    exit_snap = MarkSnapshot(exit_ts, es2.price, c2.price, p2.price, s2)
    expiry_utc = _expiry_utc(expiry, tz)

    hedge_trades: list[HedgeTrade] = []
    hedge_pnl_pts = 0.0

    if hedge_enabled:
        # Build the ES price path: entry -> intraday bars -> exit.
        path: list[tuple[datetime, float]] = [(entry_ts, es1.price)]
        for bar in es_bars:
            if entry_ts < bar.ts < exit_ts:
                path.append((bar.ts, bar.price))
        path.append((exit_ts, es2.price))

        hedged_qty = 0.0
        last_rehedge_ts = entry_ts
        for i, (ts, es_price) in enumerate(path):
            # Delta marks carry-forward: LAST KNOWN option quote at or before
            # ``ts`` — never a quote printed after ``ts`` (no look-ahead). The ES
            # price at each path point is that point's OWN bar price (also no
            # look-ahead). Entry/exit FILL marks (c1/c2/p1/p2) intentionally use
            # nearest-at-target and are unchanged.
            cm = last_known_at_or_before(call_marks, ts)
            pm = last_known_at_or_before(put_marks, ts)
            t_years = _year_fraction(ts, expiry_utc)
            net_delta = net_straddle_delta(
                kernel,
                es_price,
                strike,
                t_years,
                cm.price if cm else c1.price,
                pm.price if pm else p1.price,
                side_sign,
                rate,
            )
            is_entry = i == 0
            residual = net_delta + hedged_qty
            interval_hit = (ts - last_rehedge_ts).total_seconds() >= (
                interval_minutes * 60.0
            )
            band_hit = abs(residual) > delta_band
            # Never rehedge exactly at exit (position is being closed there).
            if is_entry or (ts < exit_ts and (interval_hit or band_hit)):
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
            # P&L on the segment ts -> next price, at the qty held over it.
            if i + 1 < len(path):
                next_price = path[i + 1][1]
                hedge_pnl_pts += hedged_qty * (next_price - es_price)

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
