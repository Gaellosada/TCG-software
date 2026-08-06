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


def _daily_fref_at(spec: "_HoldPnLSpec", idx: int) -> float:
    """The per-date reference-future price at output index ``idx`` (NaN if absent).

    P-OFFROLL-SIZING rescue source: unlike :func:`_fref_at` (finite only at roll /
    segment-open bars) this is the SAME reference future's close on EVERY trade
    date, used ONLY to size an off-roll open whose roll/segment reference and
    carried ``seg_fref`` are both NaN.  ``None`` (the default) returns NaN so the
    rescue never fires and the shipped path stays byte-identical."""
    arr = spec.daily_future_ref
    if arr is None or idx < 0 or idx >= arr.size:
        return np.nan
    return float(arr[idx])


def delta_hedge_qty(
    factor: float,
    option_qty: float,
    option_delta: float,
    hedge_unit_delta: float = 1.0,
    cap_mult: float = 10.0,
) -> float:
    """Delta-hedge instrument quantity for a single option position (F2 core mechanic).

    ``qty_hedge = -factor · (option_qty · option_delta) / hedge_unit_delta`` — the
    hedge instrument's OWN per-unit delta divides, so ``factor`` of the option's
    net delta is neutralised by that many delta-equivalents of the hedge
    instrument.  A future/spot has ``hedge_unit_delta = 1`` (the DEFAULT), which
    reduces to the pre-modularization ``-factor·option_qty·option_delta`` exactly
    (``x / 1.0 == x`` in IEEE754).  For a LONG call (``option_qty·option_delta >
    0``, ``hedge_unit_delta > 0``) the result is NEGATIVE ⇒ SHORT the hedge, the
    sign the spec requires.

    Two guards make the divide safe for a general (option-)hedge whose per-unit
    delta drifts and can approach 0:

    * **Degenerate delta** — ``|hedge_unit_delta| < 1e-6`` (or non-finite) ⇒ return
      ``0.0`` (book NO hedge this step; never a silent inf/NaN fill).
    * **Quantity cap** — ``|qty_hedge| ≤ cap_mult · |option_qty|`` (symmetric clip,
      default ``cap_mult = 10.0``), applied in QUANTITY units (option-qty scale)
      before the caller multiplies by ``ΔP_hedge``.  For the VX1/future path
      (``|δ_opt| ≤ 1``, ``factor = 1/3``, ``hedge_unit_delta = 1``) ``|qty| ≤
      0.33·|option_qty|`` never binds ⇒ byte-identical.

    Pure scalar; used both by :func:`hedge_step_contrib` / :func:`_compound_with_hold`
    (per-step) and directly by the unit oracle so the sizing law is verified in
    isolation.
    """
    if not np.isfinite(hedge_unit_delta) or abs(hedge_unit_delta) < 1e-6:
        return 0.0
    q = -factor * option_qty * option_delta / hedge_unit_delta
    bound = cap_mult * abs(option_qty)
    return float(np.clip(q, -bound, bound))


