"""Engine tests for the two default-off features added on wip/gt-validation-build3.

Feature 1 — per-input ``position_cap`` clamp on the NET latched position:
  F1a  OR of two +100 entries → net position clamped to +1.0 (long-or-flat),
       NOT +2.0 (byte-identity control asserts uncapped IS +2.0).
  F1b  clamp flows into contrib_step / realized_pnl / equity curve (the
       capped position drives a smaller compounded return than the uncapped).
  F1c  short-or-flat cap (-1.0, 0.0) clamps a net -2.0 to -1.0 and forbids
       positive exposure.
  F1d  DEFAULT (position_cap=None) is unchanged — net +2.0 preserved.
  F1e  cap that never binds (range wide enough) leaves the position untouched.

Feature 2 — ``CrossCondition.count_mode="since_reset"``:
  F2a  fires on EXACTLY the Nth crossing since the last reset (impulse), and
       re-arms after the reset fires (fires again on the next Nth crossing).
  F2b  DEFAULT count_mode="rolling" byte-identical to a trailing-window count.
  F2c  no bound reset → counter never resets (cumulative from bar 0).
  F2d  reset fire BEFORE reaching N restarts the count (partial progress lost).
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

from tcg.engine.signal_exec import _cross_since_reset, evaluate_signal
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    CrossCondition,
    Input,
    InstrumentOperand,
    InstrumentSpot,
    Signal,
    SignalRules,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _make_fetcher(prices: np.ndarray, dates: np.ndarray) -> Callable:
    async def fetch(instrument, field):
        return dates, np.asarray(prices, dtype=np.float64)

    return fetch


def _close(iid: str = "X") -> InstrumentOperand:
    return InstrumentOperand(input_id=iid, field="close")


def _const(v: float) -> ConstantOperand:
    return ConstantOperand(value=v)


def _input(iid: str = "X", *, position_cap=None) -> Input:
    return Input(
        id=iid,
        instrument=InstrumentSpot(collection="I", instrument_id=iid),
        position_cap=position_cap,
    )


def _gt(iid: str, thr: float) -> CompareCondition:
    return CompareCondition(op="gt", lhs=_close(iid), rhs=_const(thr))


def _lt(iid: str, thr: float) -> CompareCondition:
    return CompareCondition(op="lt", lhs=_close(iid), rhs=_const(thr))


async def _positions(signal: Signal, prices, dates):
    res = await evaluate_signal(signal, {}, _make_fetcher(np.asarray(prices), dates))
    return res


# =========================================================================== #
# Feature 1 — position_cap
# =========================================================================== #

# Two entry blocks on the SAME input, both +100, that latch together and
# never exit. Prices rise so both conditions are True from t=0.
_CAP_PRICES = [110.0, 111.0, 112.0, 113.0, 114.0]
_CAP_DATES = np.arange(20240101, 20240101 + len(_CAP_PRICES), dtype=np.int64)


def _two_plus100_or(position_cap=None) -> Signal:
    # both conditions true for all bars (price always > 100 and > 105)
    return Signal(
        id="s",
        name="s",
        inputs=(_input("X", position_cap=position_cap),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1", input_id="X", weight=100.0, conditions=(_gt("X", 100.0),)
                ),
                Block(
                    id="e2", input_id="X", weight=100.0, conditions=(_gt("X", 105.0),)
                ),
            )
        ),
    )


@pytest.mark.asyncio
async def test_f1d_default_uncapped_stacks_to_plus_two():
    """Control: with NO cap the OR of two +100 blocks nets +2.0."""
    res = await _positions(_two_plus100_or(position_cap=None), _CAP_PRICES, _CAP_DATES)
    pos = res.positions[0].values
    assert pos.tolist() == [2.0, 2.0, 2.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_f1a_long_or_flat_caps_at_plus_one():
    """position_cap=(0.0, 1.0): the same OR-of-two-+100 nets +1.0, not +2.0."""
    res = await _positions(
        _two_plus100_or(position_cap=(0.0, 1.0)), _CAP_PRICES, _CAP_DATES
    )
    pos = res.positions[0].values
    assert pos.tolist() == [1.0, 1.0, 1.0, 1.0, 1.0]


@pytest.mark.asyncio
async def test_f1b_cap_flows_into_returns_and_equity():
    """The capped position drives the return calc: capped equity < uncapped."""
    uncapped = await _positions(_two_plus100_or(None), _CAP_PRICES, _CAP_DATES)
    capped = await _positions(_two_plus100_or((0.0, 1.0)), _CAP_PRICES, _CAP_DATES)
    # Prices rise every bar → both long. Uncapped is 2x leveraged, so its equity
    # gain must strictly exceed the 1x capped gain.
    assert uncapped.equity_ratio[-1] > capped.equity_ratio[-1] > 1.0
    # realized_pnl of the single input tracks the clamped position exactly:
    # at 1x, first step return = pos(=1.0) * (111-110)/110.
    exp_first = 1.0 * (111.0 - 110.0) / 110.0
    assert capped.positions[0].realized_pnl[1] == pytest.approx(exp_first)


@pytest.mark.asyncio
async def test_f1c_short_or_flat_caps_at_minus_one():
    """position_cap=(-1.0, 0.0): two -100 shorts net -1.0 (short-or-flat)."""
    sig = Signal(
        id="s",
        name="s",
        inputs=(_input("X", position_cap=(-1.0, 0.0)),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1", input_id="X", weight=-100.0, conditions=(_gt("X", 100.0),)
                ),
                Block(
                    id="e2", input_id="X", weight=-100.0, conditions=(_gt("X", 105.0),)
                ),
            )
        ),
    )
    res = await _positions(sig, _CAP_PRICES, _CAP_DATES)
    assert res.positions[0].values.tolist() == [-1.0, -1.0, -1.0, -1.0, -1.0]


@pytest.mark.asyncio
async def test_f1e_cap_wider_than_position_is_noop():
    """A cap range wider than the achieved net position leaves it untouched."""
    res = await _positions(
        _two_plus100_or(position_cap=(-5.0, 5.0)), _CAP_PRICES, _CAP_DATES
    )
    assert res.positions[0].values.tolist() == [2.0, 2.0, 2.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_f1_cap_clamps_lower_bound_when_flat():
    """A positive lower bound does NOT fabricate exposure when flat.

    The clamp is applied to the ACTUAL net latched position; np.clip of a
    flat 0.0 into (0.0, 1.0) stays 0.0 (no phantom long). Guard against a
    mis-implementation that clamps a 0 up to the lower bound.
    """
    # condition never true (price always below 100) → flat throughout
    sig = Signal(
        id="s",
        name="s",
        inputs=(_input("X", position_cap=(0.0, 1.0)),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1", input_id="X", weight=100.0, conditions=(_gt("X", 999.0),)
                ),
            )
        ),
    )
    res = await _positions(sig, _CAP_PRICES, _CAP_DATES)
    assert res.positions[0].values.tolist() == [0.0, 0.0, 0.0, 0.0, 0.0]


# =========================================================================== #
# Feature 2 — count_mode="since_reset" (unit level: _cross_since_reset)
# =========================================================================== #


def _sr(pulses, reset_fire, count):
    p = np.array(pulses, dtype=np.bool_)
    r = np.array(reset_fire, dtype=np.bool_)
    return _cross_since_reset(p, r, count).astype(int).tolist()


def test_cross_since_reset_fires_on_nth_impulse():
    # crossings at t=1,3,5 ; count=2 → fire on the 2nd crossing (t=3) only.
    pulses = [0, 1, 0, 1, 0, 1, 0]
    reset = [0, 0, 0, 0, 0, 0, 0]
    assert _sr(pulses, reset, 2) == [0, 0, 0, 1, 0, 0, 0]


def test_cross_since_reset_rearms_after_reset():
    # crossings t=1,2 (fires @2 for count=2). reset @4. crossings t=5,6 → fire @6.
    pulses = [0, 1, 1, 0, 0, 1, 1, 0]
    reset = [0, 0, 0, 0, 1, 0, 0, 0]
    assert _sr(pulses, reset, 2) == [0, 0, 1, 0, 0, 0, 1, 0]


def test_cross_since_reset_partial_progress_lost_on_reset():
    # count=3: crossings t=1,2 (only 2, no fire), reset @3 wipes the count,
    # then crossings t=4,5,6 → fire @6.
    pulses = [0, 1, 1, 0, 1, 1, 1, 0]
    reset = [0, 0, 0, 1, 0, 0, 0, 0]
    assert _sr(pulses, reset, 3) == [0, 0, 0, 0, 0, 0, 1, 0]


def test_cross_since_reset_count_one_equals_every_crossing():
    # count=1 fires on every crossing (each crossing is the 1st since reset,
    # because a fire consumes and re-arms immediately).
    pulses = [0, 1, 0, 1, 1, 0]
    reset = [0, 0, 0, 0, 0, 0]
    assert _sr(pulses, reset, 1) == [0, 1, 0, 1, 1, 0]


def test_cross_since_reset_same_bar_reset_and_crossing():
    # A reset and a crossing on the SAME bar: reset zeroes the counter, then
    # the crossing counts as the 1st-since-reset. count=1 → fire that bar.
    pulses = [0, 1, 0, 1, 0]
    reset = [0, 0, 0, 1, 0]  # reset coincident with the 2nd crossing
    # t=1: 1st crossing, count=1 fire. t=3: reset then crossing → 1st again, fire.
    assert _sr(pulses, reset, 1) == [0, 1, 0, 1, 0]


# =========================================================================== #
# Feature 2 — count_mode="since_reset" (integration through evaluate_signal)
# =========================================================================== #

# Price series engineered so up-crosses of 100 land on known bars and a reset
# (price < 90) lands between them. Grid:
#   t:      0    1    2    3    4    5    6    7    8
#   price: 95  101   95  101   85   95  101   95  101
# up-crosses of 100: t=1, t=3, t=6, t=8  (prev<=100 & cur>100)
# reset (price<90):  t=4
_SR_PRICES = [95.0, 101.0, 95.0, 101.0, 85.0, 95.0, 101.0, 95.0, 101.0]
_SR_DATES = np.arange(20240101, 20240101 + len(_SR_PRICES), dtype=np.int64)


def _since_reset_signal() -> Signal:
    return Signal(
        id="s",
        name="s",
        inputs=(_input("X"),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    name="long",
                    input_id="X",
                    weight=100.0,
                    conditions=(
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=_const(100.0),
                            count=2,
                            count_mode="since_reset",
                        ),
                    ),
                    # NB: no requires_reset_block_id here — the count reset is
                    # keyed to the reset block that this block binds to. Bound
                    # below in the integration test variant.
                    requires_reset_block_id="r1",
                ),
            ),
            resets=(
                Block(
                    id="r1",
                    name="reset",
                    conditions=(_lt("X", 90.0),),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_f2a_since_reset_fires_nth_and_rearms_through_engine():
    """The block's entry_truth fires on the 2nd up-cross since the last reset.

    Up-crosses: t=1, t=3, t=6, t=8. Reset (price<90): t=4.
    count=2 since_reset:
      * before reset: crossings t=1 (1st), t=3 (2nd → FIRE @3).
      * reset @4 wipes the count.
      * after reset: crossings t=6 (1st), t=8 (2nd → FIRE @8).
    So the *condition* is True only at t=3 and t=8.
    """
    sig = _since_reset_signal()
    res = await _positions(sig, _SR_PRICES, _SR_DATES)
    # The entry latches (opens) when the condition first fires and STAYS latched
    # (no exit), except the reset re-arms the per-block gate. Between the two
    # fires the position holds. We assert the *fire* bars via events.
    ev = {e.block_id: e for e in res.events}
    fired = list(ev["e1"].fired_indices)
    assert fired == [3, 8], f"expected condition fires at t=3,8 got {fired}"


@pytest.mark.asyncio
async def test_f2b_since_reset_differs_from_rolling_default():
    """Same signal with default rolling count=2,window=large fires DIFFERENTLY.

    rolling count=2 window=10: True once 2 crossings sit in the trailing 10-bar
    window — that is satisfied from t=3 onward and STAYS true (both crossings
    remain in a 10-window through t=6,8...). So rolling fires a CONTIGUOUS run
    from t=3, unlike the two isolated impulses of since_reset.
    """
    sig = Signal(
        id="s",
        name="s",
        inputs=(_input("X"),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    name="long",
                    input_id="X",
                    weight=100.0,
                    conditions=(
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=_const(100.0),
                            count=2,
                            window=10,
                            count_mode="rolling",
                        ),
                    ),
                ),
            ),
        ),
    )
    res = await _positions(sig, _SR_PRICES, _SR_DATES)
    ev = {e.block_id: e for e in res.events}
    fired = list(ev["e1"].fired_indices)
    # rolling: contiguous run once 2 crossings accumulate (t=3 has crossings at
    # t=1,3; stays true while >=2 remain in the trailing 10-window).
    assert fired != [3, 8]
    assert 3 in fired and 4 in fired  # contiguous, not an isolated impulse


@pytest.mark.asyncio
async def test_f2c_since_reset_no_binding_never_resets():
    """count_mode=since_reset on a block with NO requires_reset_block_id counts
    cumulatively from bar 0 (no reset events)."""
    sig = Signal(
        id="s",
        name="s",
        inputs=(_input("X"),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    name="long",
                    input_id="X",
                    weight=100.0,
                    conditions=(
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=_const(100.0),
                            count=3,
                            count_mode="since_reset",
                        ),
                    ),
                    # no requires_reset_block_id
                ),
            ),
        ),
    )
    res = await _positions(sig, _SR_PRICES, _SR_DATES)
    ev = {e.block_id: e for e in res.events}
    fired = list(ev["e1"].fired_indices)
    # up-crosses at t=1,3,6,8 → 3rd cumulative crossing (ignoring the reset since
    # it isn't bound) is at t=6.
    assert fired == [6], f"expected single fire at 3rd cumulative cross t=6 got {fired}"
