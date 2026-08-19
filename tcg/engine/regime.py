"""Pure vol-regime SIGNAL computation for the intraday long-gamma page (F2.1).

This module computes REALIZED-VOLATILITY signals (RV H20/H30/H100) from a daily
close series and NOTHING ELSE — it deliberately carries NO side-decision, NO
threshold, and NO regime->action logic (that is the SEPARATE later F2.2 task).
It is a PURE, dependency-free part of :mod:`tcg.engine`: it imports only stdlib +
NumPy and NEVER touches :mod:`tcg.data` (the module-boundary contract is
``types <- data/engine <- core`` with data/engine independent). The FETCH of the
daily closes — via the P0.3 :class:`tcg.data._sql.daily_series.DailySeriesReader`
seam — and the join with VVIX/VIX1D passthrough happen one layer up, in
:mod:`tcg.core.api.intraday_backtest`, which then feeds these signals to the sim.

Realized-volatility convention (documented exactly)
---------------------------------------------------
Given a close series ``C[0..n-1]`` ordered ASCENDING by trade date:

1. Daily LOG returns ``r[k] = ln(C[k+1] / C[k])`` (``n-1`` of them; ``r[k]`` is
   the return realized AT price index ``k+1``).
2. Rolling realized vol at price index ``i`` for a ``window`` of ``w`` trading
   days is the SAMPLE standard deviation (``ddof=1``, i.e. divide by ``w-1``) of
   the trailing ``w`` log returns ending at ``i`` — the returns ``r[i-w .. i-1]``
   — ANNUALIZED by ``sqrt(annualization)`` (``annualization=252`` trading days).
3. NO-LOOK-AHEAD: RV at date ``i`` uses only closes up to and including ``i``.
4. INSUFFICIENT HISTORY: the first ``w`` price points (indices ``0..w-1``) have
   fewer than ``w`` trailing returns, so their RV is ``None`` — a null, NEVER a
   fabricated value.
5. A CONSTANT series has all-zero log returns => RV ``0.0`` (not null) once there
   is enough history.

Both functions are fully deterministic and unit-testable WITHOUT a database.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np


def rolling_realized_vol(
    values: Sequence[float],
    window: int,
    *,
    annualization: int = 252,
) -> list[float | None]:
    """Trailing-``window`` annualized realized vol of a daily close series.

    Parameters
    ----------
    values : Sequence[float]
        Daily closes ordered ASCENDING by date (the caller — the P0.3 reader —
        already returns ascending, NULL-dropped floats). Must be strictly
        positive (a log return is taken); a non-positive close is a data fault
        the reader would not emit and is not silently masked here.
    window : int
        Number of trailing trading-day RETURNS in the estimate (>= 2; a sample
        std of one return is undefined, so ``window < 2`` raises loudly rather
        than returning a silent NaN).
    annualization : int
        Trading days per year for the ``sqrt`` annualization (default 252).

    Returns
    -------
    list[float | None]
        One entry PER input close, aligned to ``values`` (same length/order).
        Indices ``0..window-1`` are ``None`` (insufficient history); index ``i
        >= window`` is the annualized sample std of ``r[i-window .. i-1]``.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2 (realized-vol needs >=2 returns); got {window}")

    n = len(values)
    out: list[float | None] = [None] * n
    if n < window + 1:
        # Fewer than ``window`` returns anywhere in the series -> all None.
        return out

    arr = np.asarray(values, dtype=float)
    if not np.all(arr > 0.0):
        raise ValueError("realized vol requires strictly positive closes (log return)")

    log_ret = np.diff(np.log(arr))  # length n-1; log_ret[k] realized at index k+1
    ann = math.sqrt(float(annualization))

    # RV at price index i (i >= window) = sample-std of the window returns
    # log_ret[i-window .. i-1] (w values), annualized.
    for i in range(window, n):
        window_returns = log_ret[i - window : i]
        rv = float(np.std(window_returns, ddof=1)) * ann
        out[i] = rv
    return out


