"""Engine tests for signal_exec v4 (unified Entries/Exits, signed weights).

Exercises:
  - signed-weight semantics (long = weight > 0, short = weight < 0);
  - per-target-entry exit clearing (NOT same-side-under-input);
  - latching persistence;
  - cross-input / multi-entry leverage;
  - indicator operand input_id binding;
  - duplicate-input id rejection.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

from tcg.engine.signal_exec import (
    IndicatorSpecInput,
    SignalDataError,
    SignalValidationError,
    evaluate_signal,
)
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    IndicatorOperand,
    Input,
    InstrumentOperand,
    InstrumentSpot,
    Signal,
    SignalRules,
)


DATES = np.array([20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64)


def _make_fetcher(
    by_key: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]],
) -> Callable:
    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            key = (instrument.collection, instrument.instrument_id)
        else:
            key = ("continuous", instrument.collection)
        if key not in by_key:
            raise SignalDataError(f"no data for {key!r} ({field})")
        return by_key[key]

    return fetch


INPUT_X = Input(
    id="X",
    instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
)
INPUT_Y = Input(
    id="Y",
    instrument=InstrumentSpot(collection="INDEX", instrument_id="NDX"),
)


@pytest.mark.asyncio
async def test_single_long_via_positive_weight():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="X",
                    weight=100.0,  # full long
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X", field="close"),
                            rhs=ConstantOperand(value=11.5),
                        ),
                    ),
                ),
            ),
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.positions) == 1
    p = result.positions[0]
    assert p.input_id == "X"
    assert list(p.values) == pytest.approx([0.0, 0.0, 1.0, 1.0, 1.0])


@pytest.mark.asyncio
async def test_single_short_via_negative_weight():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="X",
                    weight=-50.0,  # 0.5 short
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=ConstantOperand(value=11.5),
                        ),
                    ),
                ),
            ),
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    p = result.positions[0]
    assert list(p.values) == pytest.approx([0.0, 0.0, -0.5, -0.5, -0.5])


@pytest.mark.asyncio
async def test_two_inputs_independent_positions():
    spx = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    ndx = np.array([100.0, 99.0, 98.0, 97.0, 96.0])
    fetcher = _make_fetcher(
        {("INDEX", "SPX"): (DATES, spx), ("INDEX", "NDX"): (DATES, ndx)}
    )

    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X, INPUT_Y),
        rules=SignalRules(
            entries=(
                Block(
                    id="eX",
                    input_id="X",
                    weight=60.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=ConstantOperand(value=11.5),
                        ),
                    ),
                ),
                Block(
                    id="eY",
                    input_id="Y",
                    weight=-40.0,
                    conditions=(
                        CompareCondition(
                            op="lt",
                            lhs=InstrumentOperand(input_id="Y"),
                            rhs=ConstantOperand(value=99.5),
                        ),
                    ),
                ),
            ),
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    by_id = {p.input_id: p for p in result.positions}
    assert list(by_id["X"].values) == pytest.approx([0.0, 0.0, 0.6, 0.6, 0.6])
    assert list(by_id["Y"].values) == pytest.approx([0.0, -0.4, -0.4, -0.4, -0.4])


@pytest.mark.asyncio
async def test_unusable_block_without_input_id_skipped():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="",  # unusable: no input
                    weight=100.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert result.positions == ()


@pytest.mark.asyncio
async def test_entry_weight_zero_skipped_by_engine():
    """Engine treats weight==0 as an unusable block (skipped)."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="X",
                    weight=0.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert result.positions == ()


@pytest.mark.asyncio
async def test_two_long_entries_leverage_sums():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    cond = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=0.0),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(id="a", input_id="X", weight=80.0, conditions=(cond,)),
                Block(id="b", input_id="X", weight=80.0, conditions=(cond,)),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert list(result.positions[0].values) == pytest.approx([1.6] * 5)


