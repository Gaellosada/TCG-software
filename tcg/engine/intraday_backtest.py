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
    COST_DISABLED,
    CostModel,
    DayPnl,
    DayResult,
    EquityPoint,
    ES_MULTIPLIER,
    ES_OPTION_TICK_SIZE,
    ES_FUTURE_TICK_SIZE,
    es_option_tick,
    ExitTrigger,
    HedgeSpec,
    HedgeTimingSpec,
    HedgeTrade,
    IntradayBar,
    LadderEntry,
    LegResult,
    MarkSnapshot,
    MaxSpreadCond,
    MaxUnderlyingMoveCond,
    MinPremiumCond,
    MinQuoteSizeCond,
    MinRehedgeDeltaCond,
    NetDeltaTrigger,
    PnlTrigger,
    SigmaMoveTrigger,
    StraddleLegs,
    UnderlyingMoveTrigger,
)

# A disabled hedge module: the ``simulate_day`` default when no hedge is given.
_HEDGE_DISABLED = HedgeSpec(enabled=False)

_ET = "America/New_York"
_TRADING_DAYS_PER_YEAR = 252.0
_YEAR_SECONDS = 365.0 * 24.0 * 3600.0
# Floor on time-to-expiry (1 minute, in years) so a 0DTE straddle held to the
# bell never divides by a zero/negative T in the Black-76 kernel.
_MIN_T_YEARS = 60.0 / _YEAR_SECONDS
# ES options settle on the underlying-future close; model expiry at 16:00 ET.
_EXPIRY_TIME_ET = time(16, 0)
# Last-resort IV used to compute a Black-76 delta when a leg's own mark fails to
# invert AND no carried entry ATM IV is available (a doubly-degenerate thin/
# one-sided quote). A modest equity-index vol so the fallback delta is a SMOOTH
# Black-76 number (~+/-0.5 ATM), never the old discontinuous 0<->|1| step that
# over-hedged a near-ATM leg. Only reached when there is no market IV at all.
_FALLBACK_IV = 0.20


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
            # Tier-aware CME ES-option tick: 0.05 for premium <= 5.00, else 0.25.
            # ``tick_size`` is the sourced low-tier increment; a >5.00 ATM leg
            # quoted one true tick wide (0.25) must not be spuriously rejected.
            tick = es_option_tick(bar.price, tick_size)
            floor = max(c.pct / 100.0 * bar.price, c.min_ticks * tick)
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


def _resync_leg_to_T(
    marks: list[IntradayBar],
    es_bars: list[IntradayBar],
    T: datetime,
    leg_conditions: list,
    move_cond: MaxUnderlyingMoveCond | None,
    es_ref: float | None,
    tick_size: float,
) -> IntradayBar | None:
    """Rule 7 (Gap 2): the leg's LAST clean qualifying quote at or before the
    common instant ``T`` (= ``straddle_on_ts`` = the later of the two leg
    entries), so BOTH legs are valued at the SAME instant.

    Scans ``marks`` (ascending ts) and returns the latest bar with ``ts <= T``
    that passes the ENTRY qualifiers (``_bar_qualifies_full`` — same predicate
    ``scan_leg_entry`` used). Returns ``None`` only if no qualifying bar is <= T
    (never happens for a leg that already entered — its own entry bar qualifies
    and is <= T — so the result is always >= the scan-entry, never earlier)."""
    best: IntradayBar | None = None
    for bar in marks:  # ascending ts
        if bar.ts > T:
            break
        if _bar_qualifies_full(
            bar, es_bars, leg_conditions, move_cond, es_ref, tick_size
        ):
            best = bar
    return best


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
    fallback_iv: float | None = None,
) -> float:
    """Black-76 delta for one leg: invert IV from the mark, then delta.

    On an IV-inversion failure (mark below intrinsic / above max, a stale or
    one-sided quote) still compute a proper Black-76 delta from a fallback vol
    rather than a discontinuous moneyness step: prefer ``fallback_iv`` (the
    carried entry ATM IV) if finite/positive, else a modest ``_FALLBACK_IV``.
    This keeps a near-ATM leg's delta smooth (~+/-0.5) instead of snapping to
    0 or +/-1 in exactly the sparse-quote regime this engine targets.
    """
    try:
        sigma = kernel.implied_vol(mark, forward, strike, t_years, rate, flag)
        if math.isfinite(sigma) and sigma > 0.0:
            d = kernel.delta(forward, strike, t_years, rate, sigma, flag)
            if math.isfinite(d):
                return d
    except Exception:  # noqa: BLE001 - py_vollib raises Below/AboveException
        pass
    # Fallback: a proper Black-76 delta from a carried/last-resort vol (smooth,
    # ATM ~ +/-0.5), never a 0<->|1| step across the strike.
    sigma = (
        fallback_iv
        if (fallback_iv is not None and math.isfinite(fallback_iv) and fallback_iv > 0.0)
        else _FALLBACK_IV
    )
    d = kernel.delta(forward, strike, t_years, rate, sigma, flag)
    return d if math.isfinite(d) else 0.0


def net_straddle_delta(
    kernel: PricingKernel,
    forward: float,
    strike: float,
    t_years: float,
    call_mark: float,
    put_mark: float,
    side_sign: int,
    rate: float = 0.0,
    fallback_iv: float | None = None,
) -> float:
    """Side-signed net delta of the straddle (long call+put => dc+dp).

    ``fallback_iv`` (the entry ATM IV, when available) is carried into each leg
    so an IV-inversion failure yields a smooth Black-76 delta, not a step.
    """
    dc = _leg_delta(kernel, forward, strike, t_years, call_mark, "c", rate, fallback_iv)
    dp = _leg_delta(kernel, forward, strike, t_years, put_mark, "p", rate, fallback_iv)
    return float(side_sign) * (dc + dp)


# --------------------------------------------------------------------------- #
# Early-exit TRIGGERS (v3) — pure per-bar evaluation over the hold window
# --------------------------------------------------------------------------- #
_TRIGGER_TYPE = {
    UnderlyingMoveTrigger: "underlying_move",
    SigmaMoveTrigger: "sigma_move",
    NetDeltaTrigger: "net_delta",
    PnlTrigger: "pnl",
}