def realized_vol_by_date(
    dates: Sequence[int],
    values: Sequence[float],
    windows: Sequence[int],
    *,
    annualization: int = 252,
) -> dict[int, dict[str, float | None]]:
    """Per-date realized-vol signal map ``{date_int: {"h<w>": rv | None}}``.

    Pure join of :func:`rolling_realized_vol` over each requested ``window`` onto
    the parallel ``dates``. The signal KEY for a window ``w`` is ``f"h{w}"`` (so
    ``[20, 30, 100]`` -> keys ``h20``/``h30``/``h100``) — the exact per-day field
    names the intraday response surfaces. A date with insufficient history for a
    given window carries ``None`` for that key (never a fabricated number).

    ``dates`` and ``values`` MUST be equal length and ascending-by-date (the P0.3
    reader guarantees this); an empty series yields an empty map.
    """
    if len(dates) != len(values):
        raise ValueError(
            f"dates/values length mismatch: {len(dates)} != {len(values)}"
        )
    result: dict[int, dict[str, float | None]] = {int(d): {} for d in dates}
    for w in windows:
        rv = rolling_realized_vol(values, w, annualization=annualization)
        key = f"h{w}"
        for d, v in zip(dates, rv):
            result[int(d)][key] = v
    return result


# =========================================================================== #
# F2.2 — PURE regime -> SIDE decision layer.
#
# This is the SPECIFIC, minimal rule that turns a per-day signal bundle
# (RV H20/H30/H100 + optional level signals such as VVIX / VIX1D) into a
# {long, short, flat} straddle side. It is deliberately NOT a general signal
# framework (that is the separate Signals page): a fixed backwardation ladder,
# an absolute low-vol floor, and an ordered list of generic LEVEL gates — no
# more. Everything here is pure, deterministic and DB-free (unit-testable
# without dwh); the FETCH of the signals + the per-day side plumbing live one
# layer up in :mod:`tcg.core.api.intraday_backtest` (engine never imports data).
#
# The decision cascade (highest precedence first):
#   1. MISSING RV input (any of the RV ladder keys is ``None``) -> we cannot
#      classify the regime -> state ``"fallback"``, trade the STATIC run-level
#      side (NEVER a silent skip). Gates are not consulted (no regime to gate).
#   2. EXTREMELY-LOW floor: an ABSOLUTE veto. If the short-window RV (the first
#      ``rv_keys`` entry) is below ``extremely_low_h20`` (> 0 to be enabled;
#      0.0 disables it) -> state ``"extremely_low"``, side ``"flat"``. This
#      precedes even a backwardated ladder.
#   3. BASE regime: the strict backwardation ladder RV[0] > RV[1] > RV[2] (with
#      an optional multiplicative tolerance) -> HVOL-ON -> ``"long"``; anything
#      else -> HVOL-OFF -> ``"short"``.
#   4. LEVEL GATES: an ordered tuple of :class:`LevelGateSpec`; each ENABLED gate
#      whose (non-``None``) signal value is strictly ABOVE its threshold applies
#      its ``action`` (veto to ``flat`` OR force ``long``/``short``). Later gates
#      override earlier ones. The ``state`` still records the underlying regime;
#      ``gate`` records which gate last fired (else ``None``). A ``None`` signal
#      value NEVER fires. Adding a new bucket (VIX1D, F2.3) is a new gate entry
#      in the list — same evaluator, NO signature change.
# =========================================================================== #

_SIDES = ("long", "short", "flat")