@pytest.mark.asyncio
async def test_exit_reentry_same_bar_relatches():
    """Entry cond true every bar; exit targets entry and fires at t=3,4.
    Clear-pass then entry-pass within the same bar: exit clears, entry
    re-latches → position stays 1.0."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=0.0),
    )
    exit_c = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=12.5),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E",
                    name="Entry",
                    input_id="X",
                    weight=100.0,
                    conditions=(entry,),
                ),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_c,),
                    target_entry_block_names=("Entry",),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = list(result.positions[0].values)
    assert vals == pytest.approx([1.0, 1.0, 1.0, 1.0, 1.0])


@pytest.mark.asyncio
async def test_latched_entry_persists_without_condition():
    """Entry fires at t=1 only; latch persists through t=2..4."""
    closes = np.array([10.0, 11.0, 10.0, 10.0, 10.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=10.5),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(Block(id="E", input_id="X", weight=50.0, conditions=(entry,)),),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert list(result.positions[0].values) == pytest.approx([0.0, 0.5, 0.5, 0.5, 0.5])


@pytest.mark.asyncio
async def test_exit_clears_only_target_entry():
    """Two entries on same input; exit targets only E1. When exit fires,
    only E1's latch clears; E2 stays latched."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    always = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=0.0),
    )  # True every bar
    exit_at_t3 = CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=13.0),
    )  # fires only at t=3
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E1",
                    name="Entry1",
                    input_id="X",
                    weight=50.0,
                    conditions=(always,),
                ),
                Block(
                    id="E2",
                    name="Entry2",
                    input_id="X",
                    weight=30.0,
                    conditions=(always,),
                ),
            ),
            exits=(
                # Exit targets Entry1 only.
                Block(
                    id="XE1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_at_t3,),
                    target_entry_block_names=("Entry1",),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = list(result.positions[0].values)
    # Both latch at t=0 → 0.8. At t=3 exit clears E1 (0.5 gone),
    # but always-condition re-latches E1 same bar → still 0.8.
    # So we need a condition that is False at t=3 onward for E1 to
    # actually stay cleared. Construct a stricter test below.
    assert vals == pytest.approx([0.8, 0.8, 0.8, 0.8, 0.8])


@pytest.mark.asyncio
async def test_exit_clears_only_target_entry_not_other():
    """E1 condition only at t=1; E2 condition only at t=2. Exit targets
    E1, fires at t=3. Both latches set by t=2; at t=3 only E1 clears
    (E1 condition False → no re-latch). E2 must remain latched."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    e1_cond = CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=11.0),
    )  # t=1 only
    e2_cond = CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=12.0),
    )  # t=2 only
    exit_cond = CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=13.0),
    )  # t=3 only
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E1",
                    name="Entry1",
                    input_id="X",
                    weight=50.0,
                    conditions=(e1_cond,),
                ),
                Block(
                    id="E2",
                    name="Entry2",
                    input_id="X",
                    weight=30.0,
                    conditions=(e2_cond,),
                ),
            ),
            exits=(
                Block(
                    id="XE1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_names=("Entry1",),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = list(result.positions[0].values)
    # t=0: nothing → 0
    # t=1: E1 latches → 0.5
    # t=2: E2 latches → 0.5 + 0.3 = 0.8
    # t=3: exit fires → E1 cleared (E1 cond false); E2 still latched → 0.3
    # t=4: still 0.3
    assert vals == pytest.approx([0.0, 0.5, 0.8, 0.3, 0.3])


@pytest.mark.asyncio
async def test_exit_does_not_clear_opposite_side_entry():
    """Two entries on same input: one long (E_L, w=+50), one short
    (E_S, w=-50). An exit targeting E_L fires; E_S must stay latched.

    This is the v4 analogue of the v3 'cross-side' guardrail — per-
    target-entry clearing makes the invariant trivial: unrelated entries
    are untouched regardless of their sign."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    always = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=0.0),
    )
    # Exit at t>=12 is True and stays True so clearing is persistent,
    # but we disable E_L's re-latch by making its condition fire only
    # at t=0.
    e_l_cond = CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=10.0),
    )
    exit_cond = CompareCondition(
        op="ge",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=12.0),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="EL",
                    name="EntryLong",
                    input_id="X",
                    weight=50.0,
                    conditions=(e_l_cond,),
                ),
                Block(
                    id="ES",
                    name="EntryShort",
                    input_id="X",
                    weight=-50.0,
                    conditions=(always,),
                ),
            ),
            exits=(
                Block(
                    id="XL",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_names=("EntryLong",),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = list(result.positions[0].values)
    # t=0: EL latches (0.5), ES latches (-0.5) → 0.0
    # t=1: same → 0.0
    # t=2: exit fires → EL cleared; ES stays → -0.5
    # t=3,4: exit keeps firing; ES stays → -0.5
    assert vals == pytest.approx([0.0, 0.0, -0.5, -0.5, -0.5])


@pytest.mark.asyncio
async def test_indicator_operand_binds_through_input():
    """Rebinding an indicator operand's input_id swaps the indicator's
    underlying instrument."""
    spx = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    ndx = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
    fetcher = _make_fetcher(
        {("INDEX", "SPX"): (DATES, spx), ("INDEX", "NDX"): (DATES, ndx)}
    )
    sma_code = (
        "def compute(series, window: int = 2):\n"
        "    s = series['price']\n"
        "    out = np.full_like(s, np.nan, dtype=float)\n"
        "    w = int(window)\n"
        "    if w <= len(s):\n"
        "        out[w-1:] = np.convolve(s, np.ones(w)/w, mode='valid')\n"
        "    return out\n"
    )
    ind_spec = IndicatorSpecInput(
        code=sma_code,
        params={"window": 2},
        series_labels=("price",),
        series_map={"price": ("INDEX", "PLACEHOLDER")},
    )

    def _sig(bind_input: str) -> Signal:
        return Signal(
            id="s",
            name="s",
            inputs=(INPUT_X, INPUT_Y),
            rules=SignalRules(
                entries=(
                    Block(
                        id="E",
                        input_id="X",
                        weight=100.0,
                        conditions=(
                            CompareCondition(
                                op="gt",
                                lhs=InstrumentOperand(input_id="X"),
                                rhs=IndicatorOperand(
                                    indicator_id="sma",
                                    input_id=bind_input,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )

    r_x = await evaluate_signal(
        _sig("X"), indicators={"sma": ind_spec}, fetcher=fetcher
    )
    r_y = await evaluate_signal(
        _sig("Y"), indicators={"sma": ind_spec}, fetcher=fetcher
    )
    assert r_x.positions[0].values.sum() > 0
    assert r_y.positions[0].values.sum() == 0.0


@pytest.mark.asyncio
async def test_indicator_unknown_input_errors():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    ind_spec = IndicatorSpecInput(
        code="def compute(series):\n    return series['price']\n",
        params={},
        series_labels=("price",),
        series_map={"price": ("INDEX", "SPX")},
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E",
                    input_id="X",
                    weight=100.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=IndicatorOperand(
                                indicator_id="sma",
                                input_id="Q",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(SignalValidationError, match="Q"):
        await evaluate_signal(signal, indicators={"sma": ind_spec}, fetcher=fetcher)


@pytest.mark.asyncio
async def test_duplicate_input_ids_rejected():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X, INPUT_X),
        rules=SignalRules(),
    )
    with pytest.raises(SignalValidationError, match="duplicate"):
        await evaluate_signal(signal, indicators={}, fetcher=fetcher)


@pytest.mark.asyncio
async def test_exit_with_dangling_target_is_noop():
    """A usable exit whose target_entry_block_names do not match any
    usable entry name is silently skipped (the API validation layer
    rejects such payloads before they reach the engine; the engine
    tolerates latent bad state gracefully)."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=0.0),
    )
    exit_c = CompareCondition(
        op="ge",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=12.0),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E", name="Entry", input_id="X", weight=50.0, conditions=(entry,)
                ),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_c,),
                    target_entry_block_names=("DOES_NOT_EXIST",),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Exit never fires (no usable target) → entry latches and stays.
    assert list(result.positions[0].values) == pytest.approx([0.5, 0.5, 0.5, 0.5, 0.5])


# ---------------------------------------------------------------------------
# S1 — an ENABLED exit whose targets ALL dangle is a no-op (dropped from
# exit_blocks), so its operands must NOT be walked/fetched. Before the fix
# the operand walk gated only on ``block.enabled`` and would fetch (and
# could raise) for such a no-op exit.
# ---------------------------------------------------------------------------


_BROKEN_INPUT = Input(
    id="B",
    instrument=InstrumentSpot(collection="INDEX", instrument_id="MISSING"),
)


@pytest.mark.asyncio
async def test_enabled_all_dangling_exit_does_not_fetch_operands():
    """An enabled exit with ALL-dangling targets is dropped (no-op). Its
    condition references a BROKEN input the fetcher cannot serve; the run
    must still complete because the no-op exit's operands are NOT walked.

    Negative control below flips the target to a real entry name → the
    exit becomes usable → the broken operand IS walked → the run raises,
    proving this test is not vacuously passing.
    """
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    # Fetcher knows SPX only — the broken input (MISSING) raises.
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    entry = Block(
        id="E", name="Entry", input_id="X", weight=50.0, conditions=(_gt(0.0),)
    )
    # Exit condition references the BROKEN input; ALL targets dangle.
    dangling_exit = Block(
        id="X1",
        weight=0.0,
        conditions=(
            CompareCondition(
                op="gt",
                lhs=InstrumentOperand(input_id="B"),  # broken input
                rhs=ConstantOperand(value=0.0),
            ),
        ),
        target_entry_block_names=("DOES_NOT_EXIST",),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X, _BROKEN_INPUT),
        rules=SignalRules(entries=(entry,), exits=(dangling_exit,)),
    )

    # Must NOT raise — the no-op exit is skipped at the walk.
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Entry behaves exactly as if the exit were absent (it latches t0+).
    without = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,)),
    )
    expected = await evaluate_signal(without, indicators={}, fetcher=fetcher)
    assert list(result.positions[0].values) == list(expected.positions[0].values)
    assert result.trades == expected.trades


@pytest.mark.asyncio
async def test_enabled_dangling_exit_negative_control_raises_when_usable():
    """Negative control for the test above: the SAME broken-operand exit
    now targets a REAL entry name → it is usable → its operand IS walked
    → the run MUST raise SignalDataError on the broken fetch.
    """
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    entry = Block(
        id="E", name="Entry", input_id="X", weight=50.0, conditions=(_gt(0.0),)
    )
    usable_broken_exit = Block(
        id="X1",
        weight=0.0,
        conditions=(
            CompareCondition(
                op="gt",
                lhs=InstrumentOperand(input_id="B"),  # broken input
                rhs=ConstantOperand(value=0.0),
            ),
        ),
        target_entry_block_names=("Entry",),  # resolves → exit is usable
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X, _BROKEN_INPUT),
        rules=SignalRules(entries=(entry,), exits=(usable_broken_exit,)),
    )
    with pytest.raises(SignalDataError):
        await evaluate_signal(signal, indicators={}, fetcher=fetcher)


@pytest.mark.asyncio
async def test_multi_target_exit_partial_dangling_closes_only_resolvable():
    """R5 gap: a multi-target exit where SOME targets resolve and SOME
    dangle closes ONLY the resolvable targets, with no error.

    EntryA (w=100, X==10 → opens t0) and EntryB (w=50, X==11 → opens t1).
    The exit targets ["EntryA", "GHOST"] (GHOST dangles) and fires at
    X==13 (t3). Only EntryA's latch clears; EntryB keeps holding.
    Position on X: t0 1.0, t1 1.5, t2 1.5, t3 0.5 (A cleared, B holds),
    t4 0.5.
    """
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    def _eq(v: float) -> CompareCondition:
        return CompareCondition(
            op="eq",
            lhs=InstrumentOperand(input_id="X"),
            rhs=ConstantOperand(value=v),
        )

    entry_a = Block(
        id="EA", name="EntryA", input_id="X", weight=100.0, conditions=(_eq(10.0),)
    )
    entry_b = Block(
        id="EB", name="EntryB", input_id="X", weight=50.0, conditions=(_eq(11.0),)
    )
    # Mixed targets: EntryA resolves, GHOST dangles. (Constructed at the
    # engine layer directly — the API rejects dangling names before the
    # engine, but the engine must tolerate latent bad state.)
    partial_exit = Block(
        id="X1",
        weight=0.0,
        conditions=(_eq(13.0),),
        target_entry_block_names=("EntryA", "GHOST"),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry_a, entry_b), exits=(partial_exit,)),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = list(result.positions[0].values)
    assert vals == pytest.approx([1.0, 1.5, 1.5, 0.5, 0.5])

    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    # Exit effectively cleared EntryA at t3.
    assert events_by[("X1", "exit")].latched_indices == (3,)
    # Only the resolvable target is emitted on the exit event.
    assert events_by[("X1", "exit")].target_entry_block_names == ("EntryA",)
    # EntryA closed at t3; EntryB has no close (open trade).
    trades_by_entry = {tr.entry_block_id: tr for tr in result.trades}
    assert trades_by_entry["EA"].close_bar == 3
    assert trades_by_entry["EB"].close_bar is None


@pytest.mark.asyncio
async def test_events_schema_entry_and_exit():
    """Verify event records carry id, kind, fired/latched/active and
    target_entry_block_names per the v4 trace schema."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    e_cond = CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=11.0),
    )  # fires only t=1
    exit_cond = CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=13.0),
    )  # fires only t=3
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E",
                    name="Entry",
                    input_id="X",
                    weight=100.0,
                    conditions=(e_cond,),
                ),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_names=("Entry",),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    e = events_by[("E", "entry")]
    assert e.fired_indices == (1,)
    assert e.latched_indices == (1,)
    # Active at t=1, 2 (latch held until cleared at t=3 by the exit).
    assert e.active_indices == (1, 2)
    assert e.target_entry_block_names == ()

    x = events_by[("X1", "exit")]
    assert x.fired_indices == (3,)
    # Effective exit: t=3, since E was open.
    assert x.latched_indices == (3,)
    assert x.active_indices == ()
    assert x.target_entry_block_names == ("Entry",)


@pytest.mark.asyncio
async def test_exit_firing_on_closed_entry_is_not_effective():
    """Exit fires but target entry was never opened → latched_indices
    stays empty (the exit was a no-op in practice)."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    never = CompareCondition(
        op="lt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=0.0),
    )
    exit_cond = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=0.0),
    )  # fires every bar
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E",
                    name="Entry",
                    input_id="X",
                    weight=100.0,
                    conditions=(never,),
                ),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_names=("Entry",),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    x = events_by[("X1", "exit")]
    assert len(x.fired_indices) == 5
    assert x.latched_indices == ()  # never actually closed anything


# ---------------------------------------------------------------------------
# enabled-flag parity and trades[] derivation
# ---------------------------------------------------------------------------


def _gt(threshold: float):
    return CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=threshold),
    )


def _lt(threshold: float):
    return CompareCondition(
        op="lt",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=threshold),
    )


@pytest.mark.asyncio
async def test_disabled_entry_block_equivalent_to_deletion():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    enabled_block = Block(
        id="e1", name="A", input_id="X", weight=50.0, conditions=(_gt(0.0),)
    )
    disabled_block = Block(
        id="e2",
        name="B",
        input_id="X",
        weight=50.0,
        conditions=(_gt(0.0),),
        enabled=False,
    )

    with_disabled = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(enabled_block, disabled_block)),
    )
    without = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(enabled_block,)),
    )

    r1 = await evaluate_signal(with_disabled, indicators={}, fetcher=fetcher)
    r2 = await evaluate_signal(without, indicators={}, fetcher=fetcher)

    assert list(r1.positions[0].values) == list(r2.positions[0].values)
    assert {(ev.block_id, ev.kind) for ev in r1.events} == {
        (ev.block_id, ev.kind) for ev in r2.events
    }
    assert r1.trades == r2.trades


@pytest.mark.asyncio
async def test_disabled_exit_block_equivalent_to_deletion():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0, conditions=(_gt(0.0),)
    )
    enabled_exit = Block(
        id="X1",
        weight=0.0,
        conditions=(_gt(12.5),),
        target_entry_block_names=("Entry",),
    )
    disabled_exit = Block(
        id="X2",
        weight=0.0,
        conditions=(_gt(10.5),),
        target_entry_block_names=("Entry",),
        enabled=False,
    )

    with_disabled = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(enabled_exit, disabled_exit)),
    )
    without = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(enabled_exit,)),
    )

    r1 = await evaluate_signal(with_disabled, indicators={}, fetcher=fetcher)
    r2 = await evaluate_signal(without, indicators={}, fetcher=fetcher)
    assert list(r1.positions[0].values) == list(r2.positions[0].values)
    assert r1.trades == r2.trades


@pytest.mark.asyncio
async def test_trades_two_round_trips_back_to_back():
    # Entry condition: close > 0 (always true).
    # Exit at t=1 and t=3 (close == 11 or 13).
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0, conditions=(_gt(0.0),)
    )
    exit_b = Block(
        id="X1",
        weight=0.0,
        conditions=(
            CompareCondition(
                op="eq",
                lhs=InstrumentOperand(input_id="X"),
                rhs=ConstantOperand(value=11.0),
            ),
        ),
        target_entry_block_names=("Entry",),
    )
    exit_c = Block(
        id="X2",
        weight=0.0,
        conditions=(
            CompareCondition(
                op="eq",
                lhs=InstrumentOperand(input_id="X"),
                rhs=ConstantOperand(value=13.0),
            ),
        ),
        target_entry_block_names=("Entry",),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_b, exit_c)),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Expected trades:
    #   t=0 open, t=1 close (exit X1), then re-entry same bar t=1 (intrabar exit→entry),
    #   close at t=3 (exit X2), then re-entry same bar t=3, still open at end.
    assert len(result.trades) == 3
    t0, t1, t2 = result.trades
    assert (t0.open_bar, t0.close_bar, t0.exit_block_id) == (0, 1, "X1")
    assert (t1.open_bar, t1.close_bar, t1.exit_block_id) == (1, 3, "X2")
    assert (t2.open_bar, t2.close_bar, t2.exit_block_id) == (3, None, None)
    for tr in result.trades:
        assert tr.direction == "long"
        assert tr.signed_weight == pytest.approx(1.0)
        assert tr.entry_block_id == "E"
        assert tr.entry_block_name == "Entry"
        assert tr.input_id == "X"
    assert t0.exit_block_name == "" and t1.exit_block_name == ""


@pytest.mark.asyncio
async def test_trades_open_at_end():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    entry = Block(
        id="E",
        name="Entry",
        input_id="X",
        weight=-25.0,
        conditions=(_gt(11.5),),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,)),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.trades) == 1
    tr = result.trades[0]
    assert tr.open_bar == 2
    assert tr.close_bar is None
    assert tr.exit_block_id is None
    assert tr.exit_block_name is None
    assert tr.direction == "short"
    assert tr.signed_weight == pytest.approx(-0.25)


@pytest.mark.asyncio
async def test_trades_disabled_block_yields_no_trades():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    entry = Block(
        id="E",
        name="Entry",
        input_id="X",
        weight=100.0,
        conditions=(_gt(0.0),),
        enabled=False,
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,)),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert result.trades == ()


@pytest.mark.asyncio
async def test_trades_same_bar_entry_then_exit_then_reentry():
    """Engine intrabar order = exit first, then entry. So an exit at the
    same bar as an open creates one closed trade and an immediate
    re-entry on the same bar."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    entry = Block(
        id="E",
        name="Entry",
        input_id="X",
        weight=100.0,
        conditions=(_gt(0.0),),  # fires every bar
    )
    exit_b = Block(
        id="X1",
        weight=0.0,
        conditions=(_gt(0.0),),  # fires every bar
        target_entry_block_names=("Entry",),
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_b,)),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Bar 0: entry opens (no prior latch to clear). Bars 1..4: exit clears
    # at start, entry re-opens. So trades: (open=0, close=1), (1,2), (2,3),
    # (3,4), then open at 4 with no close.
    assert len(result.trades) == 5
    opens = [t.open_bar for t in result.trades]
    closes_arr = [t.close_bar for t in result.trades]
    assert opens == [0, 1, 2, 3, 4]
    assert closes_arr == [1, 2, 3, 4, None]


# ---------------------------------------------------------------------------
# Regression: disabled blocks must not contribute references in _walk_operands.
# A disabled block referencing a broken indicator/input must be skipped at
# the walk so the run still succeeds.
# ---------------------------------------------------------------------------


INPUT_BROKEN = Input(
    id="B",
    instrument=InstrumentSpot(collection="INDEX", instrument_id="MISSING"),
)


@pytest.mark.asyncio
async def test_disabled_block_does_not_trigger_operand_fetch():
    """If a disabled block references a broken input, the run must still
    complete — the disabled block's operands are NOT walked, so the
    fetcher is never asked for the missing instrument.

    Negative control below proves the test isn't vacuously passing.
    """
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    # Fetcher knows about SPX only — MISSING raises SignalDataError.
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    enabled_block = Block(
        id="A",
        name="A",
        input_id="X",
        weight=50.0,
        conditions=(_gt(0.0),),  # references INPUT_X (SPX) only
    )
    disabled_broken_block = Block(
        id="B",
        name="B",
        input_id="B",
        weight=50.0,
        conditions=(
            CompareCondition(
                op="gt",
                lhs=InstrumentOperand(input_id="B"),  # broken input
                rhs=ConstantOperand(value=0.0),
            ),
        ),
        enabled=False,
    )

    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X, INPUT_BROKEN),
        rules=SignalRules(entries=(enabled_block, disabled_broken_block)),
    )

    # Must NOT raise — disabled block is skipped at the walk.
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Only block A's behavior is reflected.
    enabled_only = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(entries=(enabled_block,)),
    )
    expected = await evaluate_signal(enabled_only, indicators={}, fetcher=fetcher)
    assert result.trades == expected.trades
    spx_pos = [p for p in result.positions if p.input_id == "X"][0]
    exp_pos = [p for p in expected.positions if p.input_id == "X"][0]
    assert list(spx_pos.values) == list(exp_pos.values)


