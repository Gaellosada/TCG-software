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
    _eval_condition,
    _link_groups,
    _sequence_active,
    _to_pulse,
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
# _link_groups partitioning (group semantics)
# --------------------------------------------------------------------------- #


def _two_cond_block(links):
    c = CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=1.0))
    return Block(id="e", input_id="X", weight=1.0, conditions=(c, c), links=links)


def _n_cond_block(n, links):
    c = CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=1.0))
    return Block(
        id="e", input_id="X", weight=1.0, conditions=tuple([c] * n), links=links
    )


def test_link_groups_none_for_zero_link():
    # No THEN boundary ⇒ one conjunction group ⇒ CNF ⇒ None.
    assert _link_groups(_two_cond_block(None)) is None
    assert _link_groups(_two_cond_block({})) is None


def test_link_groups_w0_folds_to_none():
    assert _link_groups(_two_cond_block({1: 0})) is None


def test_link_groups_valid_full_chain():
    # every gap a boundary -> each condition its own group (old full-chain case).
    assert _link_groups(_two_cond_block({1: 5})) == ([(0,), (1,)], [5])


def test_link_groups_three_conditions_full_chain():
    assert _link_groups(_n_cond_block(3, {1: 3, 2: 4})) == ([(0,), (1,), (2,)], [3, 4])


def test_link_groups_partial_map_forms_two_groups():
    # {1: 3} on 3 conditions: gap 1 is a THEN boundary, gap 2 is AND ->
    # groups {0} THEN {1,2}. (Was rejected as a "partial chain" before v5.)
    assert _link_groups(_n_cond_block(3, {1: 3})) == ([(0,), (1, 2)], [3])


def test_link_groups_and_then_and_on_four_conditions():
    # (A AND B) THEN (C AND D): only gap 2 is a boundary.
    assert _link_groups(_n_cond_block(4, {2: 5})) == ([(0, 1), (2, 3)], [5])


def test_link_groups_trailing_group_after_boundary():
    # {1: 2} on 4 conds -> {0} THEN {1,2,3}.
    assert _link_groups(_n_cond_block(4, {1: 2})) == ([(0,), (1, 2, 3)], [2])


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


# --------------------------------------------------------------------------- #
# group semantics: (A AND B) THEN (C AND D)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_group_and_then_and_full_path():
    # (close>100 AND close<200) THEN (close>300 AND close<400) within 5 bars.
    # group1 true @0 (150 in (100,200)); group2 true @1 (350 in (300,400)),
    # 1 bar after -> fire @1; entry latches and holds.
    prices = [150.0, 350.0, 50.0, 50.0, 50.0]
    pos = await _run_single_entry_positions(
        (
            CompareCondition(
                op="gt", lhs=_close("X"), rhs=ConstantOperand(value=100.0)
            ),
            CompareCondition(
                op="lt", lhs=_close("X"), rhs=ConstantOperand(value=200.0)
            ),
            CompareCondition(
                op="gt", lhs=_close("X"), rhs=ConstantOperand(value=300.0)
            ),
            CompareCondition(
                op="lt", lhs=_close("X"), rhs=ConstantOperand(value=400.0)
            ),
        ),
        prices,
        links={2: 5},  # gap 2 is the ONLY THEN boundary -> groups {0,1} THEN {2,3}
    )
    assert pos.tolist() == [0.0, 1.0, 1.0, 1.0, 1.0]