def entry_atm_iv(
    kernel: PricingKernel,
    forward: float,
    strike: float,
    t_years: float,
    call_mark: float,
    put_mark: float,
    rate: float = 0.0,
) -> float | None:
    """ATM implied vol backed out of the ENTRY straddle (Black-76 inversion,
    average of the call and put legs).

    Returns ``None`` if NEITHER leg inverts to a finite positive vol (a thin /
    one-sided entry quote) — sigma_move then cannot fire and is disabled for the
    day rather than firing on a fabricated sigma.
    """
    vols: list[float] = []
    for mark, flag in ((call_mark, "c"), (put_mark, "p")):
        try:
            sigma = kernel.implied_vol(mark, forward, strike, t_years, rate, flag)
        except Exception:  # noqa: BLE001 - py_vollib Below/Above exceptions
            continue
        if math.isfinite(sigma) and sigma > 0.0:
            vols.append(sigma)
    if not vols:
        return None
    return sum(vols) / len(vols)


def _eval_trigger(
    trig: object,
    *,
    es_bar: float,
    es_entry: float,
    net_delta: float,
    pnl_pts: float,
    p_entry: float,
    sigma_bar: float | None,
    multiplier: float,
) -> float | None:
    """Evaluate ONE trigger at a bar; return its ``value`` if it fires, else
    ``None``. Move / delta / sigma are absolute (either direction); pnl is
    directional. ``value`` is in the trigger's own terms (see ``ExitTrigger``).
    """
    if isinstance(trig, UnderlyingMoveTrigger):
        move = abs(es_bar - es_entry)
        thresh = (
            trig.amount
            if trig.unit == "points"
            else trig.amount / 100.0 * es_entry
        )
        if move >= thresh:
            return move if trig.unit == "points" else move / es_entry * 100.0
        return None
    if isinstance(trig, SigmaMoveTrigger):
        if sigma_bar is None or sigma_bar <= 0.0:
            return None
        move = abs(es_bar - es_entry)
        if move >= trig.n * sigma_bar:
            return move / sigma_bar  # realized sigmas
        return None
    if isinstance(trig, NetDeltaTrigger):
        if abs(net_delta) >= trig.threshold:
            return abs(net_delta)
        return None
    if isinstance(trig, PnlTrigger):
        if trig.unit == "points":
            p = pnl_pts
        elif trig.unit == "percent":
            p = pnl_pts / p_entry * 100.0 if p_entry else 0.0
        else:  # "usd"
            p = pnl_pts * multiplier
        if trig.direction == "profit":
            fired = p >= trig.amount
        elif trig.direction == "loss":
            fired = p <= -trig.amount
        else:  # "both"
            fired = abs(p) >= trig.amount
        return p if fired else None
    return None


def find_trigger_fire(
    *,
    triggers: tuple | list,
    es_bars: list[IntradayBar],
    call_marks: list[IntradayBar],
    put_marks: list[IntradayBar],
    on_ts: datetime,
    exit_ts: datetime,
    es_entry: float,
    p_entry: float,
    iv_entry: float | None,
    strike: float,
    expiry_utc: datetime,
    side_sign: int,
    rate: float,
    multiplier: float,
    hedge: HedgeSpec,
    es_tick: float,
    call_fallback: float,
    put_fallback: float,
    kernel: PricingKernel,
) -> ExitTrigger | None:
    """Walk the ES bar cadence over the hold ``(on_ts, exit_ts]`` and return the
    FIRST (type, ts, value) at which ANY enabled trigger fires, else ``None``.

    Reuses the hedge loop's cadence: carried-forward option marks, no
    look-ahead, and a mirror of the hedge accounting (the SAME v4 trigger-OR /
    condition-AND / target helpers as :func:`_run_hedge`) so the ``pnl`` trigger
    sees the hedge MTM realized to each bar. ``sigma_bar`` shrinks intraday
    (``ES_entry * IV_entry * sqrt(T_bar)``). At a single bar the triggers are
    tested in list order; the first to fire wins.

    GROSS vs NET (intentional): the ``pnl`` trigger below fires on
    ``option_mtm_pts`` (mid-mark MTM) + ``cum_hedge_pnl`` — GROSS of
    transaction cost, matching :func:`simulate_day`'s exit-trigger mirror
    design so trigger timing (WHICH bar fires) is identical whether cost is
    on or off. The REALIZED ``DayPnl.total_pnl_pts`` that a caller actually
    books is NET (cost subtracted). This decoupling is deliberate and small
    by default (bounded by the half-spread), but widens when a caller sets a
    large ``fallback_cost_pts``: the trigger can fire at a gross level the net
    P&L would not have reached on its own. Do not "fix" by netting cost into
    this loop — that would make trigger timing cost-dependent and break the
    cost-OFF regression guard's bar-for-bar equivalence.
    """
    if not triggers or es_entry <= 0.0:
        return None

    # Path mirrors ``_run_hedge``: the entry anchor, then interior ES bars, then
    # the exit_ts endpoint (tentative off used only for the rehedge cadence).
    off_bar = _es_at(es_bars, exit_ts)
    off_price = off_bar.price if off_bar else es_entry
    path: list[IntradayBar] = [IntradayBar(ts=on_ts, price=es_entry)]
    for bar in es_bars:
        if on_ts < bar.ts < exit_ts:
            path.append(bar)
    path.append(IntradayBar(ts=exit_ts, price=off_price))

    hedged_qty = 0.0
    last_rehedge_ts = on_ts
    net_delta_last = 0.0
    es_last = es_entry
    cum_hedge_pnl = 0.0  # hedge MTM realized reaching the current point
    # F1.2 running-extremum context is computed only when the gate is armed, so a
    # default (F1.2-off) hedge pays zero overhead and stays bit-identical.
    f12_on = hedge.enabled and hedge.timing.skip_near_extremum.enabled
    for i, es_bar in enumerate(path):
        ts, es_price = es_bar.ts, es_bar.price
        running_high, running_low = (
            _running_extremum(es_bars, ts) if f12_on else (None, None)
        )
        cm = last_known_at_or_before(call_marks, ts)
        pm = last_known_at_or_before(put_marks, ts)
        call_mark = cm.price if cm else call_fallback
        put_mark = pm.price if pm else put_fallback
        t_years = _year_fraction(ts, expiry_utc)
        net_delta = net_straddle_delta(
            kernel, es_price, strike, t_years, call_mark, put_mark, side_sign,
            rate, iv_entry,
        )

        # Evaluate triggers at every bar strictly after entry (window is OPEN at
        # on_ts). cum_hedge_pnl excludes the forward segment, so it is exactly
        # the hedge MTM "so far" at this bar.
        if i > 0:
            # GROSS mid-mark MTM (no cost) — see the docstring note above on
            # why the pnl trigger intentionally fires on gross, not net.
            option_mtm_pts = side_sign * ((call_mark + put_mark) - p_entry)
            pnl_pts = option_mtm_pts + cum_hedge_pnl
            sigma_bar = (
                es_entry * iv_entry * math.sqrt(t_years)
                if iv_entry is not None
                else None
            )
            for trig in triggers:
                val = _eval_trigger(
                    trig,
                    es_bar=es_price,
                    es_entry=es_entry,
                    net_delta=net_delta,
                    pnl_pts=pnl_pts,
                    p_entry=p_entry,
                    sigma_bar=sigma_bar,
                    multiplier=multiplier,
                )
                if val is not None:
                    return ExitTrigger(
                        type=_TRIGGER_TYPE[type(trig)], ts=ts, value=val
                    )

        # Mirror the v4 hedge cadence so cum_hedge_pnl tracks the real hedge via
        # the SAME per-bar step as _run_hedge (single source of truth). The
        # endpoint here is exit_ts (the tentative close for the rehedge cadence).
        if hedge.enabled:
            _executed, hedged_qty, last_rehedge_ts, net_delta_last, es_last = (
                _rehedge_step(
                    hedge,
                    es_bar=es_bar,
                    ts=ts,
                    es_price=es_price,
                    net_delta=net_delta,
                    hedged_qty=hedged_qty,
                    last_rehedge_ts=last_rehedge_ts,
                    net_delta_last=net_delta_last,
                    es_last=es_last,
                    is_entry=(i == 0),
                    endpoint_ts=exit_ts,
                    iv_entry=iv_entry,
                    t_years=t_years,
                    es_tick=es_tick,
                    running_high=running_high,
                    running_low=running_low,
                )
            )
        if i + 1 < len(path) and hedge.enabled:
            next_price = path[i + 1].price
            cum_hedge_pnl += hedged_qty * (next_price - es_price)

    return None


