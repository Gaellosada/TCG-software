"""Engine tests for the two Phase-2 stateful signal primitives.

Primitive A — ``HysteresisCondition`` (two-threshold episode completion, SPEC
§4.1): fires a single-bar pulse when an "hits ENTER then reaches EXIT" episode
COMPLETES; re-arms afterwards; never fires for an incomplete episode; a NaN bar
holds arm state (a data gap is not an episode edge).

Primitive B — ``CompareCondition.consecutive_days`` (run-length, SPEC §5.5):
fires only where the comparison has held for the last N bars. N=1 is
byte-identical to today's single-bar compare; a False/NaN bar breaks the streak.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

from tcg.engine.signal_exec import (
    _consecutive_true,
    _hysteresis_episode,
    evaluate_signal,
)
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    HysteresisCondition,
    Input,
    InstrumentOperand,
    InstrumentSpot,
    Signal,
    SignalRules,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _make_fetcher(prices, dates) -> Callable:
    async def fetch(instrument, field):
        return dates, np.asarray(prices, dtype=np.float64)

    return fetch


def _close(iid: str = "X") -> InstrumentOperand:
    return InstrumentOperand(input_id=iid, field="close")


def _const(v: float) -> ConstantOperand:
    return ConstantOperand(value=v)


def _input(iid: str = "X") -> Input:
    return Input(id=iid, instrument=InstrumentSpot(collection="I", instrument_id=iid))


def _hyst(arr, enter, exit_, direction):
    return _hysteresis_episode(
        np.asarray(arr, dtype=np.float64),
        np.full(len(arr), enter, dtype=np.float64),
        np.full(len(arr), exit_, dtype=np.float64),
        direction,
    ).astype(int).tolist()


def _cons(truth, n):
    return _consecutive_true(np.array(truth, dtype=np.bool_), n).astype(int).tolist()


# =========================================================================== #
# Primitive A — _hysteresis_episode (unit oracle)
# =========================================================================== #


def test_hysteresis_up_fires_on_completion_and_rearms():
    # enter=95, exit=75, direction="up" == "hits 95 then descends to 75".
    #   t0 70: nothing     t1 96: ARM        t2 90: hold
    #   t3 74: FIRE+disarm  t4 80: nothing   t5 97: RE-ARM
    #   t6 76: hold (episode incomplete at series end → no fire)
    arr = [70.0, 96.0, 90.0, 74.0, 80.0, 97.0, 76.0]
    assert _hyst(arr, 95.0, 75.0, "up") == [0, 0, 0, 1, 0, 0, 0]


def test_hysteresis_up_no_fire_when_never_reaches_exit():
    # arms at t0 (96>95) but never descends below 75 → no completion, no fire.
    arr = [96.0, 90.0, 80.0, 96.0]
    assert _hyst(arr, 95.0, 75.0, "up") == [0, 0, 0, 0]


def test_hysteresis_up_no_fire_when_never_arms():
    # never rises above enter=95 → never armed → firing below exit is impossible.
    arr = [70.0, 74.0, 60.0, 74.0]
    assert _hyst(arr, 95.0, 75.0, "up") == [0, 0, 0, 0]


def test_hysteresis_down_fires_on_completion_and_rearms():
    # enter=10, exit=20, direction="down" == "hits 10 then rises to 20".
    #   t0 15: nothing  t1 8: ARM  t2 12: hold  t3 22: FIRE+disarm
    #   t4 18: nothing  t5 5: RE-ARM  t6 25: FIRE
    arr = [15.0, 8.0, 12.0, 22.0, 18.0, 5.0, 25.0]
    assert _hyst(arr, 10.0, 20.0, "down") == [0, 0, 0, 1, 0, 0, 1]


def test_hysteresis_nan_holds_arm_state_no_spurious_fire():
    # NaN mid-episode must NOT break the arm and must NOT fire:
    #   t0 96: ARM   t1 NaN: skip (still armed)   t2 74: FIRE.
    arr = [96.0, np.nan, 74.0]
    assert _hyst(arr, 95.0, 75.0, "up") == [0, 0, 1]


def test_hysteresis_two_full_episodes_two_pulses():
    # Two complete up-episodes → exactly two impulses (one per completion).
    #   ARM@1, FIRE@2 ; ARM@4, FIRE@6.
    arr = [70.0, 96.0, 74.0, 80.0, 97.0, 90.0, 70.0]
    assert _hyst(arr, 95.0, 75.0, "up") == [0, 0, 1, 0, 0, 0, 1]


# =========================================================================== #
# Primitive B — _consecutive_true (unit oracle)
# =========================================================================== #


def test_consecutive_n1_is_identity():
    truth = [True, False, True, True, False]
    assert _cons(truth, 1) == [1, 0, 1, 1, 0]


def test_consecutive_n2_run_length():
    truth = [True, True, False, True, True, True]
    # trailing-2 all True at: t1 (T,T), t4 (T,T), t5 (T,T)
    assert _cons(truth, 2) == [0, 1, 0, 0, 1, 1]


def test_consecutive_n3_run_length():
    truth = [True, True, True, True]
    # trailing-3 all True at t2 and t3.
    assert _cons(truth, 3) == [0, 0, 1, 1]


def test_consecutive_broken_streak():
    truth = [True, True, True, False, True, True]
    # n=3 fires only at t2 (T,T,T); the False at t3 breaks it and only 2 trues
    # follow so t4/t5 never reach a run of 3.
    assert _cons(truth, 3) == [0, 0, 1, 0, 0, 0]


def test_consecutive_all_false():
    assert _cons([False, False, False], 2) == [0, 0, 0]


# =========================================================================== #
# Integration through evaluate_signal
# =========================================================================== #

_HDATES = np.arange(20240101, 20240101 + 7, dtype=np.int64)


@pytest.mark.asyncio
async def test_hysteresis_integration_fires_at_completion_bar():
    sig = Signal(
        id="s",
        name="s",
        inputs=(_input("X"),),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    name="ep",
                    input_id="X",
                    weight=100.0,
                    conditions=(
                        HysteresisCondition(
                            op="hysteresis",
                            operand=_close("X"),
                            enter=_const(95.0),
                            exit=_const(75.0),
                            direction="up",
                        ),
                    ),
                ),
            )
        ),
    )
    prices = [70.0, 96.0, 90.0, 74.0, 80.0, 97.0, 76.0]
    res = await evaluate_signal(sig, {}, _make_fetcher(prices, _HDATES))
    ev = {e.block_id: e for e in res.events}
    assert list(ev["e1"].fired_indices) == [3]


@pytest.mark.asyncio
async def test_consecutive_integration_and_byte_identity_for_n1():
    # lt 3.5: truth = price<3.5. prices below engineered so a 2-day streak
    # completes only where two consecutive bars are < 3.5.
    #   prices: 5  3  4  2  1
    #   truth : F  T  F  T  T   → n=2 fires only at t=4.
    prices = [5.0, 3.0, 4.0, 2.0, 1.0]
    dates = np.arange(20240101, 20240101 + 5, dtype=np.int64)

    def _sig(cd):
        return Signal(
            id="s",
            name="s",
            inputs=(_input("X"),),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        name="c",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CompareCondition(
                                op="lt",
                                lhs=_close("X"),
                                rhs=_const(3.5),
                                consecutive_days=cd,
                            ),
                        ),
                    ),
                )
            ),
        )

    res2 = await evaluate_signal(_sig(2), {}, _make_fetcher(prices, dates))
    fired2 = list({e.block_id: e for e in res2.events}["e1"].fired_indices)
    assert fired2 == [4]

    # N=1 == default single-bar compare (byte-identical fire set).
    res1 = await evaluate_signal(_sig(1), {}, _make_fetcher(prices, dates))
    res_default = await evaluate_signal(
        Signal(
            id="s",
            name="s",
            inputs=(_input("X"),),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        name="c",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CompareCondition(op="lt", lhs=_close("X"), rhs=_const(3.5)),
                        ),
                    ),
                )
            ),
        ),
        {},
        _make_fetcher(prices, dates),
    )
    fired1 = list({e.block_id: e for e in res1.events}["e1"].fired_indices)
    fired_default = list(
        {e.block_id: e for e in res_default.events}["e1"].fired_indices
    )
    assert fired1 == fired_default == [1, 3, 4]