@pytest.mark.asyncio
async def test_disabled_block_negative_control_broken_when_enabled():
    """Negative control: flip which block is disabled. Now the broken
    block is enabled — the run MUST raise SignalDataError, proving the
    test above isn't vacuously passing on an unreachable fetch path.
    """
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    disabled_ok_block = Block(
        id="A",
        name="A",
        input_id="X",
        weight=50.0,
        conditions=(_gt(0.0),),
        enabled=False,  # flipped vs above
    )
    enabled_broken_block = Block(
        id="B",
        name="B",
        input_id="B",
        weight=50.0,
        conditions=(
            CompareCondition(
                op="gt",
                lhs=InstrumentOperand(input_id="B"),
                rhs=ConstantOperand(value=0.0),
            ),
        ),
        enabled=True,  # flipped vs above
    )

    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X, INPUT_BROKEN),
        rules=SignalRules(entries=(disabled_ok_block, enabled_broken_block)),
    )

    with pytest.raises(SignalDataError):
        await evaluate_signal(signal, indicators={}, fetcher=fetcher)


# ---------------------------------------------------------------------------
# F1 — multi-target exits (one exit closes MULTIPLE entries)
# ---------------------------------------------------------------------------


def _eq_x(value: float) -> CompareCondition:
    return CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="X"),
        rhs=ConstantOperand(value=value),
    )