def _triggered_leg_exit(
    marks: list[IntradayBar], fire_ts: datetime, tolerance_minutes: float
) -> tuple[IntradayBar | None, bool]:
    """A TRIGGERED exit BYPASSES exit.conditions: sell each leg at its nearest
    bar within the exit snap tolerance (``met=True``), else the nearest bar at
    all (``met=False`` — a degraded fill). ``bar`` is ``None`` only if the leg
    has no marks (cannot happen once its entry filled)."""
    within = snap_nearest(marks, fire_ts, tolerance_minutes)
    if within is not None:
        return within, True
    return snap_nearest(marks, fire_ts, None), False


# --------------------------------------------------------------------------- #
# Transaction cost — adverse half-spread crossing from the bbba bar (P0.2)
# --------------------------------------------------------------------------- #
def crossing_fill_price(mid: float, half_spread: float, *, is_buy: bool) -> float:
    """Fill price when crossing the spread from the mark ``mid``.

    A BUY lifts the offer: ``mid + half_spread``. A SELL hits the bid:
    ``mid - half_spread``. ``half_spread`` is ``(ask - bid)/2`` (>= 0), so the
    crossing is ALWAYS adverse and reduces P&L. With ``half_spread == 0`` the
    fill is the mid (no cost), either direction.

    ILLUSTRATIVE reference, not wired into the compute path. The real per-fill
    cost accumulation is ``half_spread`` added directly (never reconstructed
    from a fill price): see ``_fill_half_spread`` + the four-fill cost loop in
    :func:`simulate_day` (option legs) and the per-rehedge cost line in
    :func:`_run_hedge` (ES hedge). That is deliberate, not an oversight:
    ``abs((mid + half_spread) - mid) == half_spread`` is NOT float-exact in
    general (empirically false for ~97% of representative float inputs), so
    deriving cost from ``crossing_fill_price(...) - mid`` would risk silently
    perturbing the bit-exact cost-ON / cost-OFF regression values. This helper
    documents and unit-tests the fill-price convention on its own; keep it and
    the summation path in sync by hand if either changes.
    """
    return mid + half_spread if is_buy else mid - half_spread


def _fill_half_spread(bar: IntradayBar, cost: CostModel) -> tuple[float, bool]:
    """Per-unit crossing cost (index points) at *bar* and whether the fixed
    fallback was used.

    Two-sided bar -> ``((ask - bid)/2, False)``. One-sided / last-trade bar
    (``bid`` or ``ask`` ``None``, so the half-spread is UNDEFINED) ->
    ``(fallback_cost_pts, True)`` — never a silent mid fill. The returned cost is
    floored at 0 so a crossed/locked book can never pay the trader.
    """
    if bar.bid is not None and bar.ask is not None:
        return max(0.0, (bar.ask - bar.bid) / 2.0), False
    return max(0.0, cost.fallback_cost_pts), True


# --------------------------------------------------------------------------- #
# Per-day simulation (pure)
# --------------------------------------------------------------------------- #
def _es_at(es_bars: list[IntradayBar], ts: datetime) -> IntradayBar | None:
    """ES bar at ``ts``: last known at/before (no look-ahead), else nearest."""
    return last_known_at_or_before(es_bars, ts) or snap_nearest(es_bars, ts, None)