def hedge_step_contrib(
    *,
    factor: float,
    option_qty_cur: float,
    delta_opt_s: float,
    hedge_unit_delta_s: float,
    d_hedge_price: float,
    cap_mult: float = 10.0,
) -> float:
    """One step's hedge $-P&L contribution, sized off the option's own quantity.

    The SINGLE accrual primitive shared by BOTH notional bases of
    :func:`_compound_with_hold` (previously two duplicated inline blocks): given
    the option's roll-frozen ``option_qty_cur`` (the coefficient of ``dprem`` in
    the option's own contrib), today's option delta ``delta_opt_s``, the hedge
    instrument's per-unit delta ``hedge_unit_delta_s`` (``1.0`` for a spot/future),
    and the hedge price move ``d_hedge_price = P_hedge[s+1] − P_hedge[s]``:

        qty_hedge  = delta_hedge_qty(factor, option_qty_cur, delta_opt_s,
                                     hedge_unit_delta_s, cap_mult)     (guarded/capped)
        contrib_$  = qty_hedge · d_hedge_price

    A non-finite ``delta_opt_s`` or ``d_hedge_price`` books ``0.0`` that step
    (never a silent fill) — mirroring the pre-modularization guard exactly, so the
    spot/future path stays byte-identical.
    """
    if not (np.isfinite(delta_opt_s) and np.isfinite(d_hedge_price)):
        return 0.0
    qty_hedge = delta_hedge_qty(
        factor, option_qty_cur, delta_opt_s, hedge_unit_delta_s, cap_mult
    )
    return qty_hedge * d_hedge_price


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
    # ── Off-roll open sizing (P-OFFROLL-SIZING; None = byte-identical) ──────────
    # ``roll_future_ref`` is finite ONLY at the option's roll / segment-open bars.
    # A SIGNAL leg whose entry latches on an arbitrary INTERIOR (off-roll) bar —
    # before any roll-while-held has frozen ``seg_fref`` — therefore read NaN, could
    # not be sized, and silently booked ZERO across the whole hold (this dropped the
    # §5.5 Aug-2024 spike gain).  ``daily_future_ref`` (length ``T``) carries the
    # SAME reference future's price on EVERY trade date, so an off-roll open can be
    # sized off the current front-future price.  RESCUE ONLY: it is consulted
    # exclusively when the roll/segment reference AND the carried ``seg_fref`` are
    # both NaN on an OFF-ROLL bar — a roll-aligned leg never touches it, so the
    # shipped (roll-held) path is byte-identical.  ``None`` disables it entirely.
    daily_future_ref: "npt.NDArray[np.float64] | None" = None
    # ── Delta-hedge overlay (F2; None = NO hedge → byte-identical, the new path is
    #    fully guarded).  Models a futures HEDGE sized off THIS option's net delta
    #    and accrued into the SAME leg equity (faithful to SPEC §5.5/§5.6:
    #    "call + VX1 hedge" is ONE leg whose equity already includes the hedge). ──
    # ``hedge_factor`` — the fraction of the option's delta to hedge (SPEC = 1/3);
    #   None disables the overlay entirely.
    # ``hedge_delta`` — the HELD option contract's per-bar delta (length ``T``);
    #   NaN on a bar ⇒ that step's hedge books 0 (never a silent fill).
    # ``hedge_price`` — the hedge future's (VX1) per-bar price (length ``T``); the
    #   daily hedge P&L is ``qty_hedge·(hedge_price[s+1]−hedge_price[s])``.  NaN at
    #   either end of a step ⇒ that step's hedge books 0.
    # ``hedge_active`` — per-bar 0/1 gate (length ``T``): the hedge accrues on step
    #   ``s`` only when ``hedge_active[s]`` (default None = always active while the
    #   position is held).  The gate/exit LIFECYCLE (VVIX>150, VIX<MA5 …) is
    #   precomputed by the caller and passed in — the engine only SIZES + rebalances.
    # Supported in BOTH sizing modes (GAP B): the hedge sizes off the option's OWN
    # quantity — ``nav_times·NAV_roll/premium_roll`` (premium_notional) or
    # ``nav_times·NAV_roll·mult_opt/(F_ref·mult_fut)`` (futures_notional) — so it is
    # well-defined regardless of the leg's notional basis.
    hedge_factor: "float | None" = None
    hedge_delta: "npt.NDArray[np.float64] | None" = None
    hedge_price: "npt.NDArray[np.float64] | None" = None
    hedge_active: "npt.NDArray[np.bool_] | None" = None
    # ``hedge_unit_delta`` — the HEDGE instrument's OWN per-unit delta (length
    #   ``T``).  The hedge quantity DIVIDES by it: ``qty_hedge =
    #   -factor·option_qty·δ_opt / δ_hedge``.  For a spot/future (the only hedge
    #   instruments in scope) ``δ_hedge ≡ 1`` — passed as an all-ones array, or left
    #   ``None`` which the accrual treats as ``1.0`` per step (both byte-identical to
    #   the pre-modularization ``·1`` behaviour).  A near-zero / non-finite entry
    #   books 0 that step (guarded in :func:`delta_hedge_qty`).
    hedge_unit_delta: "npt.NDArray[np.float64] | None" = None
    # ``rebalance_interval_days`` — re-size the hedge off TODAY's delta only on
    #   rebalance bars (axis-step index ``s`` with ``s % N == 0``); FREEZE the delta
    #   sizing between them (delta drift ignored until the next rebalance).  ``N = 1``
    #   (DEFAULT) rebalances every bar ⇒ EXACTLY the pre-parametrization daily
    #   behaviour (byte-identical); ``N <= 1`` is treated as ``1``.
    rebalance_interval_days: int = 1
    # ``qty_cap_mult`` — the symmetric per-step quantity cap fed to
    #   :func:`delta_hedge_qty` (``|qty_hedge| ≤ qty_cap_mult·|option_qty|``).  The
    #   ``10.0`` default never binds for the VX1/future path ⇒ byte-identical.
    qty_cap_mult: float = 10.0


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
    # Delta-hedge rebalance state: the option delta + hedge-unit delta CAPTURED at
    # the most recent rebalance bar, reused on frozen (non-rebalance) bars.  NaN
    # until the first rebalance capture.  At ``rebalance_interval_days == 1`` every
    # bar recaptures ⇒ these carry no cross-step effect (byte-identical daily path).
    hedge_frozen_delta: dict[str, float] = {spec.ref_id: np.nan for spec in hold_specs}
    hedge_frozen_hud: dict[str, float] = {spec.ref_id: np.nan for spec in hold_specs}

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
                    # P-OFFROLL-SIZING: a bar-0 OFF-ROLL open (no roll reference)
                    # is sized off the per-date front-future price so it is not left
                    # unsized until the first roll.  Guarded on ``not is_roll[0]`` so
                    # a roll-with-missing-future bar-0 keeps its tail/flat behaviour
                    # (byte-identical).
                    if not np.isfinite(fref0) and not bool(spec.is_roll[0]):
                        fref0 = _daily_fref_at(spec, 0)
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

                # ── Delta-hedge overlay (F2) ──────────────────────────────────
                # A futures HEDGE sized off THIS option's net delta, rebalanced
                # DAILY, accrued into the SAME leg contrib.  qty_hedge(s) =
                # -factor·option_qty·delta[s]; hedge $ P&L = qty_hedge·ΔVX1[s].
                # ``option_qty`` is the SAME quantity that scales ``dprem`` in the
                # option's OWN contrib — i.e. the coefficient of ``dprem`` above —
                # so the hedge and the hedged option are sized off ONE consistent
                # quantity in EITHER notional basis (GAP B):
                #   * premium_notional: sign·nav_times·(seg_er/ratio)/seg_premium
                #     (byte-identical to before);
                #   * futures_notional: sign·nav_times·(seg_er/ratio)·mult_opt/
                #     (seg_fref·mult_fut)  — the option's futures-notional qty; the
                #     future's per-unit delta is 1 in this same quantity space.
                # The extra 1/ratio[s] converts the $ P&L to a fraction of CURRENT
                # NAV, exactly as the option contrib does.  The hedge is INDEPENDENT
                # of the option's own premium move (depends only on the frozen option
                # qty, today's delta and ΔVX1), so it books even on a NaN-premium day.
                # Guarded: no hedge_factor ⇒ untouched (byte-identical).
                if spec.hedge_factor is not None and (
                    spec.hedge_active is None or bool(spec.hedge_active[s])
                ):
                    hd = spec.hedge_delta
                    hp = spec.hedge_price
                    hud = spec.hedge_unit_delta
                    if hd is not None and hp is not None:
                        # Mode-specific: the option's OWN roll-frozen quantity — the
                        # SAME coefficient of ``dprem`` in the option contrib above,
                        # in EITHER notional basis (GAP B).  ``None`` ⇒ this step's
                        # seg-denominator is unusable ⇒ no hedge (as before).
                        option_qty_cur: "float | None" = None
                        if spec.sizing_mode == "futures_notional":
                            # Sized off the frozen future notional (seg_fref·mult_fut),
                            # dollar-delta exposure carries mult_opt.
                            seg_f_h = seg_fref[rid]
                            if (
                                np.isfinite(seg_f_h)
                                and seg_f_h != 0.0
                                and np.isfinite(spec.mult_fut)
                                and spec.mult_fut != 0.0
                            ):
                                option_qty_cur = (
                                    spec.sign
                                    * spec.nav_times
                                    * (seg_er[rid] / ratio[s])
                                    * spec.mult_opt
                                    / (seg_f_h * spec.mult_fut)
                                )
                        else:
                            seg_p_h = seg_premium[rid]
                            if np.isfinite(seg_p_h) and seg_p_h != 0.0:
                                option_qty_cur = (
                                    spec.sign
                                    * spec.nav_times
                                    * (seg_er[rid] / ratio[s])
                                    / seg_p_h
                                )
                        if option_qty_cur is not None:
                            # Rebalance-freeze: recapture the delta sizing (option
                            # delta + hedge-unit delta) on a rebalance bar
                            # (``s % N == 0``), else REUSE the last capture — "re-size
                            # off today's delta only on rebalance bars".  ``N <= 1``
                            # recaptures every bar ⇒ byte-identical daily behaviour.
                            N = spec.rebalance_interval_days
                            # ``δ_hedge = 1`` for a spot/future (all-ones array or None).
                            raw_hud = float(hud[s]) if hud is not None else 1.0
                            if (
                                N <= 1
                                or (s % N == 0)
                                or not np.isfinite(hedge_frozen_delta[rid])
                            ):
                                hedge_frozen_delta[rid] = float(hd[s])
                                hedge_frozen_hud[rid] = raw_hud
                            delta_s = hedge_frozen_delta[rid]
                            hud_s = hedge_frozen_hud[rid]
                            d_hedge = float(hp[s + 1]) - float(hp[s])
                            if np.isfinite(delta_s) and np.isfinite(d_hedge):
                                contrib += hedge_step_contrib(
                                    factor=spec.hedge_factor,
                                    option_qty_cur=option_qty_cur,
                                    delta_opt_s=delta_s,
                                    hedge_unit_delta_s=hud_s,
                                    d_hedge_price=d_hedge,
                                    cap_mult=spec.qty_cap_mult,
                                )
            hold_contrib[rid][s] = contrib
            net += contrib

        # Advance the joint equity with the absorbing ruin clamp (identical to
        # _compound_clamped) — this is the equity_ratio the NEXT step's hold
        # contribs read via ratio[s+1].
        f = 1.0 + net
        if not np.isfinite(f) or f <= 0.0:
            # Absorbing ruin: the equity_ratio latches to exactly 0.0 here (and
            # stays 0.0 for all later steps via the ``wiped`` guard above).
            # CONTRACT: this literal 0.0 is the DEAD-leg marker that
            # ``metrics._compute_periodic_rebalance`` keys on (== 0.0) to stop
            # re-funding a wiped leg. Keep it exactly 0.0 — a clamp to an
            # epsilon would silently break that downstream detection.
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
                # The delta-hedge rebalance-freeze (seg_premium's sibling) is a
                # PER-SEGMENT capture: clear it at every segment open (a roll, or an
                # off-roll re-latch) so the first active bar of the NEW segment
                # recaptures off the NEW contract's delta instead of reusing the
                # PRIOR contract's frozen value under ``rebalance_interval_days > 1``.
                # NaN forces the next active bar's ``not np.isfinite(...)`` recapture.
                # At ``N <= 1`` the next bar recaptures every bar regardless ⇒ this is
                # byte-identical (incl. the VX1/VVIX default path, where N == 1).
                if spec.hedge_factor is not None:
                    hedge_frozen_delta[rid] = np.nan
                    hedge_frozen_hud[rid] = np.nan
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
                    # P-OFFROLL-SIZING: when the carried ``seg_fref`` is ALSO NaN
                    # (the leg was never held at a roll — e.g. a signal entry that
                    # FIRST latches off-roll), fall back to the per-date front-future
                    # price so the open is sized instead of booking ZERO for the whole
                    # hold.  RESCUE ONLY — reached exclusively on an off-roll bar where
                    # both references are NaN, so a roll-aligned leg (finite
                    # roll_future_ref) and a same-roll-period re-entry (finite
                    # seg_fref) are byte-identical.
                    if not bool(spec.is_roll[s + 1]) and not np.isfinite(fref_here):
                        fref_here = seg_fref[rid]
                        if not np.isfinite(fref_here):
                            fref_here = _daily_fref_at(spec, s + 1)
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
            # P-OFFROLL-SIZING (mirror the P&L seed): rescue a bar-0 OFF-ROLL open
            # with the per-date front-future price so cost turnover matches the
            # sizing path.  Guarded on ``not is_roll[0]`` → byte-identical otherwise.
            if not np.isfinite(fref0) and not bool(is_roll[0]):
                fref0 = _daily_fref_at(spec, 0)
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
            # re-entry), matching the P&L path.  P-OFFROLL-SIZING: when seg_fref is
            # ALSO NaN (never held at a roll) fall back to the per-date front-future
            # price so cost turnover matches the sizing rescue (byte-identical
            # otherwise — reached only when both references are NaN off-roll).
            if not bool(is_roll[b]) and not np.isfinite(fref_here):
                fref_here = seg_fref
                if not np.isfinite(fref_here):
                    fref_here = _daily_fref_at(spec, b)
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
