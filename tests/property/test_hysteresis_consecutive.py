"""Hypothesis properties for the Phase-2 stateful signal primitives.

Properties proven here:
  * H1 hysteresis NEVER fires without a completed enter->exit episode: every
    fire bar is preceded (since the last fire / start) by an arming bar with no
    intervening fire, and the fire bar itself crosses the exit threshold in the
    episode's direction. Equivalent statement: a fire implies the latch was
    armed and the operand reached the exit side.
  * H2 hysteresis fire count == number of completed episodes counted by an
    independent scalar reference oracle.
  * H3 hysteresis fire bars ⊆ arm-then-exit reachable bars (no fire on a NaN
    bar; no fire while disarmed).
  * C1 N-consecutive equivalent to a run-length >= N over the raw compare truth
    (independent brute-force reference).
  * C2 consecutive_days=1 is byte-identical to the raw single-bar compare.

Mirrors ``tests/property/test_temporal_automaton.py`` (same operand-key wiring
through ``_eval_condition``).
"""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

import tcg.engine.signal_exec as se
from tcg.engine.signal_exec import (
    _consecutive_true,
    _eval_condition,
    _hysteresis_episode,
)
from tcg.types.signal import (
    CompareCondition,
    ConstantOperand,
    HysteresisCondition,
    Input,
    InstrumentOperand,
    InstrumentSpot,
)


# --------------------------------------------------------------------------- #
# strategies
# --------------------------------------------------------------------------- #

_VALS = st.floats(min_value=-5.0, max_value=5.0, allow_nan=False, allow_infinity=False)


@st.composite
def _series_with_nan(draw, lo=1, hi=40):
    T = draw(st.integers(min_value=lo, max_value=hi))
    vals = draw(st.lists(_VALS, min_size=T, max_size=T))
    arr = np.array(vals, dtype=np.float64)
    n_nan = draw(st.integers(min_value=0, max_value=min(4, T)))
    if n_nan:
        idx = draw(
            st.lists(
                st.integers(min_value=0, max_value=T - 1),
                min_size=n_nan,
                max_size=n_nan,
            )
        )
        arr[idx] = np.nan
    return arr


# --------------------------------------------------------------------------- #
# reference oracles (independent re-implementations)
# --------------------------------------------------------------------------- #


def _ref_hysteresis(operand, enter, exit_, direction):
    """Scalar reference: identical spec, written independently for cross-check."""
    T = operand.size
    out = np.zeros(T, dtype=np.bool_)
    armed = False
    for t in range(T):
        x, e, xt = operand[t], enter[t], exit_[t]
        if np.isnan(x) or np.isnan(e) or np.isnan(xt):
            continue
        if direction == "up":
            fire = armed and x < xt
            arm = x > e
        else:
            fire = armed and x > xt
            arm = x < e
        if fire:
            out[t] = True
            armed = False
        elif arm:
            armed = True
    return out


def _ref_runlength(truth, n):
    """Brute-force run-length >= n reference."""
    T = truth.size
    out = np.zeros(T, dtype=np.bool_)
    for t in range(T):
        if t - n + 1 < 0:
            continue
        if bool(np.all(truth[t - n + 1 : t + 1])):
            out[t] = True
    return out


# --------------------------------------------------------------------------- #
# H — hysteresis
# --------------------------------------------------------------------------- #


@settings(max_examples=400, deadline=None)
@given(
    _series_with_nan(),
    st.floats(min_value=0.5, max_value=4.0),
    st.floats(min_value=-4.0, max_value=-0.5),
    st.sampled_from(["up", "down"]),
)
def test_hysteresis_never_fires_without_completed_episode(arr, enter_v, exit_v, direction):
    # For "up" use enter > exit (e.g. +2 / -2); for "down" swap so enter < exit.
    if direction == "up":
        enter = np.full(arr.size, enter_v, dtype=np.float64)
        exit_ = np.full(arr.size, exit_v, dtype=np.float64)
    else:
        enter = np.full(arr.size, exit_v, dtype=np.float64)
        exit_ = np.full(arr.size, enter_v, dtype=np.float64)

    out = _hysteresis_episode(arr, enter, exit_, direction)

    # Independent re-walk: every fire must be preceded by an arm since the last
    # fire, and the fire bar must be on the exit side. Also no fire on NaN bars.
    armed = False
    for t in range(arr.size):
        x, e, xt = arr[t], enter[t], exit_[t]
        if np.isnan(x) or np.isnan(e) or np.isnan(xt):
            assert not out[t]  # H3: no fire on a NaN bar
            continue
        if direction == "up":
            expect_fire = armed and x < xt
            arm = x > e
        else:
            expect_fire = armed and x > xt
            arm = x < e
        assert bool(out[t]) == bool(expect_fire)
        if out[t]:
            assert armed  # H1: never fires while disarmed
            armed = False
        elif arm:
            armed = True