# --------------------------------------------------------------------------- #
# Hedge module (v4) — trigger-OR / condition-AND / target. Shared by the real
# hedge loop AND the exit-trigger pnl mirror so both agree bar-for-bar.
# --------------------------------------------------------------------------- #
def _target_hedged_qty(hedge: HedgeSpec, net_delta: float) -> float:
    """The ES-future position a rehedge WOULD set, given ``target.mode``.

    * ``zero``      -> ``-net_delta`` (residual 0).
    * ``band_edge`` -> leave ``sign(net_delta)*delta_band`` of delta on
      (``residual = +band`` if net_delta>=0 else ``-band``). Requires delta_band.
    * ``ratio``     -> ``-ratio*net_delta`` (partial; residual = (1-ratio)*net_delta).
    """
    mode = hedge.target.mode
    if mode == "ratio":
        return -hedge.target.ratio * net_delta
    if mode == "band_edge":
        band = hedge.triggers.delta_band or 0.0
        sign = 1.0 if net_delta >= 0.0 else -1.0
        return -net_delta + sign * band
    # "zero" (default)
    return -net_delta


def _running_extremum(
    es_bars: list[IntradayBar], ts: datetime
) -> tuple[float | None, float | None]:
    """Running ``(high, low)`` of the ES session over bars with ``bar.ts <= ts``.

    NO LOOK-AHEAD by construction: only bars AT OR BEFORE ``ts`` contribute, so
    the extremum is the session high/low "so far" (from the fetched session start
    — anchored at ~09:30 — up to and INCLUDING the current bar), never a value set
    by a later bar. Assumes ``es_bars`` is ascending in ``ts`` (the reader returns
    them sorted) and breaks at the first bar strictly after ``ts``. Returns
    ``(None, None)`` when no bar is at or before ``ts``.
    """
    hi = -math.inf
    lo = math.inf
    seen = False
    for bar in es_bars:
        if bar.ts <= ts:
            if bar.price > hi:
                hi = bar.price
            if bar.price < lo:
                lo = bar.price
            seen = True
        else:
            break
    if not seen:
        return None, None
    return hi, lo


def _hedge_time_gate_ok(
    timing: HedgeTimingSpec, ts: datetime, close_ts: datetime
) -> bool:
    """F1.1 time-anchored gate. ``True`` => (re)hedging is allowed at ``ts``.

    When ``only_within_minutes_before_close`` is set, a (re)hedge is only
    considered in the final N minutes before ``close_ts`` (the hedge-window
    close); earlier bars are suppressed. ``None`` (default) => always allowed
    (no time restriction — baseline behavior).
    """
    n = timing.only_within_minutes_before_close
    if n is None:
        return True
    return (close_ts - ts).total_seconds() <= n * 60.0


def _skip_near_extremum(
    timing: HedgeTimingSpec,
    *,
    ts: datetime,
    close_ts: datetime,
    es_price: float,
    running_high: float | None,
    running_low: float | None,
    delta_change: float,
) -> bool:
    """F1.2 gate. ``True`` => SUPPRESS this hedge (buy-high / sell-low skip).

    Active only in the final ``window_minutes`` before ``close_ts``. Suppresses a
    BUY (``delta_change > 0``) when ES is within ``tolerance`` of the RUNNING
    session HIGH, and a SELL (``delta_change < 0``) when within ``tolerance`` of
    the RUNNING session LOW. A zero-size trade has no direction and is never
    suppressed. Fails OPEN (does not suppress) when the running extremum is
    unavailable — the hedge is never blocked on missing session context.
    """
    spec = timing.skip_near_extremum
    if not spec.enabled:
        return False
    if (close_ts - ts).total_seconds() > spec.window_minutes * 60.0:
        return False  # outside the late-session window
    if running_high is None or running_low is None:
        return False  # no session context -> cannot evaluate -> do not suppress
    tol = spec.tolerance
    if spec.tolerance_unit == "percent":
        tol = tol / 100.0 * es_price
    if delta_change > 0.0:  # a BUY of ES
        return (running_high - es_price) <= tol
    if delta_change < 0.0:  # a SELL of ES
        return (es_price - running_low) <= tol
    return False


def _hedge_considered(
    hedge: HedgeSpec,
    *,
    ts: datetime,
    last_rehedge_ts: datetime,
    net_delta: float,
    net_delta_last: float,
    es_price: float,
    es_last: float,
    sigma_bar: float | None,
) -> bool:
    """OR of the enabled rehedge triggers (DESIGN v4). ``net_delta_last`` /
    ``es_last`` are the net delta / ES price captured at the LAST executed hedge
    (``sigma_bar`` uses ``es_last`` as its reference)."""
    trig = hedge.triggers
    if trig.interval_minutes:  # None or 0 -> off
        if (ts - last_rehedge_ts).total_seconds() >= trig.interval_minutes * 60.0:
            return True
    if trig.delta_band is not None:  # |delta drift since last hedge|
        if abs(net_delta - net_delta_last) >= trig.delta_band:
            return True
    sm = trig.sigma_move
    if sm.enabled and sigma_bar is not None and sigma_bar > 0.0:
        if abs(es_price - es_last) >= sm.n * sigma_bar:
            return True
    return False


def _hedge_conditions_pass(
    conditions: tuple | list,
    es_bar: IntradayBar,
    delta_to_remove: float,
    es_tick: float,
) -> bool:
    """AND of the hedge execution conditions on the ES-FUTURE bar. A considered
    rehedge that fails ANY condition is DEFERRED. ``max_spread`` /
    ``min_quote_size`` REQUIRE a two-sided ES quote (bid/ask/sizes present); a
    trade-only ES bar fails them. ``min_rehedge_delta`` gates on the size of the
    ES trade the rehedge would make (``delta_to_remove``)."""
    for c in conditions:
        if isinstance(c, MaxSpreadCond):
            if es_bar.bid is None or es_bar.ask is None:
                return False
            spread = es_bar.ask - es_bar.bid
            floor = max(c.pct / 100.0 * es_bar.price, c.min_ticks * es_tick)
            if spread > floor:
                return False
        elif isinstance(c, MinQuoteSizeCond):
            if es_bar.bid_size is None or es_bar.ask_size is None:
                return False
            if not (es_bar.bid_size >= c.size and es_bar.ask_size >= c.size):
                return False
        elif isinstance(c, MinRehedgeDeltaCond):
            if abs(delta_to_remove) < c.threshold:
                return False
    return True


