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


DATES = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64
)


def _make_fetcher(
    by_key: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]
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
    assert list(by_id["Y"].values) == pytest.approx(
        [0.0, -0.4, -0.4, -0.4, -0.4]
    )


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
                Block(id="E", name="Entry", input_id="X", weight=100.0, conditions=(entry,)),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_c,),
                    target_entry_block_name="Entry",
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
            entries=(
                Block(id="E", input_id="X", weight=50.0, conditions=(entry,)),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert list(result.positions[0].values) == pytest.approx(
        [0.0, 0.5, 0.5, 0.5, 0.5]
    )


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
                Block(id="E1", name="Entry1", input_id="X", weight=50.0, conditions=(always,)),
                Block(id="E2", name="Entry2", input_id="X", weight=30.0, conditions=(always,)),
            ),
            exits=(
                # Exit targets Entry1 only.
                Block(
                    id="XE1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_at_t3,),
                    target_entry_block_name="Entry1",
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
                Block(id="E1", name="Entry1", input_id="X", weight=50.0, conditions=(e1_cond,)),
                Block(id="E2", name="Entry2", input_id="X", weight=30.0, conditions=(e2_cond,)),
            ),
            exits=(
                Block(
                    id="XE1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_name="Entry1",
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
                Block(id="EL", name="EntryLong", input_id="X", weight=50.0, conditions=(e_l_cond,)),
                Block(id="ES", name="EntryShort", input_id="X", weight=-50.0, conditions=(always,)),
            ),
            exits=(
                Block(
                    id="XL",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_name="EntryLong",
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

    r_x = await evaluate_signal(_sig("X"), indicators={"sma": ind_spec}, fetcher=fetcher)
    r_y = await evaluate_signal(_sig("Y"), indicators={"sma": ind_spec}, fetcher=fetcher)
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
    """A usable exit whose target_entry_block_name does not match any
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
                Block(id="E", name="Entry", input_id="X", weight=50.0, conditions=(entry,)),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_c,),
                    target_entry_block_name="DOES_NOT_EXIST",
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Exit never fires (no usable target) → entry latches and stays.
    assert list(result.positions[0].values) == pytest.approx(
        [0.5, 0.5, 0.5, 0.5, 0.5]
    )


@pytest.mark.asyncio
async def test_events_schema_entry_and_exit():
    """Verify event records carry id, kind, fired/latched/active and
    target_entry_block_name per the v4 trace schema."""
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
                Block(id="E", name="Entry", input_id="X", weight=100.0, conditions=(e_cond,)),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_name="Entry",
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
    assert e.target_entry_block_name is None

    x = events_by[("X1", "exit")]
    assert x.fired_indices == (3,)
    # Effective exit: t=3, since E was open.
    assert x.latched_indices == (3,)
    assert x.active_indices == ()
    assert x.target_entry_block_name == "Entry"


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
                Block(id="E", name="Entry", input_id="X", weight=100.0, conditions=(never,)),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(exit_cond,),
                    target_entry_block_name="Entry",
                ),
            ),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    x = events_by[("X1", "exit")]
    assert len(x.fired_indices) == 5
    assert x.latched_indices == ()  # never actually closed anything