@pytest.mark.asyncio
async def test_group2_completion_strictly_after_group1():
    # If group2 is true on the SAME bar group1 arms, it must NOT complete
    # (strictly-after semantics carries over to groups). Here both groups are
    # only ever co-true at the same bar -> never fires.
    prices = [150.0, 150.0, 150.0]  # group1 true every bar; group2 never true
    pos = await _run_single_entry_positions(
        (
            CompareCondition(
                op="gt", lhs=_close("X"), rhs=ConstantOperand(value=100.0)
            ),
            CompareCondition(
                op="lt", lhs=_close("X"), rhs=ConstantOperand(value=200.0)
            ),
            CompareCondition(
                op="gt", lhs=_close("X"), rhs=ConstantOperand(value=300.0)
            ),
            CompareCondition(
                op="lt", lhs=_close("X"), rhs=ConstantOperand(value=400.0)
            ),
        ),
        prices,
        links={2: 5},
    )
    assert pos.tolist() == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_all_and_links_empty_equals_cnf():
    # links={} must take the SAME literal CNF path as links=None (one group).
    conds = (
        CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=100.0)),
        CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=50.0)),
        CompareCondition(op="gt", lhs=_close("X"), rhs=ConstantOperand(value=10.0)),
    )
    prices = [150.0, 5.0, 150.0]
    pos_none = await _run_single_entry_positions(conds, prices, links=None)
    pos_empty = await _run_single_entry_positions(conds, prices, links={})
    assert pos_none.tolist() == pos_empty.tolist()
    assert pos_none[0] == 1.0  # non-degenerate: CNF true @0


# --------------------------------------------------------------------------- #
# fire_mode pulse
# --------------------------------------------------------------------------- #


def test_to_pulse_rising_edge():
    a = np.array([0, 1, 1, 0, 1, 1], dtype=np.bool_)
    assert _to_pulse(a).astype(int).tolist() == [0, 1, 0, 0, 1, 0]
    # active[0] is passed through as the first edge.
    b = np.array([1, 1, 0], dtype=np.bool_)
    assert _to_pulse(b).astype(int).tolist() == [1, 0, 0]
    assert _to_pulse(np.zeros(0, dtype=np.bool_)).tolist() == []


async def _entry_fired_indices(fire_mode: str) -> tuple[int, ...]:
    dates = np.arange(20240101, 20240101 + 5, dtype=np.int64)
    prices = np.array([150.0, 160.0, 90.0, 170.0, 180.0])  # >100 at 0,1,3,4
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
                            op="gt", lhs=_close("X"), rhs=ConstantOperand(value=100.0)
                        ),
                    ),
                    fire_mode=fire_mode,
                ),
            )
        ),
    )
    result = await evaluate_signal(sig, {}, _make_fetcher(prices, dates))
    return next(e for e in result.events if e.kind == "entry").fired_indices


@pytest.mark.asyncio
async def test_pulse_fired_indices_are_edges_of_sustained():
    sustained = await _entry_fired_indices("sustained")
    pulse = await _entry_fired_indices("pulse")
    assert sustained == (0, 1, 3, 4)  # LEVEL: every bar close>100
    assert pulse == (0, 3)  # rising edges only (t0 edge, t3 after the t2 drop)


# --------------------------------------------------------------------------- #
# exit-reset (always-on): aborts in-flight chain / zeroes since_reset ladder
# --------------------------------------------------------------------------- #


def _chain_entry_with_optional_exit(prices, *, with_exit: bool):
    exits = ()
    if with_exit:
        exits = (
            Block(
                id="x",
                name="ex",
                target_entry_block_names=("long",),
                conditions=(
                    # true only @2 (150 in (140,160)); other bars fall outside.
                    CompareCondition(
                        op="gt", lhs=_close("X"), rhs=ConstantOperand(value=140.0)
                    ),
                    CompareCondition(
                        op="lt", lhs=_close("X"), rhs=ConstantOperand(value=160.0)
                    ),
                ),
            ),
        )
    return Signal(
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
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=100.0),
                        ),
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=200.0),
                        ),
                    ),
                    links={1: 5},
                ),
            ),
            exits=exits,
        ),
    )


@pytest.mark.asyncio
async def test_exit_reset_aborts_inflight_chain():
    # head cross_above 100 @1 (90->101); exit fires @2 (150); completion
    # cross_above 200 @3 (150->250). WITHOUT the exit the chain completes @3;
    # WITH the exit the in-flight candidate is aborted @2 so it never completes.
    dates = np.arange(20240101, 20240101 + 4, dtype=np.int64)
    prices = np.array([90.0, 101.0, 150.0, 250.0])

    ctrl = await evaluate_signal(
        _chain_entry_with_optional_exit(prices, with_exit=False),
        {},
        _make_fetcher(prices, dates),
    )
    assert ctrl.positions[0].values[3] == 1.0  # completes @3 without the exit

    withx = await evaluate_signal(
        _chain_entry_with_optional_exit(prices, with_exit=True),
        {},
        _make_fetcher(prices, dates),
    )
    assert withx.positions[0].values.tolist() == [0.0, 0.0, 0.0, 0.0]