def _rehedge_step(
    hedge: HedgeSpec,
    *,
    es_bar: IntradayBar,
    ts: datetime,
    es_price: float,
    net_delta: float,
    hedged_qty: float,
    last_rehedge_ts: datetime,
    net_delta_last: float,
    es_last: float,
    is_entry: bool,
    endpoint_ts: datetime,
    iv_entry: float | None,
    t_years: float,
    es_tick: float,
    running_high: float | None = None,
    running_low: float | None = None,
) -> tuple[bool, float, datetime, float, float]:
    """One bar of the v4 rehedge cadence: decide whether to (re)hedge and return
    the resulting state. SHARED by :func:`_run_hedge` (the realized hedge) and
    :func:`find_trigger_fire` (the pnl-trigger mirror) so both stay in lockstep
    bar-for-bar — a single source of truth for the trigger-OR / condition-AND /
    target machinery.

    The entry bar (``is_entry``) establishes the hedge UNCONDITIONALLY; an
    interior bar strictly before ``endpoint_ts`` CONSIDERS a rehedge if ANY
    trigger fires (OR) and EXECUTES only if ALL conditions pass on ``es_bar``
    (AND); ``endpoint_ts`` itself never rehedges (the close happens there).
    ``sigma_bar`` uses ``es_last`` (the ES at the last hedge) as its reference.
    Returns ``(executed, hedged_qty, last_rehedge_ts, net_delta_last, es_last)``
    — the trailing four unchanged when no rehedge executes.

    Session-relative hedge-timing gates (both neutral by default, so a HedgeSpec
    with a default ``timing`` behaves bit-identically to before):

    * F1.1 ``only_within_minutes_before_close`` — an AND-gate applied FIRST to
      EVERY decision (entry included): outside the final-N-minutes window nothing
      hedges (``endpoint_ts`` is the hedge-window close).
    * F1.2 ``skip_near_extremum`` — suppresses an otherwise-executing directional
      hedge that would BUY near the running session high / SELL near the running
      low in the late window. ``running_high`` / ``running_low`` are the running
      extrema at/before ``ts`` (no look-ahead); the caller passes them only when
      F1.2 is enabled, else ``None`` (the gate is inert).
    """
    timing = hedge.timing
    # F1.1: no hedging outside the final-N-minutes window (applies to the
    # establishing entry hedge too, so "hedge only near the close" truly holds).
    if not _hedge_time_gate_ok(timing, ts, endpoint_ts):
        return False, hedged_qty, last_rehedge_ts, net_delta_last, es_last

    execute = False
    target_qty = hedged_qty
    if is_entry:
        execute = True  # establish the hedge (unconditional, modulo the gates)
        target_qty = _target_hedged_qty(hedge, net_delta)
    elif ts < endpoint_ts:  # never rehedge exactly at the endpoint (closing there)
        sigma_bar = (
            es_last * iv_entry * math.sqrt(t_years) if iv_entry is not None else None
        )
        if _hedge_considered(
            hedge,
            ts=ts,
            last_rehedge_ts=last_rehedge_ts,
            net_delta=net_delta,
            net_delta_last=net_delta_last,
            es_price=es_price,
            es_last=es_last,
            sigma_bar=sigma_bar,
        ):
            target_qty = _target_hedged_qty(hedge, net_delta)
            if _hedge_conditions_pass(
                hedge.conditions, es_bar, target_qty - hedged_qty, es_tick
            ):
                execute = True

    # F1.2: suppress a directional buy-high / sell-low hedge in the late window.
    if execute and _skip_near_extremum(
        timing,
        ts=ts,
        close_ts=endpoint_ts,
        es_price=es_price,
        running_high=running_high,
        running_low=running_low,
        delta_change=target_qty - hedged_qty,
    ):
        execute = False

    if execute:
        return True, target_qty, ts, net_delta, es_price
    return False, hedged_qty, last_rehedge_ts, net_delta_last, es_last


def _run_hedge(
    *,
    es_bars: list[IntradayBar],
    call_marks: list[IntradayBar],
    put_marks: list[IntradayBar],
    on_ts: datetime,
    off_ts: datetime,
    on_price: float,
    off_price: float,
    on_bar: IntradayBar | None,
    strike: float,
    expiry_utc: datetime,
    side_sign: int,
    hedge: HedgeSpec,
    iv_entry: float | None,
    es_tick: float,
    rate: float,
    call_fallback: float,
    put_fallback: float,
    kernel: PricingKernel,
    cost: CostModel = COST_DISABLED,
) -> tuple[list[HedgeTrade], float, float, int]:
    """Delta-hedge the COMBINED net delta over the BOTH-ON window
    ``[on_ts, off_ts]`` (single-leg legging windows are UNHEDGED, per DESIGN).

    v4 module: the entry hedge at ``on_ts`` establishes the position
    unconditionally (applying ``target``); each interior ES bar CONSIDERS a
    rehedge if ANY trigger fires (OR) and EXECUTES only if ALL conditions pass on
    that ES bar (AND) — else it DEFERS.

    When ``cost`` is enabled, each EXECUTED rehedge pays an adverse half-spread on
    the ES trade it makes: ``|delta position change| * half_spread`` where the
    half-spread is read from the EXECUTING ES bar (the real interior bar, or
    ``on_bar`` — the real ES bar at the entry — for the establishing hedge; the
    synthetic path anchors are never used for cost). A one-sided ES bar charges
    the fixed fallback per unit and counts as one fallback trade. Returns
    ``(hedge_trades, hedge_pnl_pts, hedge_cost_pts, n_fallback_trades)``.
    """
    hedge_trades: list[HedgeTrade] = []
    hedge_pnl_pts = 0.0
    hedge_cost_pts = 0.0
    n_fallback = 0

    # Interior points carry their real ES bar (with quote fields for conditions);
    # the on/off anchors are synthetic price-only endpoints (entry is
    # unconditional; off never rehedges — both bypass conditions). ``on_bar`` is
    # the REAL ES bar at the entry, used only to cost the establishing hedge at a
    # true spread rather than the quote-less synthetic anchor.
    path: list[IntradayBar] = [IntradayBar(ts=on_ts, price=on_price)]
    for bar in es_bars:
        if on_ts < bar.ts < off_ts:
            path.append(bar)
    path.append(IntradayBar(ts=off_ts, price=off_price))

    hedged_qty = 0.0
    last_rehedge_ts = on_ts
    net_delta_last = 0.0
    es_last = on_price
    # F1.2 running-extremum context is computed only when the gate is armed, so a
    # default (F1.2-off) hedge pays zero overhead and stays bit-identical.
    f12_on = hedge.enabled and hedge.timing.skip_near_extremum.enabled
    for i, bar in enumerate(path):
        ts, es_price = bar.ts, bar.price
        running_high, running_low = (
            _running_extremum(es_bars, ts) if f12_on else (None, None)
        )
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
            iv_entry,
        )
        prev_qty = hedged_qty
        executed, hedged_qty, last_rehedge_ts, net_delta_last, es_last = _rehedge_step(
            hedge,
            es_bar=bar,
            ts=ts,
            es_price=es_price,
            net_delta=net_delta,
            hedged_qty=hedged_qty,
            last_rehedge_ts=last_rehedge_ts,
            net_delta_last=net_delta_last,
            es_last=es_last,
            is_entry=(i == 0),
            endpoint_ts=off_ts,
            iv_entry=iv_entry,
            t_years=t_years,
            es_tick=es_tick,
            running_high=running_high,
            running_low=running_low,
        )
        if executed:
            hedge_trades.append(
                HedgeTrade(
                    ts=ts,
                    underlying=es_price,
                    net_delta=net_delta,
                    hedge_qty=hedged_qty,
                )
            )
            if cost.enabled:
                # Cost the ES trade at the executing bar's spread. The entry hedge
                # (synthetic anchor) uses the real ``on_bar`` quote when supplied.
                fill_bar = on_bar if (i == 0 and on_bar is not None) else bar
                half, used_fb = _fill_half_spread(fill_bar, cost)
                hedge_cost_pts += abs(hedged_qty - prev_qty) * half
                if used_fb:
                    n_fallback += 1
        if i + 1 < len(path):
            next_price = path[i + 1].price
            hedge_pnl_pts += hedged_qty * (next_price - es_price)

    return hedge_trades, hedge_pnl_pts, hedge_cost_pts, n_fallback