@dataclass(frozen=True)
class LevelGateSpec:
    """A generic single-signal LEVEL gate (VVIX now, VIX1D later — F2.3).

    ``enabled`` off makes the gate inert. When on, the gate reads ``signal`` from
    the per-day bundle; if that value is present (not ``None``) and strictly
    greater than ``above``, the gate fires and forces ``action`` (one of
    ``long`` / ``short`` / ``flat``). A list of gates is evaluated in order and a
    later gate overrides an earlier one — so a new VIX1D bucket slots in with no
    rework and no change to :func:`decide_regime_side`'s signature.
    """

    enabled: bool
    signal: str
    above: float
    action: str  # "long" | "short" | "flat"


@dataclass(frozen=True)
class Decision:
    """The resolved per-day regime decision (pure, DB-free).

    ``side`` is the straddle side to trade (``flat`` => skip the day like a
    ``custom_days`` exclude). ``state`` records WHY: ``hvol_on`` (backwardated
    ladder), ``hvol_off`` (complement), ``extremely_low`` (absolute floor veto),
    or ``fallback`` (missing RV input => trade the static side). ``gate`` is the
    name of the level gate that last vetoed/adjusted the base side, else
    ``None``. ``asof`` (set by :func:`resolve_regime_decisions`) is the signal
    date the decision was taken as-of (strictly before the trade day), or
    ``None`` when no prior signal existed. ``signals`` is the exact signal bundle
    the decision consumed (all-``None`` on a no-prior fallback).
    """

    side: str  # "long" | "short" | "flat"
    state: str  # "hvol_on" | "hvol_off" | "extremely_low" | "fallback"
    gate: str | None = None
    asof: int | None = None
    signals: Mapping[str, float | None] = field(default_factory=dict)


def decide_regime_side(
    signals: Mapping[str, float | None],
    static_side: str,
    rv_keys: Sequence[str],
    hvol_tolerance: float = 0.0,
    extremely_low_h20: float = 0.0,
    gates: Sequence[LevelGateSpec] = (),
) -> Decision:
    """Pure regime -> {long, short, flat} decision for ONE day's signal bundle.

    Parameters
    ----------
    signals : Mapping[str, float | None]
        The per-day signal bundle — the RV ladder keys (``rv_keys``) plus any
        level-gate signals (e.g. ``vvix``). A ``None`` value means "unavailable".
    static_side : str
        The run-level side (``long`` / ``short``) traded when the regime cannot
        be classified (missing RV) — the documented safe default, never a skip.
    rv_keys : Sequence[str]
        The RV ladder keys ordered SHORT->LONG (e.g. ``("h20","h30","h100")``);
        backwardation is ``signals[rv_keys[0]] > rv_keys[1] > rv_keys[2]``.
    hvol_tolerance : float
        Multiplicative relaxation of the STRICT ladder: each rung compares
        ``upper > lower * (1 - tolerance)``. ``0.0`` (default) is the strict
        ``>`` ladder (exact ties fail). Must be >= 0.
    extremely_low_h20 : float
        Absolute floor on the short-window RV (``rv_keys[0]``). If that RV is
        below this floor the day is forced ``flat`` (state ``extremely_low``),
        with precedence over even a backwardated ladder. ``0.0`` disables it.
    gates : Sequence[LevelGateSpec]
        Ordered level gates; later overrides earlier. See :class:`LevelGateSpec`.

    Returns
    -------
    Decision
        ``.side`` / ``.state`` / ``.gate`` set; ``.asof`` is ``None`` here (the
        as-of picker sets it), ``.signals`` echoes the consumed bundle.
    """
    if hvol_tolerance < 0:
        raise ValueError(f"hvol_tolerance must be >= 0, got {hvol_tolerance}")
    if extremely_low_h20 < 0:
        raise ValueError(f"extremely_low_h20 must be >= 0, got {extremely_low_h20}")
    if static_side not in ("long", "short"):
        raise ValueError(f"static_side must be 'long'|'short', got {static_side!r}")
    if len(rv_keys) != 3:
        raise ValueError(f"rv_keys must be a 3-tuple short->long, got {tuple(rv_keys)!r}")

    bundle = dict(signals)
    rv = [bundle.get(k) for k in rv_keys]

    # (1) Missing RV -> cannot classify -> fallback to the static run-level side.
    if any(v is None for v in rv):
        return Decision(side=static_side, state="fallback", gate=None, signals=bundle)

    h_short, h_mid, h_long = float(rv[0]), float(rv[1]), float(rv[2])

    # (2) Extremely-low floor: an ABSOLUTE veto (precedes even backwardation).
    #     Floor 0.0 disables it (RV is always >= 0, so ``> 0`` gates the check).
    if extremely_low_h20 > 0.0 and h_short < extremely_low_h20:
        return Decision(side="flat", state="extremely_low", gate=None, signals=bundle)

    # (3) Base regime: strict backwardation ladder, optionally relaxed.
    factor = 1.0 - hvol_tolerance
    hvol_on = (h_short > h_mid * factor) and (h_mid > h_long * factor)
    if hvol_on:
        state, base_side = "hvol_on", "long"
    else:
        state, base_side = "hvol_off", "short"

    # (4) Level gates: ordered, later overrides earlier. A None value never fires.
    side = base_side
    fired: str | None = None
    for g in gates:
        if not g.enabled:
            continue
        if g.action not in _SIDES:
            raise ValueError(f"gate action must be in {_SIDES}, got {g.action!r}")
        val = bundle.get(g.signal)
        if val is None:
            continue
        if float(val) > g.above:
            side = g.action
            fired = g.signal

    return Decision(side=side, state=state, gate=fired, signals=bundle)