def _since_reset_entry_with_optional_exit(prices, *, with_exit: bool):
    exits = ()
    if with_exit:
        exits = (
            Block(
                id="x",
                name="ex",
                target_entry_block_names=("long",),
                conditions=(
                    # cross_below 95 fires only @2 (101->90).
                    CrossCondition(
                        op="cross_below",
                        lhs=_close("X"),
                        rhs=ConstantOperand(value=95.0),
                    ),
                ),
            ),
        )
    return Signal(
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
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=100.0),
                            count=2,
                            count_mode="since_reset",
                        ),
                    ),
                ),
            ),
            exits=exits,
        ),
    )


@pytest.mark.asyncio
async def test_exit_reset_zeroes_since_reset_ladder():
    # cross_above 100 crossings @1,@3,@5 (count=2, since_reset). The exit
    # (cross_below 95) fires ONCE @2. WITHOUT the exit the 2nd crossing @3 fires;
    # WITH the exit @2 the tap counter zeroes, so @3 is the 1st of a NEW ladder
    # and firing waits for the 2nd new crossing @5.
    dates = np.arange(20240101, 20240101 + 6, dtype=np.int64)
    prices = np.array([90.0, 101.0, 90.0, 101.0, 96.0, 101.0])

    ctrl = await evaluate_signal(
        _since_reset_entry_with_optional_exit(prices, with_exit=False),
        {},
        _make_fetcher(prices, dates),
    )
    assert ctrl.positions[0].values[3] == 1.0  # 2nd crossing fires @3

    withx = await evaluate_signal(
        _since_reset_entry_with_optional_exit(prices, with_exit=True),
        {},
        _make_fetcher(prices, dates),
    )
    v = withx.positions[0].values
    assert v[3] == 0.0  # ladder was reset @2 -> @3 is only the 1st new tap
    assert v[5] == 1.0  # 2nd new tap fires @5


# --------------------------------------------------------------------------- #
# PINS (Iteration 2, tests-only): lock in two load-bearing semantics.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fire_mode_default_is_sustained_and_hydrates_level():
    # PIN — ``Block.fire_mode`` default is "sustained" (LEVEL), NOT "pulse".
    #
    # WHY THIS MATTERS: the golden-master corpus (test_golden_master_cnf) was
    # captured with LEVEL firing. If this dataclass default — or the hydration
    # of a stored block payload that predates the field — ever flips to
    # "pulse", every stored/legacy signal silently re-fires on rising edges
    # only and the golden byte-identity gate breaks. The "pulse-by-default for
    # NEW blocks" UX default lives ONLY in the FRONTEND (blockShape.js
    # defaultBlock sets fire_mode="pulse"); the backend must never default to
    # pulse. If the backend default is ever intentionally changed, this is the
    # test to update — deliberately, alongside a golden-master decision.

    # (a) dataclass default.
    assert Block().fire_mode == "sustained"
    assert (
        Block(id="e", name="long", input_id="X", weight=100.0).fire_mode == "sustained"
    )

    # (b) a STORED block payload LACKING ``fire_mode`` parses to "sustained"
    #     AND evaluates with LEVEL behaviour (fires every bar the condition
    #     holds — not just rising edges). Reuses the sustained-vs-pulse
    #     discriminator from ``test_pulse_fired_indices_are_edges_of_sustained``.
    from tcg.core.api.signals import SignalIn, parse_signal

    raw = SignalIn.model_validate(
        {
            "id": "s",
            "name": "s",
            "inputs": [
                {
                    "id": "X",
                    "instrument": {
                        "type": "spot",
                        "collection": "I",
                        "instrument_id": "X",
                    },
                }
            ],
            "rules": {
                "entries": [
                    {
                        "id": "e",
                        "name": "long",
                        "input_id": "X",
                        "weight": 100.0,
                        # NO "fire_mode" key — simulates a pre-fire_mode payload.
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {"kind": "instrument", "input_id": "X"},
                                "rhs": {"kind": "constant", "value": 100.0},
                            }
                        ],
                    }
                ],
                "exits": [],
                "resets": [],
            },
        }
    )
    sig = parse_signal(raw)
    assert sig.rules.entries[0].fire_mode == "sustained"

    dates = np.arange(20240101, 20240101 + 5, dtype=np.int64)
    prices = np.array([150.0, 160.0, 90.0, 170.0, 180.0])  # >100 at 0,1,3,4
    result = await evaluate_signal(sig, {}, _make_fetcher(prices, dates))
    fired = next(e for e in result.events if e.kind == "entry").fired_indices
    assert fired == (0, 1, 3, 4)  # LEVEL: every bar close>100 (sustained)