def _use_settlement(
    exit_mode: str,
    expiry: date,
    date_int: int,
    exit_trigger: ExitTrigger | None,
    settlement_price: float | None,
) -> bool:
    """True iff the day exits at the front-future SETTLEMENT intrinsic
    ``|F_settle - K|`` (Gap 1).

    Requires: mode != ``"quote"``, a settlement value present, NO early trigger
    fired, AND the option EXPIRES on the trade day (0DTE held to settlement).
    ``"settlement"`` and ``"auto"`` behave identically here — the only gate is
    0DTE-held; the exact exit clock is ignored because ``|F-K|`` is
    time-independent (resolved design Q4). ``settlement_price is None`` (an
    upstream fetch gap) falls back to quote — never fabricate a settle level."""
    if exit_mode == "quote" or settlement_price is None or exit_trigger is not None:
        return False
    day = date(date_int // 10000, (date_int // 100) % 100, date_int % 100)
    if expiry != day:
        return False
    return exit_mode in ("settlement", "auto")


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
    exit_triggers: tuple | list = (),
    tick_size: float = ES_OPTION_TICK_SIZE,
    hedge: HedgeSpec | None = None,
    es_tick: float = ES_FUTURE_TICK_SIZE,
    es_day_open: float | None = None,
    multiplier: float = ES_MULTIPLIER,
    rate: float = 0.0,
    tz: str = _ET,
    kernel: PricingKernel | None = None,
    cost: CostModel | None = None,
    exit_mode: str = "quote",
    settlement_price: float | None = None,
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
    hedge = hedge or _HEDGE_DISABLED
    cost = cost or COST_DISABLED
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

    # Both-on window opens at the LATER of the two independent leg entries.
    straddle_on_ts = max(c_entry.ts, p_entry.ts)
    # Rule 7 (Gap 2, ALWAYS-ON): re-value BOTH legs at the common instant
    # T=straddle_on_ts, each at its LAST clean qualifying quote <= T, so the
    # earlier leg is not carried at a stale price. Done BEFORE iv/trigger/entry
    # snapshot so every downstream computation uses the synced entry prices. The
    # leg that defines T re-marks to itself; the leg that defines T never moves
    # so straddle_on_ts stays consistent.
    c_entry = _resync_leg_to_T(
        call_marks, es_bars, straddle_on_ts,
        entry_leg_conds, entry_move, es_ref, tick_size,
    ) or c_entry
    p_entry = _resync_leg_to_T(
        put_marks, es_bars, straddle_on_ts,
        entry_leg_conds, entry_move, es_ref, tick_size,
    ) or p_entry
    es_on = _es_at(es_bars, straddle_on_ts)
    expiry_utc = _expiry_utc(expiry, tz)

    # ATM entry IV (Black-76 inversion of the entry straddle) — shared by the
    # exit sigma_move trigger AND the hedge sigma_move trigger. Computed once,
    # only when either needs it (thin/one-sided entry -> None -> sigma disabled).
    hedge_sigma_on = hedge.enabled and hedge.triggers.sigma_move.enabled
    iv_entry: float | None = None
    if (exit_triggers or hedge_sigma_on) and es_on is not None:
        t_entry = _year_fraction(straddle_on_ts, expiry_utc)
        iv_entry = entry_atm_iv(
            kernel, es_on.price, strike, t_entry,
            c_entry.price, p_entry.price, rate,
        )

    # --- Early-exit TRIGGERS (v3): may close the straddle before exit.time #
    exit_trigger: ExitTrigger | None = None
    if exit_triggers and es_on is not None and straddle_on_ts < exit_ts:
        p_entry_prem = c_entry.price + p_entry.price
        exit_trigger = find_trigger_fire(
            triggers=exit_triggers,
            es_bars=es_bars,
            call_marks=call_marks,
            put_marks=put_marks,
            on_ts=straddle_on_ts,
            exit_ts=exit_ts,
            es_entry=es_on.price,
            p_entry=p_entry_prem,
            iv_entry=iv_entry,
            strike=strike,
            expiry_utc=expiry_utc,
            side_sign=side_sign,
            rate=rate,
            multiplier=multiplier,
            hedge=hedge,
            es_tick=es_tick,
            call_fallback=c_entry.price,
            put_fallback=p_entry.price,
            kernel=kernel,
        )

    # --- INDEPENDENT per-leg EXIT (must close) -------------------------- #
    if exit_trigger is not None:
        # A TRIGGERED exit closes the WHOLE straddle at the trigger time, each
        # leg at its nearest available bar (snap tolerance) else nearest
        # fallback — BYPASSING exit.conditions (stop/target: exit regardless).
        c_exit, c_met = _triggered_leg_exit(call_marks, exit_trigger.ts, exit_tol)
        p_exit, p_met = _triggered_leg_exit(put_marks, exit_trigger.ts, exit_tol)
    else:
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

    # --- Settlement-intrinsic exit |F_settle - K| (Gap 1) --------------- #
    # On a 0DTE held-to-settlement day (mode settlement/auto, a settle level
    # present, no early trigger) BOTH legs close at their cash-settlement
    # intrinsic against the front-future settle F, at the expiry instant. This
    # REPLACES the quote exit; there is no exit fill (handled in the cost loop).
    settlement_exit = _use_settlement(
        exit_mode, expiry, date_int, exit_trigger, settlement_price
    )
    if settlement_exit:
        f_settle = settlement_price
        settle_ts = expiry_utc
        c_exit = IntradayBar(ts=settle_ts, price=max(f_settle - strike, 0.0))
        p_exit = IntradayBar(ts=settle_ts, price=max(strike - f_settle, 0.0))
        c_met = p_met = True

    # --- Per-leg P&L (points, side-signed) ------------------------------ #
    call_pnl = side_sign * (c_exit.price - c_entry.price)
    put_pnl = side_sign * (p_exit.price - p_entry.price)
    option_pnl_pts = call_pnl + put_pnl

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
    es_off = _es_at(es_bars, straddle_off_ts)
    entry_snap = MarkSnapshot(
        straddle_on_ts,
        es_on.price if es_on else float("nan"),
        c_entry.price, p_entry.price, c_entry.price + p_entry.price,
    )
    # Under a settlement exit the exit underlying IS the settle level F (the
    # option cash-settles against it), not a carried-forward intraday ES mark.
    exit_underlying = (
        settlement_price if settlement_exit
        else (es_off.price if es_off else float("nan"))
    )
    exit_snap = MarkSnapshot(
        straddle_off_ts,
        exit_underlying,
        c_exit.price, p_exit.price, c_exit.price + p_exit.price,
    )

    # --- Delta hedge over the BOTH-ON window ---------------------------- #
    hedge_trades: list[HedgeTrade] = []
    hedge_pnl_pts = 0.0
    hedge_cost_pts = 0.0
    n_hedge_fallback = 0
    if hedge.enabled and straddle_off_ts > straddle_on_ts and es_on and es_off:
        hedge_trades, hedge_pnl_pts, hedge_cost_pts, n_hedge_fallback = _run_hedge(
            es_bars=es_bars,
            call_marks=call_marks,
            put_marks=put_marks,
            on_ts=straddle_on_ts,
            off_ts=straddle_off_ts,
            on_price=es_on.price,
            off_price=es_off.price,
            on_bar=es_on,
            strike=strike,
            expiry_utc=expiry_utc,
            side_sign=side_sign,
            hedge=hedge,
            iv_entry=iv_entry,
            es_tick=es_tick,
            rate=rate,
            call_fallback=c_entry.price,
            put_fallback=p_entry.price,
            kernel=kernel,
            cost=cost,
        )

    # --- Transaction cost on the four option fills (adverse half-spread) --- #
    # Charged per fill regardless of side: a long entry buys / a short entry
    # sells — either way crossing the spread reduces P&L by the half-spread.
    option_cost_pts = 0.0
    n_option_fallback = 0
    if cost.enabled:
        # A settlement exit is NOT a fill (no spread crossed at cash settlement),
        # so only the two ENTRY fills are charged — removing the exit-leg
        # double-charge that a quote exit would incur (Gap 1).
        fills = (
            (c_entry, p_entry)
            if settlement_exit
            else (c_entry, p_entry, c_exit, p_exit)
        )
        for fill_bar in fills:
            half, used_fb = _fill_half_spread(fill_bar, cost)
            option_cost_pts += half
            if used_fb:
                n_option_fallback += 1

    cost_pts = option_cost_pts + hedge_cost_pts
    n_fallback_fills = n_option_fallback + n_hedge_fallback
    total_pnl_pts = option_pnl_pts + hedge_pnl_pts - cost_pts
    pnl = DayPnl(
        option_pnl_pts=option_pnl_pts,
        hedge_pnl_pts=hedge_pnl_pts,
        total_pnl_pts=total_pnl_pts,
        total_pnl_usd=total_pnl_pts * multiplier,
        cost_pts=cost_pts,
        cost_usd=cost_pts * multiplier,
        n_fallback_fills=n_fallback_fills,
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
        exit_trigger=exit_trigger,
    )


# --------------------------------------------------------------------------- #
# Laddered multi-entry aggregation (F4.1, pure)
# --------------------------------------------------------------------------- #
def aggregate_ladder_day(
    date_int: int,
    entries: list[LadderEntry],
    *,
    multiplier: float = ES_MULTIPLIER,
    expiry: date | None = None,
) -> DayResult:
    """Fold a day's independent laddered rungs into ONE day-aggregate DayResult.

    Each rung's ``result.pnl`` is per ONE contract; ``entry.contracts`` is the
    rung's sizing weight. The day's dollar P&L is the WEIGHTED SUM of the rungs
    that traded (``sum(contracts_i * pnl_i.total_pnl_usd)``) — unambiguous and
    order-free. The component points/usd (option/hedge/cost) are summed the same
    way and points are re-expressed via the multiplier so the DayPnl invariants
    hold (``usd == pts * multiplier`` and ``total == option + hedge - cost``);
    ``n_fallback_fills`` is a raw event COUNT so it is summed unweighted.

    The aggregate row keeps the one-row-per-day shape the weekday/regime/event
    views rely on (``date``/``status``/``pnl``/regime) and carries the rungs in
    ``entries`` for the per-rung readout. Straddle-level fields (entry/exit/legs/
    hedge_trades/strike) stay unset on the aggregate — that detail lives on each
    child. ``status`` is ``ok`` if ANY rung traded, else ``skipped`` with the
    first rung's skip reason (or ``no_ladder_fills`` when there are no rungs).
    """
    traded = [e for e in entries if e.result.pnl is not None]
    if not traded:
        reason = "no_ladder_fills"
        for e in entries:
            if e.result.skip_reason:
                reason = e.result.skip_reason
                break
        return DayResult(
            date=date_int,
            status="skipped",
            skip_reason=reason,
            expiry=expiry,
            entries=tuple(entries),
        )

    option_usd = 0.0
    hedge_usd = 0.0
    cost_usd = 0.0
    n_fallback = 0
    for e in traded:
        w = e.contracts
        p = e.result.pnl
        option_usd += w * p.option_pnl_pts * multiplier
        hedge_usd += w * p.hedge_pnl_pts * multiplier
        cost_usd += w * p.cost_pts * multiplier
        n_fallback += p.n_fallback_fills
    total_usd = option_usd + hedge_usd - cost_usd

    inv_m = 1.0 / multiplier if multiplier else 0.0
    pnl = DayPnl(
        option_pnl_pts=option_usd * inv_m,
        hedge_pnl_pts=hedge_usd * inv_m,
        total_pnl_pts=total_usd * inv_m,
        total_pnl_usd=total_usd,
        cost_pts=cost_usd * inv_m,
        cost_usd=cost_usd,
        n_fallback_fills=n_fallback,
    )
    return DayResult(
        date=date_int,
        status="ok",
        skip_reason=None,
        expiry=expiry,
        pnl=pnl,
        entries=tuple(entries),
    )


# --------------------------------------------------------------------------- #
# Aggregation (pure)
# --------------------------------------------------------------------------- #
def _native_return_metrics(returns: np.ndarray) -> dict[str, float | int]:
    """Native %-NAV metrics on the daily-return series (Gap 3), numpy-only (no
    scipy in the pure engine). ``returns`` = ``total_pnl_pts / entry.underlying``
    per traded day. Formulas mirror ``w3_core.py`` / ``w3_yann_basis.py``:

    * ``pct_return_year`` — compounded ``nav_final**(252/n) - 1``.
    * ``ann_vol`` — ``std(r, ddof=1) * sqrt(252)``.
    * ``return_sharpe`` — ``mean(r)/std(r, ddof=1) * sqrt(252)`` (no rf).
    * ``max_drawdown_pct`` — ``min(nav/cummax(nav) - 1)`` on ``nav = cumprod(1+r)``.
    * ``return_skew`` — bias-corrected G1 (``sqrt(n(n-1))/(n-2) * m3/m2**1.5``),
      identical to ``scipy.stats.skew(bias=False)``.
    An empty series returns the neutral defaults (nav_final=1.0, zeros)."""
    n = int(returns.size)
    if n == 0:
        return {
            "n_return_days": 0, "mean_daily_return": 0.0, "pct_return_year": 0.0,
            "nav_final": 1.0, "ann_vol": 0.0, "return_sharpe": 0.0,
            "max_drawdown_pct": 0.0, "median_daily_return": 0.0, "return_skew": 0.0,
        }
    nav = np.cumprod(1.0 + returns)
    nav_final = float(nav[-1])
    mean_r = float(returns.mean())
    pct_year = nav_final ** (_TRADING_DAYS_PER_YEAR / n) - 1.0
    if n > 1:
        std = float(np.std(returns, ddof=1))
        ann_vol = std * math.sqrt(_TRADING_DAYS_PER_YEAR)
        sharpe = (
            mean_r / std * math.sqrt(_TRADING_DAYS_PER_YEAR) if std > 0 else 0.0
        )
    else:
        ann_vol = 0.0
        sharpe = 0.0
    max_dd = float((nav / np.maximum.accumulate(nav) - 1.0).min())
    median_r = float(np.median(returns))
    if n > 2:
        d = returns - mean_r
        m2 = float((d ** 2).mean())
        m3 = float((d ** 3).mean())
        g1 = m3 / m2 ** 1.5 if m2 > 0 else 0.0
        skew = math.sqrt(n * (n - 1)) / (n - 2) * g1
    else:
        skew = 0.0
    return {
        "n_return_days": n, "mean_daily_return": mean_r, "pct_return_year": pct_year,
        "nav_final": nav_final, "ann_vol": ann_vol, "return_sharpe": sharpe,
        "max_drawdown_pct": max_dd, "median_daily_return": median_r,
        "return_skew": skew,
    }


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

    # Transaction-cost coverage across traded days (0 when the model is off).
    total_cost_usd = float(sum(r.pnl.cost_usd for r in traded))
    n_fallback_fills = int(sum(r.pnl.n_fallback_fills for r in traded))

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

    # Native %-NAV metrics (Gap 3) on the Yann-basis daily-return series
    # r = total_pnl_pts / entry.underlying. Only traded days that carry an
    # ``entry`` with a positive underlying contribute; ladder-aggregate days
    # (entry is None) are excluded from the return series (Q3) though they still
    # count in the USD aggregation above.
    returns = np.array(
        [
            r.pnl.total_pnl_pts / r.entry.underlying
            for r in traded
            if r.entry is not None and r.entry.underlying > 0
        ],
        dtype=np.float64,
    )
    native = _native_return_metrics(returns)

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
        total_cost_usd=total_cost_usd,
        n_fallback_fills=n_fallback_fills,
        n_return_days=int(native["n_return_days"]),
        mean_daily_return=native["mean_daily_return"],
        pct_return_year=native["pct_return_year"],
        nav_final=native["nav_final"],
        ann_vol=native["ann_vol"],
        return_sharpe=native["return_sharpe"],
        max_drawdown_pct=native["max_drawdown_pct"],
        median_daily_return=native["median_daily_return"],
        return_skew=native["return_skew"],
    )