def resolve_regime_decisions(
    dates: Sequence[int],
    signals_by_date: Mapping[int, Mapping[str, float | None]],
    static_side: str,
    rv_keys: Sequence[str],
    extremely_low_h20: float = 0.0,
    hvol_tolerance: float = 0.0,
    gates: Sequence[LevelGateSpec] = (),
    signal_names: Sequence[str] = ("h20", "h30", "h100", "vvix"),
) -> dict[int, Decision]:
    """NO-LOOK-AHEAD as-of picker: one :class:`Decision` per trade day.

    For each trade day ``D`` in ``dates`` the decision is taken as-of the LATEST
    signal date STRICTLY BEFORE ``D`` (``asof < D``) — day ``D``'s own daily
    close can NEVER influence ``D``'s side (the anti-look-ahead guarantee). When
    NO signal date precedes ``D``, the decision falls back to the static side
    with an all-``None`` signal bundle (state ``fallback``) — never a fabricated
    regime, never a silent skip.

    Parameters mirror :func:`decide_regime_side`; ``signals_by_date`` maps a
    ``YYYYMMDD`` signal date to its bundle (typically ALL daily signal dates over
    the fetch window, so ``asof`` is the true prior daily close, not merely the
    previous backtest day). ``signal_names`` is the full key list a bundle
    carries (RV keys + passthrough names), used to shape the fallback bundle.
    """
    signal_dates = sorted(int(d) for d in signals_by_date)
    out: dict[int, Decision] = {}
    for d in dates:
        di = int(d)
        # Latest signal date strictly before the trade day (<= D-1).
        asof: int | None = None
        for sd in signal_dates:
            if sd < di:
                asof = sd
            else:
                break
        if asof is None:
            null_bundle: dict[str, float | None] = {name: None for name in signal_names}
            dec = decide_regime_side(
                null_bundle, static_side, rv_keys,
                hvol_tolerance=hvol_tolerance,
                extremely_low_h20=extremely_low_h20,
                gates=gates,
            )
            out[di] = replace(dec, asof=None, signals=null_bundle)
            continue
        bundle = dict(signals_by_date[asof])
        dec = decide_regime_side(
            bundle, static_side, rv_keys,
            hvol_tolerance=hvol_tolerance,
            extremely_low_h20=extremely_low_h20,
            gates=gates,
        )
        out[di] = replace(dec, asof=asof, signals=bundle)
    return out
