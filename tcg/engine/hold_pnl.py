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


def _fref_at(spec: "_HoldPnLSpec", idx: int) -> float:
    """The frozen reference-future price at output index ``idx`` (NaN if absent)."""
    arr = spec.roll_future_ref
    if arr is None or idx < 0 or idx >= arr.size:
        return np.nan
    return float(arr[idx])


def _futures_denom_ok(spec: "_HoldPnLSpec", fref: float) -> bool:
    """True iff a futures-notional quantity can be sized at ``fref``.

    Requires a finite positive reference price AND finite positive multipliers —
    a missing/zero value must NEVER produce a silent 1.0 denominator (Guardrail
    Sign 2); it triggers the tail carry-forward instead.
    """
    return bool(
        np.isfinite(fref)
        and fref > 0.0
        and np.isfinite(spec.mult_fut)
        and spec.mult_fut > 0.0
        and np.isfinite(spec.mult_opt)
        and spec.mult_opt > 0.0
    )


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
    # ── Futures-notional sizing (Wave-1 opt-in; premium_notional is the default and
    #    is byte-identical — none of these fields are read in premium mode) ──────
    # ``premium_notional`` (default): qty = nav_times·NAV_roll/premium_roll,
    #   daily $ = qty·Δpremium.
    # ``futures_notional``: qty = nav_times·NAV_roll/(F_ref·mult_fut) (fractional,
    #   NOT floored), daily $ = qty·Δpremium·mult_opt.
    sizing_mode: str = "premium_notional"
    # Per-index reference-future price, FROZEN-at-roll (finite at each ``is_roll``
    # index, NaN elsewhere/off-roll).  ``None`` in premium mode.  A roll whose entry
    # is NaN triggers the tail carry-forward (keep the last sized qty).
    roll_future_ref: "npt.NDArray[np.float64] | None" = None
    # Contract multipliers: ``mult_fut`` scales the reference-future price into the
    # sizing DENOMINATOR notional; ``mult_opt`` scales the option premium move into
    # $ P&L.  They DIFFER for VIX (fut 1000, opt 100).  Read ONLY in futures mode;
    # the 1.0 defaults are inert there because the caller always supplies resolved
    # values (or NaN → tail carry-forward) — NEVER a silent 1.0 (Guardrail Sign 2).
    mult_fut: float = 1.0
    mult_opt: float = 1.0


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
    # Futures-notional companion: the reference-future price frozen at the segment's
    # roll (the sizing DENOMINATOR, with mult_fut).  NaN in premium mode / until the
    # first sizable roll.  Carried forward (unchanged) across a roll with no covering
    # future so the last sized quantity keeps accruing.
    seg_fref: dict[str, float] = {spec.ref_id: np.nan for spec in hold_specs}
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
        fut_mode = spec.sizing_mode == "futures_notional"
        if T >= 1 and bool(spec.pos_active[0]):
            open_prem = (
                spec.roll_premium[0] if bool(spec.is_roll[0]) else spec.premium[0]
            )
            if np.isfinite(open_prem) and open_prem > 0.0:
                if fut_mode:
                    # Futures mode also needs a valid reference-future denominator at
                    # the initial open; without one the leg stays flat until the
                    # first roll that HAS a covering future (nothing to carry from at
                    # bar 0).
                    fref0 = _fref_at(spec, 0)
                    if _futures_denom_ok(spec, fref0):
                        seg_premium[rid] = float(open_prem)
                        seg_fref[rid] = float(fref0)
                        seg_er[rid] = ratio[0]  # == 1.0
                        holding[rid] = True
                        last_finite[rid] = float(open_prem)
                else:
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
                dprem = cur - base
                if spec.sizing_mode == "futures_notional":
                    # Futures-notional: divide by the frozen future notional
                    # (F_ref·mult_fut) and scale the premium move by mult_opt.  The
                    # (seg_er/ratio[s]) equity-coupling and the dprem base are the
                    # SAME as premium mode — only the denominator + mult_opt differ.
                    seg_f = seg_fref[rid]
                    if (
                        np.isfinite(dprem)
                        and np.isfinite(base)
                        and np.isfinite(seg_f)
                        and seg_f != 0.0
                    ):
                        contrib = (
                            spec.sign
                            * spec.nav_times
                            * (seg_er[rid] / ratio[s])
                            * (dprem * spec.mult_opt)
                            / (seg_f * spec.mult_fut)
                        )
                else:
                    seg_p = seg_premium[rid]
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
                if spec.sizing_mode == "futures_notional":
                    fref_here = _fref_at(spec, s + 1)
                    # ``roll_future_ref`` is finite ONLY at roll bars, so a
                    # MID-SEGMENT (off-roll) re-open — the leg went flat and
                    # re-latched between rolls — reads NaN here and could not be
                    # sized, silently booking ZERO until the next roll (premium
                    # mode re-sizes fine on the same bar).  We are still inside the
                    # same roll period, so the segment's frozen reference (captured
                    # at its roll) is the correct anchor: carry it forward to size
                    # the re-entry.  A genuine roll bar keeps its own fref_here.
                    # KNOWN LIMITATION: if the leg is flat ACROSS a roll (the roll's
                    # resize was skipped while flat) and then re-enters off-roll,
                    # ``seg_fref`` is stale by one+ roll period, so the re-entry is
                    # approximately (not exactly) sized until the next roll re-anchors
                    # it.  Same-roll-period re-entry is exact; and this is strictly
                    # better than the prior behaviour (ZERO P&L for the whole window).
                    if not bool(spec.is_roll[s + 1]) and not np.isfinite(fref_here):
                        fref_here = seg_fref[rid]
                    if (
                        np.isfinite(open_prem)
                        and open_prem > 0.0
                        and ratio[s + 1] != 0.0
                    ):
                        # The dprem base ALWAYS re-anchors to the new segment's open
                        # (so the roll seam is never booked), independent of whether
                        # we can re-size the quantity.
                        seg_premium[rid] = float(open_prem)
                        last_finite[rid] = float(open_prem)
                        if _futures_denom_ok(spec, fref_here):
                            # Full re-size off the new future notional.
                            seg_fref[rid] = float(fref_here)
                            seg_er[rid] = ratio[s + 1]
                            holding[rid] = True
                        elif holding[rid]:
                            # TAIL CARRY-FORWARD (Guardrail tail policy): no covering
                            # future at this roll → keep the LAST sized quantity
                            # (seg_fref + seg_er frozen) and keep accruing option $
                            # P&L on the new contract.  NEVER size off missing data,
                            # never crash.  (Diagnostic is surfaced upstream by the
                            # resolver/fetcher that produced the NaN roll_future_ref.)
                            pass
                        else:
                            # Never sized yet AND no covering future → cannot size.
                            holding[rid] = False
                    elif not holding[rid]:
                        holding[rid] = False
                    # else: NaN open premium but already holding → keep prior sizing
                    #       (a NaN open leaves seg_* intact, matching premium mode).
                elif np.isfinite(open_prem) and open_prem > 0.0 and ratio[s + 1] != 0.0:
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