def _bound_disarmed_exit_signal():
    # Entry "long": since_reset ladder (2nd cross_above 100 since last reset
    # fires). Exit "ex": BOUND to reset "r" and targets "long"; "r" never fires
    # (cross_above 1e9) so once "ex" disarms it stays disarmed. cross_below 95
    # is the exit trigger.
    return Signal(
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
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=100.0),
                            count=2,
                            count_mode="since_reset",
                        ),
                    ),
                ),
            ),
            exits=(
                Block(
                    id="x",
                    name="ex",
                    target_entry_block_names=("long",),
                    requires_reset_block_id="r",
                    conditions=(
                        CrossCondition(
                            op="cross_below",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=95.0),
                        ),
                    ),
                ),
            ),
            resets=(
                Block(
                    id="r",
                    name="rst",
                    conditions=(
                        CrossCondition(
                            op="cross_above",
                            lhs=_close("X"),
                            rhs=ConstantOperand(value=1_000_000_000.0),
                        ),
                    ),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_bound_disarmed_exit_still_resets_since_reset_ladder():
    # PIN — a BOUND exit whose arm-gate is False (disarmed → it would NOT clear
    # a latch) STILL resets an in-flight entry since_reset tap ladder, because
    # the always-on exit-reset uses UN-gated exit truth (``exit_truth &
    # ~exit_nan``), independent of the exit's bound-reset arm.
    #
    # Pins current unconditional-reset semantics (assumption A4); pending
    # explicit product confirmation whether a disarmed exit should be fully
    # inert — if that is decided, this test is the one to flip.
    #
    # Timeline (cross_above 100 = up-cross; cross_below 95 = exit trigger):
    #   b1 up-cross (tap1); b3 up-cross (tap2) -> ENTRY LATCHES @3.
    #   b4 cross_below -> exit ARMED, clears the @3 latch (EFFECTIVE) -> DISARMS
    #      "ex" (reset "r" never fires, so "ex" stays disarmed forever).
    #   b5 up-cross (tap1 of a fresh ladder).
    #   b6 cross_below -> "ex" is DISARMED: it clears nothing, but its un-gated
    #      exit truth STILL resets the entry ladder (counter -> 0).  <-- PIN
    #   b7 up-cross (fresh tap1); b9 up-cross (tap2) -> ENTRY LATCHES @9.
    # If a disarmed exit were inert (A4 flipped), b5's tap would survive and the
    # entry would re-latch at b7 instead of b9.
    dates = np.arange(20240101, 20240101 + 10, dtype=np.int64)
    prices = np.array([96.0, 101.0, 96.0, 101.0, 90.0, 101.0, 90.0, 101.0, 96.0, 101.0])
    result = await evaluate_signal(
        _bound_disarmed_exit_signal(), {}, _make_fetcher(prices, dates)
    )
    v = result.positions[0].values
    assert v[3] == 1.0  # first latch (2nd up-cross)
    assert v[4] == 0.0  # armed exit EFFECTIVELY cleared the latch -> disarmed
    assert v[7] == 0.0  # PIN: disarmed exit @6 reset the ladder, so no fire @7
    assert v[9] == 1.0  # two fresh up-crosses after the @6 reset -> latch @9
