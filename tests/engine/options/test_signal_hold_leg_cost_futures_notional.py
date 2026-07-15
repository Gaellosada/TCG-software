"""Transaction-cost basis for a HOLD option leg in ``futures_notional`` mode.

Round-5 review MINOR: ``hold_leg_turnover`` billed the leg's turnover on
``nav_times`` as if it were the traded notional fraction.  That is correct for
``premium_notional`` (the premium notional deployed == nav_times·NAV), but in
``futures_notional`` mode the leg is sized off the FUTURE notional
``qty = nav_times·NAV/(F_ref·mult_fut)`` so the option-premium notional actually
crossed is only ``nav_times·premium·mult_opt/(F_ref·mult_fut)`` of NAV — bps must
apply to THAT premium notional, not to the underlying futures notional.

These tests pin the per-segment premium-notional basis end-to-end through
``evaluate_signal`` (and directly on the ``hold_leg_notional_fractions`` /
``hold_leg_turnover`` primitives), and guard the ``premium_notional`` regression.
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.costs import CostConfig, cumulative_cost_pct, hold_leg_turnover
from tcg.engine.hold_pnl import _HoldPnLSpec, hold_leg_notional_fractions
from tcg.engine.signal_exec import evaluate_signal

from _hold_pnl_oracle import (
    IS_ROLL as _IS_ROLL,
    HELD_PREMIUM as _HELD_PREMIUM,
    ROLL_PREMIUM as _ROLL_PREMIUM,
    make_hold_fetch,
)

from tcg.types.options import ByDelta, NearestToTarget
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    Input,
    InstrumentOperand,
    InstrumentOptionStream,
    InstrumentSpot,
    Signal,
    SignalRules,
)

# Async tests auto-marked (asyncio_mode="auto").

# Reference-future price frozen at each roll (bars 0 and 3); NaN elsewhere.
_ROLL_FREF = np.array([4500.0, np.nan, np.nan, 4520.0, np.nan, np.nan])


def _opt(*, sizing_mode: str, nav_times: float = 1.0) -> InstrumentOptionStream:
    return InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=-0.10, tolerance=0.20),
        stream="mid",
        hold_between_rolls=True,
        nav_times=nav_times,
        sizing_mode=sizing_mode,
    )


def _signal(*, sizing_mode: str, weight: float, nav_times: float) -> Signal:
    return Signal(
        id="s",
        name="fut-cost",
        inputs=(
            Input(
                id="P", instrument=_opt(sizing_mode=sizing_mode, nav_times=nav_times)
            ),
            Input(
                id="S",
                instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
            ),
        ),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="P",
                    weight=weight,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="S", field="close"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            )
        ),
    )


# --------------------------------------------------------------------------- #
# Primitive-level: the per-segment premium-notional fraction and the turnover
# it produces (reviewer's crisp VIX numbers).
# --------------------------------------------------------------------------- #


def _fut_spec(*, nav_times, premium, roll_premium, is_roll, roll_fref, m_fut, m_opt):
    prem = np.asarray(premium, dtype=np.float64)
    return _HoldPnLSpec(
        ref_id="P",
        sign=-1.0,
        nav_times=float(nav_times),
        premium=prem,
        is_roll=np.asarray(is_roll, dtype=bool),
        roll_premium=np.asarray(roll_premium, dtype=np.float64),
        pos_active=np.ones(prem.size, dtype=bool),
        sizing_mode="futures_notional",
        roll_future_ref=np.asarray(roll_fref, dtype=np.float64),
        mult_fut=float(m_fut),
        mult_opt=float(m_opt),
    )


def test_notional_fraction_reviewer_vix_numbers():
    """VIX put: nav_times=1, F_ref=20, mult_fut=1000, premium=2, mult_opt=100.

    Premium-notional fraction crossed = 1·2·100/(20·1000) = 0.01 — NOT nav_times
    (1.0).  Single held segment across all bars.
    """
    spec = _fut_spec(
        nav_times=1.0,
        premium=[2.0, 2.0, 2.0],
        roll_premium=[2.0, np.nan, np.nan],
        is_roll=[True, False, False],
        roll_fref=[20.0, np.nan, np.nan],
        m_fut=1000.0,
        m_opt=100.0,
    )
    frac = hold_leg_notional_fractions(spec)
    np.testing.assert_allclose(frac, [0.01, 0.01, 0.01], rtol=1e-12)
    assert not np.allclose(frac, 1.0)  # NOT the nav_times basis


def test_hold_leg_turnover_uses_per_segment_fraction():
    """A per-bar ``notional_frac`` bills open/close/roll on the SEGMENT fraction.

    Two segments (roll at bar 3), always held: open@0 (frac0), round-trip@3
    (frac2_close + frac3_open), no final close (held to end).  The scalar path
    (no ``notional_frac``) still bills the nav_times basis unchanged.
    """
    is_roll = np.array([True, False, False, True, False, False])
    active = np.ones(6, dtype=bool)
    frac = np.array([0.006, 0.006, 0.006, 0.004, 0.004, 0.004])
    t = hold_leg_turnover(is_roll, active, 1.0, 5, notional_frac=frac)
    np.testing.assert_allclose(t, [0.006, 0.0, 0.0, 0.004 + 0.006, 0.0], rtol=1e-12)
    # Scalar path (premium_notional) is UNCHANGED: nav_times basis.
    t_scalar = hold_leg_turnover(is_roll, active, 1.0, 5)
    np.testing.assert_allclose(t_scalar, [1.0, 0.0, 0.0, 2.0, 0.0], atol=1e-15)


def test_hold_leg_turnover_bills_late_sized_open():
    """Round-6 review MINOR: a leg latched on a no-quote/false-zero premium bar
    (frac=0, unsized) and sized only on a LATER non-roll continuation bar
    (frac 0→+ with the leg already active, NOT a roll) must still bill that
    OPEN.  The old event logic keyed OPEN off ``pos_active``/roll transitions
    (``opens = active & (rolls | ~active_prev)``) so a resize while continuously
    held fired neither an open nor a close → the segment's OPEN side was billed
    0 (under-charge).

    Reviewer's exact repro: mult_fut=mult_opt=50, nav_times=1,
    premium=[30,30,0,30,30], is_roll=[1,0,0,0,0], roll_premium[0]=30,
    roll_future_ref[0]=4500, pos_active=[1,0,1,1,1].  The leg latches at bar 0
    (sized 30/4500), unlatches at bar 1, re-latches at bar 2 on a false-zero
    premium (unsized, frac=0), and is finally sized at bar 3 on a non-roll
    continuation bar → held notional q=[f,0,0,f,f], f=30/4500.  Turnover must be
    the held-notional transitions |Δq| (q[-1]=0), billing the bar-3 open.
    """
    f = 30.0 / 4500.0
    spec = _fut_spec(
        nav_times=1.0,
        premium=[30.0, 30.0, 0.0, 30.0, 30.0],
        roll_premium=[30.0, np.nan, np.nan, np.nan, np.nan],
        is_roll=[True, False, False, False, False],
        roll_fref=[4500.0, np.nan, np.nan, np.nan, np.nan],
        m_fut=50.0,
        m_opt=50.0,
    )
    # Override the always-held pos_active baked into ``_fut_spec``.
    spec = _HoldPnLSpec(
        ref_id=spec.ref_id,
        sign=spec.sign,
        nav_times=spec.nav_times,
        premium=spec.premium,
        is_roll=spec.is_roll,
        roll_premium=spec.roll_premium,
        pos_active=np.array([True, False, True, True, True], dtype=bool),
        sizing_mode="futures_notional",
        roll_future_ref=spec.roll_future_ref,
        mult_fut=spec.mult_fut,
        mult_opt=spec.mult_opt,
    )
    frac = hold_leg_notional_fractions(spec)
    # Held notional: sized@0, flat@1, false-zero premium leaves it unsized@2,
    # finally sized@3 (carried @4).
    np.testing.assert_allclose(frac, [f, 0.0, 0.0, f, f], rtol=1e-12)

    t = hold_leg_turnover(
        spec.is_roll, spec.pos_active, spec.nav_times, 4, notional_frac=frac
    )
    # Ground truth |Δq| (q=[f,0,0,f,f], prepend 0): open@0, close@1, open@3.
    np.testing.assert_allclose(t, [f, f, 0.0, f], rtol=1e-12)
    # The old event logic under-billed the bar-3 open to 0.
    assert t[3] == pytest.approx(f, rel=1e-12)


# --------------------------------------------------------------------------- #
# End-to-end through evaluate_signal: the reported cost pct must reflect the
# premium-notional fraction actually crossed, NOT nav_times.
# --------------------------------------------------------------------------- #


async def test_signal_futures_notional_cost_uses_premium_notional_basis():
    """Reported slippage/fees pct for a futures_notional hold leg equals the
    per-segment premium-notional turnover basis, NOT the nav_times basis."""
    fetch = make_hold_fetch(
        held_premium=_HELD_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        roll_future_ref=_ROLL_FREF,
        multipliers=(50.0, 50.0),  # SP_500: m_fut == m_opt
    )
    cfg = CostConfig(slippage_bps=100.0, fees_bps=50.0)
    res = await evaluate_signal(
        _signal(sizing_mode="futures_notional", weight=-10.0, nav_times=1.0),
        {},
        fetch,
        cost_config=cfg,
    )

    # Per-segment premium-notional fraction (m_fut == m_opt cancels): seg0 opens
    # at roll_premium 30 / F_ref 4500; seg1 at 18 / 4520.  Always held.
    frac = np.array([30.0 / 4500.0] * 3 + [18.0 / 4520.0] * 3, dtype=np.float64)
    # Turnover: open@0 (frac0), interior round-trip@3 (frac2 close + frac3 open),
    # no final close (held to the last bar). Length n_steps == 5.
    turnover = np.array([frac[0], 0.0, 0.0, frac[2] + frac[3], 0.0], dtype=np.float64)
    er_start = res.equity_ratio[:-1]
    exp_slip = cumulative_cost_pct(0.01 * turnover, er_start)
    exp_fees = cumulative_cost_pct(0.005 * turnover, er_start)

    assert res.total_slippage_paid_pct == pytest.approx(exp_slip, rel=1e-9, abs=1e-15)
    assert res.total_fees_paid_pct == pytest.approx(exp_fees, rel=1e-9, abs=1e-15)

    # The buggy nav_times basis (turnover [1,0,0,2,0]) would be ~100x larger and
    # must be firmly rejected.
    buggy_turnover = np.array([1.0, 0.0, 0.0, 2.0, 0.0], dtype=np.float64)
    buggy_slip = cumulative_cost_pct(0.01 * buggy_turnover, er_start)
    assert res.total_slippage_paid_pct < 0.1 * buggy_slip


async def test_signal_premium_notional_cost_unchanged_nav_times_basis():
    """Regression: a premium_notional hold leg still bills the nav_times basis."""
    fetch = make_hold_fetch(
        held_premium=_HELD_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
    )
    cfg = CostConfig(slippage_bps=100.0, fees_bps=50.0)
    res = await evaluate_signal(
        _signal(sizing_mode="premium_notional", weight=-10.0, nav_times=1.0),
        {},
        fetch,
        cost_config=cfg,
    )
    # nav_times basis: open@0 (1.0), round-trip@3 (2.0), held to end.
    turnover = np.array([1.0, 0.0, 0.0, 2.0, 0.0], dtype=np.float64)
    er_start = res.equity_ratio[:-1]
    exp_slip = cumulative_cost_pct(0.01 * turnover, er_start)
    exp_fees = cumulative_cost_pct(0.005 * turnover, er_start)
    assert res.total_slippage_paid_pct == pytest.approx(exp_slip, rel=1e-9, abs=1e-15)
    assert res.total_fees_paid_pct == pytest.approx(exp_fees, rel=1e-9, abs=1e-15)