def _eq_y(value: float) -> CompareCondition:
    return CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id="Y"),
        rhs=ConstantOperand(value=value),
    )


@pytest.mark.asyncio
async def test_multi_target_exit_closes_two_entries_same_input():
    """One exit targeting TWO entries on the SAME input clears both
    latches at the firing bar, and the engine emits two Trades that
    share the exit's block id.

    SPX closes ``[10, 11, 12, 13, 14]``:
      * E1 (w=+50) fires only at t=0  → latched 0.5 from t=0;
      * E2 (w=+30) fires only at t=1  → latched +0.3 → 0.8 from t=1;
      * exit (X==13) fires only at t=3, targets BOTH E1 and E2 → both
        latches clear at t=3 → position drops 0.8 → 0.0.
    Neither entry re-fires after t=3 (conditions false), so the clear
    is persistent.
    """
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E1",
                    name="E1",
                    input_id="X",
                    weight=50.0,
                    conditions=(_eq_x(10.0),),
                ),
                Block(
                    id="E2",
                    name="E2",
                    input_id="X",
                    weight=30.0,
                    conditions=(_eq_x(11.0),),
                ),
            ),
            exits=(
                Block(
                    id="XBOTH",
                    weight=0.0,
                    conditions=(_eq_x(13.0),),
                    target_entry_block_names=("E1", "E2"),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = list(result.positions[0].values)
    # t0: E1 → 0.5; t1: E2 → 0.8; t2: 0.8; t3: both cleared → 0.0; t4: 0.0
    assert vals == pytest.approx([0.5, 0.8, 0.8, 0.0, 0.0])

    # Exactly two closed trades, both stamped with the shared exit id and
    # the same close bar (the single firing closes both entries at t=3).
    assert len(result.trades) == 2
    by_entry = {tr.entry_block_id: tr for tr in result.trades}
    assert set(by_entry) == {"E1", "E2"}
    assert by_entry["E1"].open_bar == 0
    assert by_entry["E2"].open_bar == 1
    for tr in result.trades:
        assert tr.close_bar == 3
        assert tr.exit_block_id == "XBOTH"
        assert tr.direction == "long"

    # The exit's "effective" bar (cleared ≥1 latch) is recorded once.
    ev = {(e.block_id, e.kind): e for e in result.events}
    x = ev[("XBOTH", "exit")]
    assert x.fired_indices == (3,)
    assert x.latched_indices == (3,)
    assert set(x.target_entry_block_names) == {"E1", "E2"}


@pytest.mark.asyncio
async def test_multi_target_exit_closes_two_entries_cross_input():
    """One exit targeting two entries on DIFFERENT inputs steps BOTH
    inputs' positions down at the firing bar (cross-input is allowed).

    SPX closes ``[10, 11, 12, 13, 14]``, NDX closes ``[20, 19, 18, 17, 16]``:
      * EntryX (input X, w=+50) fires only at t=0 → X-position 0.5;
      * EntryY (input Y, w=-40) fires only at t=1 → Y-position -0.4;
      * exit condition on X (X==13) fires only at t=3 and targets BOTH
        EntryX and EntryY → both inputs clear at t=3.
    """
    spx = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    ndx = np.array([20.0, 19.0, 18.0, 17.0, 16.0])
    fetcher = _make_fetcher(
        {("INDEX", "SPX"): (DATES, spx), ("INDEX", "NDX"): (DATES, ndx)}
    )
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X, INPUT_Y),
        rules=SignalRules(
            entries=(
                Block(
                    id="EX",
                    name="EntryX",
                    input_id="X",
                    weight=50.0,
                    conditions=(_eq_x(10.0),),
                ),
                Block(
                    id="EY",
                    name="EntryY",
                    input_id="Y",
                    weight=-40.0,
                    conditions=(_eq_y(19.0),),
                ),
            ),
            exits=(
                Block(
                    id="XALL",
                    weight=0.0,
                    conditions=(_eq_x(13.0),),  # exit's condition reads input X
                    target_entry_block_names=("EntryX", "EntryY"),
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    pos = {p.input_id: list(p.values) for p in result.positions}
    # X: 0.5 from t=0, cleared at t=3.
    assert pos["X"] == pytest.approx([0.5, 0.5, 0.5, 0.0, 0.0])
    # Y: -0.4 from t=1, cleared at t=3 (closed by the X-driven exit).
    assert pos["Y"] == pytest.approx([0.0, -0.4, -0.4, 0.0, 0.0])

    # Two closed trades on two inputs, both via the shared exit id.
    assert len(result.trades) == 2
    by_entry = {tr.entry_block_id: tr for tr in result.trades}
    assert by_entry["EX"].input_id == "X"
    assert by_entry["EX"].direction == "long"
    assert by_entry["EY"].input_id == "Y"
    assert by_entry["EY"].direction == "short"
    for tr in result.trades:
        assert tr.close_bar == 3
        assert tr.exit_block_id == "XALL"

    # The exit event references both targets; its ``input_id`` carries the
    # first resolvable target's operating input (declaration order → X).
    ev = {(e.block_id, e.kind): e for e in result.events}
    x = ev[("XALL", "exit")]
    assert x.input_id == "X"
    assert set(x.target_entry_block_names) == {"EntryX", "EntryY"}
