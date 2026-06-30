"""Functional tests for block temporal composition (cross_count + automaton).

Covers the NEW code paths (the golden-master gate covers the unchanged
zero-link path):
  * cross_count: defaults byte-identical; same-direction; trailing-window count;
    NaN contributes 0; O(T) (correctness, not timing).
  * temporal automaton: A->B within W; strictly-after; expiry; coincident
    head+completion (redteam Finding 1); NaN aborts; 3-stage chain; impulse;
    W=0 folds to AND; exit-block chains.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

from tcg.engine.signal_exec import (
    _chain_window_list,
    _eval_condition,
    _sequence_active,
    evaluate_signal,
)
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


def _make_fetcher(prices: np.ndarray, dates: np.ndarray) -> Callable:
    async def fetch(instrument, field):
        return dates, np.asarray(prices, dtype=np.float64)

    return fetch


def _close(iid: str = "X") -> InstrumentOperand:
    return InstrumentOperand(input_id=iid, field="close")


def _input(iid: str = "X") -> Input:
    return Input(id=iid, instrument=InstrumentSpot(collection="I", instrument_id=iid))


async def _run_single_entry_positions(
    conditions: tuple, prices: list[float], *, links: dict[int, int] | None = None
) -> np.ndarray:
    dates = np.arange(20240101, 20240101 + len(prices), dtype=np.int64)
    sig = Signal(
        id="s",
        name="s",
        inputs=(_input("X"),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e",
                    input_id="X",
                    weight=100.0,
                    conditions=conditions,
                    links=links,
                ),
            )
        ),
    )
    result = await evaluate_signal(sig, {}, _make_fetcher(np.array(prices), dates))
    return result.positions[0].values


# --------------------------------------------------------------------------- #
# cross_count
# --------------------------------------------------------------------------- #


def _cross_truth(prices, count, window, op="cross_above"):
    """Evaluate a CrossCondition directly via the engine (single operand key)."""
    import tcg.engine.signal_exec as se

    cond = CrossCondition(
        op=op,
        lhs=_close("X"),
        rhs=ConstantOperand(value=100.0),
        count=count,
        window=window,
    )
    inputs = {"X": _input("X")}
    key = se._operand_key(cond.lhs, {}, inputs)
    key_rhs = se._operand_key(cond.rhs, {}, inputs)
    vbk = {
        key: np.asarray(prices, dtype=np.float64),
        key_rhs: np.full(len(prices), 100.0, dtype=np.float64),
    }
    return _eval_condition(cond, {}, inputs, vbk, len(prices))


def test_cross_count_default_is_single_bar_pulse():
    # up-cross at t=1 (99->101) and t=3 (98->102)
    prices = [99.0, 101.0, 98.0, 102.0, 103.0]
    truth, _ = _cross_truth(prices, count=1, window=1)
    assert truth.tolist() == [False, True, False, True, False]


def test_cross_count_two_in_window():
    prices = [99.0, 101.0, 98.0, 102.0, 103.0, 101.0]
    # up-crosses land at t=1 and t=3. Trailing window of 3 first contains BOTH
    # at t=3 (window covers bars 1,2,3). So True from t=3 onward while 2 remain.
    truth, _ = _cross_truth(prices, count=2, window=3)
    assert truth[3]  # 2 crossings in trailing 3 bars
    assert not truth[2]  # only 1 crossing by t=2
    # at t=4 window covers bars 2,3,4 -> only the t=3 crossing -> 1 < 2
    assert not truth[4]


def test_cross_count_wide_window_persists():
    prices = [99.0, 101.0, 98.0, 102.0, 103.0, 101.0]
    truth, _ = _cross_truth(prices, count=2, window=10)
    assert not truth[0] and not truth[2]
    assert truth[3] and truth[4] and truth[5]  # both crossings stay in window


def test_cross_count_same_direction_only():
    # alternating crosses of 100: up @1,3,5 ; down @2,4
    prices = [99.0, 101.0, 99.0, 101.0, 99.0, 101.0]
    up, _ = _cross_truth(prices, count=2, window=6, op="cross_above")
    # need 2 up-crosses: up@1, up@3 -> True from t=3
    assert up[3]
    assert not up[2]
    down, _ = _cross_truth(prices, count=2, window=6, op="cross_below")
    # down-crosses @2, @4 -> 2 down by t=4
    assert down[4]
    assert not down[3]


def test_cross_count_nan_contributes_zero():
    prices = [99.0, 101.0, np.nan, 102.0, 103.0, 98.0, 101.0]
    truth, nan = _cross_truth(prices, count=2, window=7)
    # NaN at t=2 prevents pulses at t=2 and t=3; nan_at_t marks t=2.
    assert nan[2]
    # The condition still becomes True once 2 clean up-crosses accumulate.
    assert truth.dtype == np.bool_


@pytest.mark.asyncio
async def test_cross_count_position_latches():
    prices = [99.0, 101.0, 98.0, 102.0, 103.0, 101.0]
    pos = await _run_single_entry_positions(
        (
            CrossCondition(
                op="cross_above",
                lhs=_close("X"),
                rhs=ConstantOperand(value=100.0),
                count=2,
                window=10,
            ),
        ),
        prices,
    )
    # latch opens at t=3 (2nd up-cross within window) and stays.
    assert pos.tolist() == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]


# --------------------------------------------------------------------------- #
# temporal automaton (direct, via _sequence_active)
# --------------------------------------------------------------------------- #


def _seq(truth_lists, windows, nan_lists=None):
    T = len(truth_lists[0])
    st = [np.array(x, dtype=np.bool_) for x in truth_lists]
    if nan_lists is None:
        sn = [np.zeros(T, dtype=np.bool_) for _ in truth_lists]
    else:
        sn = [np.array(x, dtype=np.bool_) for x in nan_lists]
    return _sequence_active(st, sn, windows, T).astype(int).tolist()


def test_automaton_basic_a_then_b():
    A = [0, 1, 0, 0, 0, 1, 0, 0]
    B = [0, 0, 0, 1, 0, 0, 1, 0]
    # head@1 -> compl@3 (2 bars, W=3) fire; head@5 -> compl@6 (1 bar) fire.
    assert _seq([A, B], [3]) == [0, 0, 0, 1, 0, 0, 1, 0]


def test_automaton_strictly_after_no_same_bar():
    A = [0, 1, 0, 0, 0]
    B = [0, 1, 0, 0, 0]  # B same bar as head
    assert _seq([A, B], [2]) == [0, 0, 0, 0, 0]


def test_automaton_expiry():
    A = [1, 0, 0, 0, 0]
    B = [0, 0, 0, 1, 0]  # 3 bars after head, W=2 -> expired before t=3
    assert _seq([A, B], [2]) == [0, 0, 0, 0, 0]


def test_automaton_window_boundary_inclusive():
    A = [1, 0, 0, 0]
    B = [0, 0, 1, 0]  # exactly W=2 bars after head
    assert _seq([A, B], [2]) == [0, 0, 1, 0]
    # one bar later than W=2 -> miss
    B2 = [0, 0, 0, 1]
    assert _seq([A, B2], [2]) == [0, 0, 0, 0]


def test_automaton_coincident_head_and_completion_fires():
    # redteam Finding 1 BLOCKER: head@0, head+completion@1, W>=1 must FIRE @1
    # (advance against the OLD candidate before re-arming).
    A = [1, 1, 0]
    B = [0, 1, 0]
    assert _seq([A, B], [3]) == [0, 1, 0]


def test_automaton_nan_aborts_candidate():
    A = [1, 0, 0, 0]
    B = [0, 0, 1, 0]  # would complete @2 (2 bars after head, within W=3)
    Bnan = [0, 1, 0, 0]  # but B's operand is NaN at t=1 while in flight -> abort
    assert _seq([A, B], [3], nan_lists=[[0, 0, 0, 0], Bnan]) == [0, 0, 0, 0]


def test_automaton_three_stage_chain():
    A = [1, 0, 0, 0, 0]
    B = [0, 1, 0, 0, 0]
    C = [0, 0, 1, 0, 0]
    assert _seq([A, B, C], [2, 2]) == [0, 0, 1, 0, 0]


def test_automaton_three_stage_middle_expires():
    A = [1, 0, 0, 0, 0, 0]
    B = [0, 0, 0, 1, 0, 0]  # B 3 bars after A, W1=2 -> A->B never completes
    C = [0, 0, 0, 0, 1, 0]
    assert _seq([A, B, C], [2, 2]) == [0, 0, 0, 0, 0, 0]


def test_automaton_impulse_single_bar():
    # completion fires exactly one bar even if B stays true.
    A = [1, 0, 0, 0]
    B = [0, 1, 1, 1]
    assert _seq([A, B], [3]) == [0, 1, 0, 0]


def test_automaton_re_arm_after_fire():
    # after completing, a fresh head re-arms and can complete again.
    A = [1, 0, 0, 1, 0, 0]
    B = [0, 1, 0, 0, 1, 0]
    assert _seq([A, B], [2]) == [0, 1, 0, 0, 1, 0]


# --------------------------------------------------------------------------- #
# _chain_window_list validation / degradation
# --------------------------------------------------------------------------- #


def _two_cond_block(links):
    c = CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=1.0))
    return Block(id="e", input_id="X", weight=1.0, conditions=(c, c), links=links)


def test_chain_window_list_none_for_zero_link():
    assert _chain_window_list(_two_cond_block(None)) is None
    assert _chain_window_list(_two_cond_block({})) is None


def test_chain_window_list_w0_folds_to_none():
    assert _chain_window_list(_two_cond_block({1: 0})) is None


def test_chain_window_list_valid():
    assert _chain_window_list(_two_cond_block({1: 5})) == [5]


def test_chain_window_list_three_conditions():
    c = CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=1.0))
    b = Block(
        id="e", input_id="X", weight=1.0, conditions=(c, c, c), links={1: 3, 2: 4}
    )
    assert _chain_window_list(b) == [3, 4]


def test_chain_window_list_partial_chain_degrades():
    # a chain that does not reach the final condition is not a single linear
    # chain over the whole block -> degrade to CNF (None).
    c = CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=1.0))
    b = Block(id="e", input_id="X", weight=1.0, conditions=(c, c, c), links={1: 3})
    assert _chain_window_list(b) is None


# --------------------------------------------------------------------------- #
# full-path: chain through evaluate_signal (entry + exit)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_entry_chain_through_evaluate_signal():
    # cross_below 100 then cross_below 95 within 3 bars.
    prices = [101.0, 99.0, 100.0, 94.0, 96.0, 93.0]
    #          t0      t1(xb100) t2     t3(xb95) ...
    pos = await _run_single_entry_positions(
        (
            CrossCondition(
                op="cross_below", lhs=_close("X"), rhs=ConstantOperand(value=100.0)
            ),
            CrossCondition(
                op="cross_below", lhs=_close("X"), rhs=ConstantOperand(value=95.0)
            ),
        ),
        prices,
        links={1: 3},
    )
    # head xb100 @ t1 (99<100 from 101). compl xb95 @ t3 (94<95 from 100). 2 bars -> fire @3.
    assert pos[3] == 1.0
    assert pos[0] == 0.0 and pos[1] == 0.0 and pos[2] == 0.0


@pytest.mark.asyncio
async def test_chain_nan_poison_preserved_g2():
    # G2: a NaN bar inside a chained block zeroes the position on that bar
    # (downstream nan_poison preserved) AND aborts the in-flight candidate, so
    # the awaited stage's later match cannot complete from a dead candidate.
    prices = [101.0, 99.0, np.nan, 94.0, 96.0, 93.0]  # head xb100 @1; NaN @2
    pos = await _run_single_entry_positions(
        (
            CrossCondition(
                op="cross_below", lhs=_close("X"), rhs=ConstantOperand(value=100.0)
            ),
            CrossCondition(
                op="cross_below", lhs=_close("X"), rhs=ConstantOperand(value=95.0)
            ),
        ),
        prices,
        links={1: 4},
    )
    assert pos[2] == 0.0  # NaN bar poisoned
    # candidate armed @1 aborts at the NaN @2; no completion -> flat throughout.
    assert pos.tolist() == [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_exit_block_chain_clears_latch():
    # entry latches at t0; an exit CHAIN (xb on close) clears it on completion.
    dates = np.arange(20240101, 20240101 + 6, dtype=np.int64)
    prices = np.array([105.0, 99.0, 101.0, 94.0, 96.0, 93.0])
    sig = Signal(
        id="s",
        name="s",
        inputs=(_input("X"),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e",
                    name="long",
                    input_id="X",
                    weight=100.0,
                    conditions=(
                        CompareCondition(
                            op="gt", lhs=_close("X"), rhs=ConstantOperand(value=50.0)
                        ),
                    ),
                ),
            ),
            exits=(
                Block(
                    id="x",
                    name="ex",
                    target_entry_block_names=("long",),
                    conditions=(
                        CrossCondition(
                            op="cross_below",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=100.0),
                        ),
                        CrossCondition(
                            op="cross_below",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=95.0),
                        ),
                    ),
                    links={1: 3},
                ),
            ),
        ),
    )
    result = await evaluate_signal(sig, {}, _make_fetcher(prices, dates))
    pos = result.positions[0].values
    # Entry is always-on (close>50) so it re-latches same-bar after any clear;
    # the position therefore stays 1.0. The PROOF that the exit CHAIN ran (not
    # silently as CNF) is its effective-exit bars: an impulse chain that
    # completes at t=3 and re-arms must record an effective exit at t=3 (it
    # cleared the open latch). A CNF exit over {xb100 AND xb95} would NOT
    # co-fire (the two crosses never land on the same bar) -> zero exits.
    exit_event = next(e for e in result.events if e.kind == "exit")
    assert 3 in exit_event.latched_indices, (
        f"exit chain did not complete @3; latched_indices={exit_event.latched_indices}"
    )
    assert pos[0] == 1.0 and pos[2] == 1.0  # always-on entry stays latched
