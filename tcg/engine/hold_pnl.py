"""Fixed-contract dollar-P&L accumulator for held option positions.

Extracted VERBATIM from ``signal_exec`` so the SAME recurrence is shared by
BOTH the signal evaluator (:mod:`tcg.engine.signal_exec`) and the portfolio
option-stream leg (:mod:`tcg.core.api.portfolio`) without duplicating the
$-P&L math.  Pure NumPy -- no coupling to block/signal machinery.

``signal_exec`` re-imports ``_HoldPnLSpec`` and ``_compound_with_hold`` from
here under their original names; their behaviour is byte-identical to their
previous in-module home (this is a pure move).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


# ---------------------------------------------------------------------------
# Fixed-contract dollar-P&L for held option positions (hold_between_rolls)
# ---------------------------------------------------------------------------


@dataclass
class _HoldPnLSpec:
    """Per-(hold-mode option input) data for the fixed-contract dollar-P&L path.

    Aligned to the signal's union date axis (length ``T``).  Direction is the
    block-weight SIGN (``sign``); ``nav_times`` is the premium-notional size (NOT
    ``|weight|/100`` — that is the whole reason ``nav_times`` is a separate field).

    * ``premium`` — the HELD contract's mid LEVEL of the contract owning each
      date's value (the resolver's hold-mode ``values``: OLD contract's mid on a
      roll day, held contract otherwise).
    * ``is_roll`` — True at each hold segment's first date (incl. the initial
      open); a roll RESIZES the held quantity off the post-P&L NAV.
    * ``roll_premium`` — at each ``is_roll`` date, the NEW segment's roll-day OPEN
      mid: the base for that segment's daily P&L and its quantity sizing (the ONLY
      place the NEW open premium is surfaced — ``premium`` on a roll date is the
      OLD mid, so the seam is exact, never a raw old→new level gap).
    * ``pos_active`` — per-bar 0/1: whether the input's net position is open
      (latched) on the step START.  A closed position contributes 0 that step; a
      re-open mid-hold is treated as a fresh open at the current premium (a new
      sizing point) so the $-P&L only accrues while the leg is actually held.
    """

    ref_id: str
    sign: float
    nav_times: float
    premium: npt.NDArray[np.float64]
    is_roll: npt.NDArray[np.bool_]
    roll_premium: npt.NDArray[np.float64]
    pos_active: npt.NDArray[np.bool_]


def _compound_with_hold(
    vectorized_net_step: npt.NDArray[np.float64],
    hold_specs: list[_HoldPnLSpec],
) -> tuple[
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    dict[str, npt.NDArray[np.float64]],
]:
    """Sequential joint compounding for a mix of vectorized inputs and hold-mode
    option inputs (fixed-contract dollar P&L).

    ``vectorized_net_step`` (length ``T-1``) is the SUM of every non-hold input's
    equity-independent ``contrib_step`` (``pos·Δprice/price`` etc.).  Each entry
    of ``hold_specs`` contributes, PER STEP ``s`` (from bar ``s`` to ``s+1``),

        contrib = sign · nav_times · (equity_ratio[roll] / equity_ratio[s])
                         · (premium[s+1] − base) / premium[roll]

    where ``base`` is the current segment's roll-day open premium on the step
    right after a roll, else ``premium[s]`` (interior); ``premium[roll]`` and
    ``equity_ratio[roll]`` are frozen at the segment's roll.  This is the
    fraction-of-current-NAV form of ``qty·Δpremium`` with the held quantity sized
    once per roll off the compounding NAV — verified equal to the Java oracle NAV
    ratio to machine epsilon.  Because it reads ``equity_ratio[s]`` (the running
    JOINT equity at the step start), the whole account is compounded in ONE
    sequential pass; the vectorized inputs' per-step contributions are added in.

    Returns ``(equity_ratio, step_scale, hold_contrib_steps)`` where:
      * ``equity_ratio`` (length ``T``), ``step_scale`` (length ``T-1``) have the
        SAME meaning as :func:`_compound_clamped` (absorbing ruin clamp; the loss
        cap on the wiping step), so the existing per-input ``realized_pnl`` builder
        (``cumsum(step_scale·equity_ratio[:-1]·contrib_step)``) reconciles to
        ``equity_ratio − 1``;
      * ``hold_contrib_steps[ref_id]`` (length ``T-1``) is each hold input's ACTUAL
        booked per-step contribution (pre-clamp; the clamp is applied uniformly via
        ``step_scale`` in the realized_pnl builder, exactly as for vectorized
        inputs) so its ``realized_pnl`` can be built the same way.
    """
    n = vectorized_net_step.size  # T-1
    T = n + 1
    ratio = np.ones(T, dtype=np.float64)
    step_scale = np.ones(max(n, 0), dtype=np.float64)
    hold_contrib: dict[str, npt.NDArray[np.float64]] = {
        spec.ref_id: np.zeros(max(n, 0), dtype=np.float64) for spec in hold_specs
    }

    # Per-hold-spec running segment state: the roll-day open premium and the
    # equity_ratio captured at the segment's roll (both frozen until the next
    # roll).  ``seg_premium`` is NaN until the leg's first valid open; while NaN
    # the leg books 0 (not yet sized / no quote to size against).  ``holding``
    # tracks whether a sized position is currently held.
    seg_premium: dict[str, float] = {spec.ref_id: np.nan for spec in hold_specs}
    seg_er: dict[str, float] = {spec.ref_id: 1.0 for spec in hold_specs}
    holding: dict[str, bool] = {spec.ref_id: False for spec in hold_specs}
    # Last FINITE premium of the held contract, carried forward as the interior
    # P&L base across a no-quote (NaN) day — matching the oracle ``java_faithful_s1``
    # (its ``prev_premium`` only updates on a finite premium; a NaN books 0 but does
    # NOT reset the base, so the first finite day after a gap captures the WHOLE
    # move ``qty·(premium_t − last_finite_premium)``).  Reset to the segment open at
    # each roll/open point.  On a gapless segment this equals ``premium[s]`` on every
    # interior step, so the default (continuous-quote) path is byte-identical.
    last_finite: dict[str, float] = {spec.ref_id: np.nan for spec in hold_specs}

    # Seed bar-0 sizing: the loop below sizes at bar s+1, so the initial open at
    # bar 0 (a leg latched at bar 0, whose first date is a segment open) must be
    # sized here off ratio[0]==1 and bar 0's open premium.  A leg not yet open at
    # bar 0 stays flat until its first latch bar, where the loop sizes it.
    for spec in hold_specs:
        rid = spec.ref_id
        if T >= 1 and bool(spec.pos_active[0]):
            open_prem = (
                spec.roll_premium[0] if bool(spec.is_roll[0]) else spec.premium[0]
            )
            if np.isfinite(open_prem) and open_prem > 0.0:
                seg_premium[rid] = float(open_prem)
                seg_er[rid] = ratio[0]  # == 1.0
                holding[rid] = True
                last_finite[rid] = float(open_prem)  # carry-forward base seed

    wiped = False
    for s in range(n):
        if wiped:
            ratio[s + 1] = 0.0
            step_scale[s] = 0.0
            continue

        net = float(vectorized_net_step[s])

        # Book each hold leg's step P&L on the quantity held INTO bar s+1 (sized
        # at the leg's current segment: seg_premium/seg_er, frozen at its roll).
        # The step-owner's move is (premium[s+1] − base): interior → base is the
        # held mid on bar s (premium[s]); the FIRST step of a segment (previous
        # bar was that segment's roll) → base is the segment's roll-day OPEN
        # (roll_premium[s]), NOT premium[s] (which on a roll bar is the OLD mid).
        for spec in hold_specs:
            rid = spec.ref_id
            contrib = 0.0
            if (
                holding[rid]
                and bool(spec.pos_active[s])
                and bool(spec.pos_active[s + 1])
                and ratio[s] != 0.0
            ):
                # Interior base = the LAST FINITE held premium (carried forward
                # across a no-quote day), so a gap books its full move on the next
                # finite day instead of dropping it (matches the oracle's
                # ``prev_premium``).  A roll bar uses the NEW segment's open
                # (roll_premium[s]) — the seam is exact, never carried across.  On a
                # gapless segment ``last_finite`` == ``premium[s]`` here, so this is
                # byte-identical to the prior behaviour.
                base = (
                    spec.roll_premium[s] if bool(spec.is_roll[s]) else last_finite[rid]
                )
                cur = spec.premium[s + 1]
                seg_p = seg_premium[rid]
                dprem = cur - base
                if (
                    np.isfinite(dprem)
                    and np.isfinite(base)
                    and np.isfinite(seg_p)
                    and seg_p != 0.0
                ):
                    contrib = (
                        spec.sign
                        * spec.nav_times
                        * (seg_er[rid] / ratio[s])
                        * dprem
                        / seg_p
                    )
                # Carry the last FINITE held premium forward as the next interior
                # step's base (the oracle updates ``prev_premium`` only on a finite
                # premium — a NaN leaves the base unchanged).
                if np.isfinite(cur):
                    last_finite[rid] = float(cur)
            hold_contrib[rid][s] = contrib
            net += contrib

        # Advance the joint equity with the absorbing ruin clamp (identical to
        # _compound_clamped) — this is the equity_ratio the NEXT step's hold
        # contribs read via ratio[s+1].
        f = 1.0 + net
        if not np.isfinite(f) or f <= 0.0:
            ratio[s + 1] = 0.0
            step_scale[s] = (-1.0 / net) if net != 0.0 else 0.0
            wiped = True
        else:
            ratio[s + 1] = ratio[s] * f

        # AFTER booking bar s+1: (re)size each hold leg whose bar s+1 is a roll or
        # a fresh open, off the POST-step NAV (ratio[s+1]) and the segment's
        # roll-day open premium.  A roll realises the OLD (already folded into
        # ratio[s+1], seam-free) and opens the NEW; a fresh latch-open sizes at the
        # current premium.  Sizing after the step means seg_er = ratio[s+1] — the
        # verified oracle ordering (qty_new = nav_times·NAV_at_roll/premium_roll).
        for spec in hold_specs:
            rid = spec.ref_id
            active_next = bool(spec.pos_active[s + 1])
            if not active_next:
                # Position closed at or before bar s+1 → drop the sizing (a later
                # re-open re-sizes fresh).
                holding[rid] = False
                continue
            is_open_point = bool(spec.is_roll[s + 1]) or not holding[rid]
            if is_open_point:
                open_prem = (
                    spec.roll_premium[s + 1]
                    if bool(spec.is_roll[s + 1])
                    else spec.premium[s + 1]
                )
                if np.isfinite(open_prem) and open_prem > 0.0 and ratio[s + 1] != 0.0:
                    seg_premium[rid] = float(open_prem)
                    seg_er[rid] = ratio[s + 1]
                    holding[rid] = True
                    # A NEW segment's carry-forward base restarts at its OPEN premium
                    # (the seam is exact — never carry the OLD segment's last finite,
                    # nor the roll-day OLD mid that ``premium[s+1]`` holds, across).
                    last_finite[rid] = float(open_prem)
                elif not holding[rid]:
                    # Cannot size (no quotable open premium) → stay flat.
                    holding[rid] = False

    return ratio, step_scale, hold_contrib
