"""Golden-master signal corpus for the temporal-composition regression gate (G1).

This module is the SINGLE source of truth for the corpus signals. Both the
fixture generator (``gen_golden_master.py``) and the regression test
(``test_golden_master_cnf.py``) import :func:`build_corpus` from here so the
*exact same* zero-link signals are exercised before and after the engine change.

Every signal here is ZERO-LINK (``Block.links`` is ``None``/absent) and uses only
the condition vocabulary that exists today (compare / cross / in_range / rolling),
across entry / exit / reset blocks, single- and multi-condition CNF, single- and
multi-input, plus deliberate NaN holes and warm-up regions. If the engine change
is truly a no-op for zero-link blocks, every signal's ``(index, positions)`` is
byte-identical before and after.

The builders return ``(Signal, fetcher)`` pairs keyed by a stable name. The
fetcher is the same ``_make_fetcher`` used across the engine test-suite.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    CrossCondition,
    InRangeCondition,
    Input,
    InstrumentOperand,
    InstrumentSpot,
    RollingCondition,
    Signal,
    SignalRules,
)

# A longer, deterministic date grid so rolling lookbacks and windows have room.
_N = 40
DATES = np.array([20240000 + d for d in range(100, 100 + _N)], dtype=np.int64)


def _make_fetcher(
    by_key: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]],
) -> Callable:
    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            key = (instrument.collection, instrument.instrument_id)
        else:
            key = ("continuous", instrument.collection)
        if key not in by_key:
            raise KeyError(f"no data for {key!r} ({field})")
        return by_key[key]

    return fetch


def _close(input_id: str) -> InstrumentOperand:
    return InstrumentOperand(input_id=input_id, field="close")


def _const(v: float) -> ConstantOperand:
    return ConstantOperand(value=v)


INPUT_X = Input(
    id="X", instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX")
)
INPUT_Y = Input(
    id="Y", instrument=InstrumentSpot(collection="INDEX", instrument_id="NDX")
)


# Deterministic, varied price series with crossings, trends and a NaN hole.
def _series_spx() -> np.ndarray:
    # oscillating around 100 with a couple of clean crossings of 100 and 105
    base = 100.0 + 6.0 * np.sin(np.arange(_N) / 3.0)
    base[10] = np.nan  # a single NaN hole
    base[11] = np.nan  # widen to two bars (tests cross-pair NaN guard)
    return base


def _series_ndx() -> np.ndarray:
    base = 200.0 + np.linspace(-15.0, 15.0, _N)  # monotone trend
    base[20] = np.nan
    return base


def build_corpus() -> dict[str, tuple[Signal, Callable]]:
    """Return the full named corpus of zero-link signals + their fetchers."""
    spx = _series_spx()
    ndx = _series_ndx()
    fx = _make_fetcher({("INDEX", "SPX"): (DATES, spx)})
    fxy = _make_fetcher(
        {("INDEX", "SPX"): (DATES, spx), ("INDEX", "NDX"): (DATES, ndx)}
    )

    out: dict[str, tuple[Signal, Callable]] = {}

    # 1. single long, single compare condition
    out["single_long_compare"] = (
        Signal(
            id="s1",
            name="s1",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("X"), rhs=_const(101.0)
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 2. single short via negative weight
    out["single_short_compare"] = (
        Signal(
            id="s2",
            name="s2",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=-50.0,
                        conditions=(
                            CompareCondition(
                                op="lt", lhs=_close("X"), rhs=_const(99.0)
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 3. multi-condition CNF (AND of compare + in_range) — single block
    out["multi_cond_cnf"] = (
        Signal(
            id="s3",
            name="s3",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=80.0,
                        conditions=(
                            CompareCondition(
                                op="ge", lhs=_close("X"), rhs=_const(98.0)
                            ),
                            InRangeCondition(
                                op="in_range",
                                operand=_close("X"),
                                min=_const(95.0),
                                max=_const(108.0),
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 4. cross_above condition (default single-bar cross — the byte-identical path)
    out["cross_above_default"] = (
        Signal(
            id="s4",
            name="s4",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CrossCondition(
                                op="cross_above", lhs=_close("X"), rhs=_const(100.0)
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 5. cross_below condition
    out["cross_below_default"] = (
        Signal(
            id="s5",
            name="s5",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CrossCondition(
                                op="cross_below", lhs=_close("X"), rhs=_const(100.0)
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 6. rolling_gt / rolling_lt (must stay evaluable, byte-identical)
    out["rolling_gt"] = (
        Signal(
            id="s6",
            name="s6",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            RollingCondition(
                                op="rolling_gt", operand=_close("X"), lookback=3
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )
    out["rolling_lt"] = (
        Signal(
            id="s6b",
            name="s6b",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=-100.0,
                        conditions=(
                            RollingCondition(
                                op="rolling_lt", operand=_close("X"), lookback=5
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 7. multi-block OR within a section (two entry blocks, same input)
    out["multi_block_or"] = (
        Signal(
            id="s7",
            name="s7",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=40.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("X"), rhs=_const(104.0)
                            ),
                        ),
                    ),
                    Block(
                        id="e2",
                        input_id="X",
                        weight=30.0,
                        conditions=(
                            CompareCondition(
                                op="lt", lhs=_close("X"), rhs=_const(96.0)
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 8. entry + exit (exit clears the entry by name)
    out["entry_exit"] = (
        Signal(
            id="s8",
            name="s8",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        name="long",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("X"), rhs=_const(100.0)
                            ),
                        ),
                    ),
                ),
                exits=(
                    Block(
                        id="x1",
                        name="exit-long",
                        target_entry_block_names=("long",),
                        conditions=(
                            CompareCondition(
                                op="lt", lhs=_close("X"), rhs=_const(98.0)
                            ),
                        ),
                    ),
                ),
            ),
        ),
        fx,
    )

    # 9. two inputs independent positions
    out["two_inputs"] = (
        Signal(
            id="s9",
            name="s9",
            inputs=(INPUT_X, INPUT_Y),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("X"), rhs=_const(100.0)
                            ),
                        ),
                    ),
                    Block(
                        id="e2",
                        input_id="Y",
                        weight=-100.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("Y"), rhs=_const(200.0)
                            ),
                        ),
                    ),
                )
            ),
        ),
        fxy,
    )

    # 10. entry + exit + reset binding (the reset/arm machine)
    out["entry_exit_reset"] = (
        Signal(
            id="s10",
            name="s10",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        name="long",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CrossCondition(
                                op="cross_above", lhs=_close("X"), rhs=_const(100.0)
                            ),
                        ),
                        requires_reset_block_id="r1",
                        requires_reset_count=1,
                    ),
                ),
                exits=(
                    Block(
                        id="x1",
                        name="exit-long",
                        target_entry_block_names=("long",),
                        conditions=(
                            CrossCondition(
                                op="cross_below", lhs=_close("X"), rhs=_const(100.0)
                            ),
                        ),
                    ),
                ),
                resets=(
                    Block(
                        id="r1",
                        name="reset",
                        conditions=(
                            CompareCondition(
                                op="lt", lhs=_close("X"), rhs=_const(95.0)
                            ),
                        ),
                    ),
                ),
            ),
        ),
        fx,
    )

    # 11. multi-target cross-input exit
    out["multi_target_exit_cross_input"] = (
        Signal(
            id="s11",
            name="s11",
            inputs=(INPUT_X, INPUT_Y),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        name="lx",
                        input_id="X",
                        weight=60.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("X"), rhs=_const(100.0)
                            ),
                        ),
                    ),
                    Block(
                        id="e2",
                        name="ly",
                        input_id="Y",
                        weight=70.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("Y"), rhs=_const(198.0)
                            ),
                        ),
                    ),
                ),
                exits=(
                    Block(
                        id="x1",
                        name="flat-all",
                        target_entry_block_names=("lx", "ly"),
                        conditions=(
                            CompareCondition(
                                op="lt", lhs=_close("X"), rhs=_const(94.0)
                            ),
                        ),
                    ),
                ),
            ),
        ),
        fxy,
    )

    # 12. multi-condition cross + compare (two crosses + a level filter) — pure CNF
    out["cnf_cross_and_compare"] = (
        Signal(
            id="s12",
            name="s12",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CrossCondition(
                                op="cross_above", lhs=_close("X"), rhs=_const(100.0)
                            ),
                            CompareCondition(
                                op="lt", lhs=_close("X"), rhs=_const(110.0)
                            ),
                        ),
                    ),
                )
            ),
        ),
        fx,
    )

    # 13. reset with count > 1 (cumulative re-arm)
    out["reset_count_two"] = (
        Signal(
            id="s13",
            name="s13",
            inputs=(INPUT_X,),
            rules=SignalRules(
                entries=(
                    Block(
                        id="e1",
                        name="long",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CompareCondition(
                                op="gt", lhs=_close("X"), rhs=_const(104.0)
                            ),
                        ),
                        requires_reset_block_id="r1",
                        requires_reset_count=2,
                    ),
                ),
                resets=(
                    Block(
                        id="r1",
                        name="reset",
                        conditions=(
                            CompareCondition(
                                op="lt", lhs=_close("X"), rhs=_const(96.0)
                            ),
                        ),
                    ),
                ),
            ),
        ),
        fx,
    )

    return out