def hold_leg_notional_fractions(spec: _HoldPnLSpec) -> npt.NDArray[np.float64]:
    """Per-bar OPTION-PREMIUM notional fraction (of NAV) a held leg actually trades.

    The transaction-cost turnover of a held option leg must be billed on the
    option premium notional the sizing recurrence actually crosses -- which is the
    ``|nav_times|`` fraction of NAV ONLY in ``premium_notional`` mode.  In
    ``futures_notional`` mode the quantity is sized off the reference-FUTURE
    notional (``qty = nav_times·NAV_roll/(F_ref·mult_fut)``), so the option-premium
    notional crossed is only

        |nav_times|·seg_premium·mult_opt / (seg_fref·mult_fut)

    of NAV, where ``seg_premium`` (the segment's roll-day open premium) and
    ``seg_fref`` (the reference-future price frozen at the segment's roll) are the
    SAME frozen values :func:`_compound_with_hold` sizes ``qty`` with.  Billing
    turnover on ``nav_times`` there over-charges by ``(seg_fref·mult_fut) /
    (seg_premium·mult_opt)`` (e.g. ~100x for a low-premium far-OTM option).

    Returns a length-``T`` array; entry ``b`` is the fraction of the segment sized
    at the last open point ``<= b`` (0 on bars where no sized segment is held).
    This replays the exact ``seg_premium``/``seg_fref`` state machine of
    :func:`_compound_with_hold` MINUS its equity (``ratio``) gates -- the fraction
    is equity-INDEPENDENT (``qty`` depends only on the frozen premium / future
    price / multipliers), so it can be computed before compounding and fed to the
    cost turnover primitive.  The wipeout gate is omitted for the same reason the
    turnover primitive ignores ruin: after a wipe positions are flat and the
    (sub-basis-point) residual cost is immaterial.
    """
    premium = np.asarray(spec.premium, dtype=np.float64)
    roll_premium = np.asarray(spec.roll_premium, dtype=np.float64)
    is_roll = np.asarray(spec.is_roll, dtype=bool)
    pos_active = np.asarray(spec.pos_active, dtype=bool)
    T = premium.shape[0]
    frac = np.zeros(T, dtype=np.float64)
    mag = abs(float(spec.nav_times))
    if T == 0:
        return frac

    if spec.sizing_mode != "futures_notional":
        # premium_notional: the option premium notional deployed is exactly
        # nav_times·NAV on every held bar (identical to the scalar cost path).
        frac[pos_active[:T]] = mag
        return frac

    def _frac(seg_prem: float, seg_f: float) -> float:
        if (
            np.isfinite(seg_prem)
            and seg_prem > 0.0
            and np.isfinite(seg_f)
            and seg_f != 0.0
        ):
            return mag * seg_prem * spec.mult_opt / (seg_f * spec.mult_fut)
        return 0.0

    seg_premium = np.nan
    seg_fref = np.nan
    holding = False

    # Seed bar 0 (mirror the seed block of ``_compound_with_hold``): a leg latched
    # at bar 0 sizes only if it has BOTH a quotable open premium and a valid
    # reference-future denominator; otherwise it stays flat until the first roll
    # that has one.
    if bool(pos_active[0]):
        open_prem = roll_premium[0] if bool(is_roll[0]) else premium[0]
        if np.isfinite(open_prem) and open_prem > 0.0:
            fref0 = _fref_at(spec, 0)
            if _futures_denom_ok(spec, fref0):
                seg_premium = float(open_prem)
                seg_fref = float(fref0)
                holding = True
    if holding:
        frac[0] = _frac(seg_premium, seg_fref)

    # Resize at each subsequent bar exactly as ``_compound_with_hold`` does AFTER
    # booking the step (its "(re)size each hold leg" block), minus the ``ratio``
    # gates.
    for b in range(1, T):
        if not bool(pos_active[b]):
            holding = False
            continue
        is_open_point = bool(is_roll[b]) or not holding
        if is_open_point:
            open_prem = roll_premium[b] if bool(is_roll[b]) else premium[b]
            fref_here = _fref_at(spec, b)
            # Off-roll re-open reads NaN (roll_future_ref is finite only at rolls)
            # -> carry the segment's frozen reference forward (same-roll-period
            # re-entry), matching the P&L path.
            if not bool(is_roll[b]) and not np.isfinite(fref_here):
                fref_here = seg_fref
            if np.isfinite(open_prem) and open_prem > 0.0:
                seg_premium = float(open_prem)
                if _futures_denom_ok(spec, fref_here):
                    seg_fref = float(fref_here)
                    holding = True
                elif holding:
                    pass  # tail carry-forward: keep the last sized seg_fref
                else:
                    holding = False
            elif not holding:
                holding = False
            # else: NaN open premium but already holding -> keep prior sizing.
        if holding:
            frac[b] = _frac(seg_premium, seg_fref)
    return frac