@settings(max_examples=400, deadline=None)
@given(_series_with_nan(), st.sampled_from(["up", "down"]))
def test_hysteresis_matches_reference_oracle(arr, direction):
    if direction == "up":
        enter = np.full(arr.size, 2.0, dtype=np.float64)
        exit_ = np.full(arr.size, -2.0, dtype=np.float64)
    else:
        enter = np.full(arr.size, -2.0, dtype=np.float64)
        exit_ = np.full(arr.size, 2.0, dtype=np.float64)
    got = _hysteresis_episode(arr, enter, exit_, direction)
    want = _ref_hysteresis(arr, enter, exit_, direction)
    assert np.array_equal(got, want)


@settings(max_examples=200, deadline=None)
@given(_series_with_nan(), st.sampled_from(["up", "down"]))
def test_hysteresis_threads_through_eval_condition(arr, direction):
    inputs = {
        "X": Input(id="X", instrument=InstrumentSpot(collection="I", instrument_id="X"))
    }
    if direction == "up":
        e_v, x_v = 2.0, -2.0
    else:
        e_v, x_v = -2.0, 2.0
    cond = HysteresisCondition(
        op="hysteresis",
        operand=InstrumentOperand(input_id="X", field="close"),
        enter=ConstantOperand(value=e_v),
        exit=ConstantOperand(value=x_v),
        direction=direction,
    )
    k_op = se._operand_key(cond.operand, {}, inputs)
    k_en = se._operand_key(cond.enter, {}, inputs)
    k_ex = se._operand_key(cond.exit, {}, inputs)
    vbk = {
        k_op: arr,
        k_en: np.full(arr.size, e_v, dtype=np.float64),
        k_ex: np.full(arr.size, x_v, dtype=np.float64),
    }
    got, _nan = _eval_condition(cond, {}, inputs, vbk, arr.size)
    want = _ref_hysteresis(
        arr,
        np.full(arr.size, e_v, dtype=np.float64),
        np.full(arr.size, x_v, dtype=np.float64),
        direction,
    )
    assert np.array_equal(got, want)


# --------------------------------------------------------------------------- #
# C — N-consecutive-days
# --------------------------------------------------------------------------- #


@settings(max_examples=400, deadline=None)
@given(
    st.lists(st.booleans(), min_size=0, max_size=40),
    st.integers(min_value=1, max_value=6),
)
def test_consecutive_equivalent_to_runlength(truth_list, n):
    truth = np.array(truth_list, dtype=np.bool_)
    got = _consecutive_true(truth, n)
    want = _ref_runlength(truth, n)
    assert np.array_equal(got, want)


@settings(max_examples=300, deadline=None)
@given(_series_with_nan(), st.sampled_from(["gt", "lt", "ge", "le", "eq"]))
def test_consecutive_days_one_byte_identical_to_single_bar(arr, op):
    inputs = {
        "X": Input(id="X", instrument=InstrumentSpot(collection="I", instrument_id="X"))
    }
    base = CompareCondition(
        op=op,
        lhs=InstrumentOperand(input_id="X", field="close"),
        rhs=ConstantOperand(value=0.0),
    )
    withN = CompareCondition(
        op=op,
        lhs=InstrumentOperand(input_id="X", field="close"),
        rhs=ConstantOperand(value=0.0),
        consecutive_days=1,
    )
    k_lhs = se._operand_key(base.lhs, {}, inputs)
    k_rhs = se._operand_key(base.rhs, {}, inputs)
    vbk = {k_lhs: arr, k_rhs: np.zeros(arr.size, dtype=np.float64)}
    got_base, nan_base = _eval_condition(base, {}, inputs, vbk, arr.size)
    got_n1, nan_n1 = _eval_condition(withN, {}, inputs, vbk, arr.size)
    assert np.array_equal(got_base, got_n1)
    assert np.array_equal(nan_base, nan_n1)
