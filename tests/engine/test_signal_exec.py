"""Engine tests for signal_exec v3 (iter-4, named inputs).

Exercises input-based composition, operand → input resolution, indicator
operand input_id binding, multi-input evaluation, clipping and
exit-kills.
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
async def test_single_input_compare():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(
                Block(
                    input_id="X",
                    weight=1.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X", field="close"),
                            rhs=ConstantOperand(value=11.5),
                        ),
                    ),
                ),
            )
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.positions) == 1
    p = result.positions[0]
    assert p.input_id == "X"
    assert list(p.values) == [0.0, 0.0, 1.0, 1.0, 1.0]
    assert not result.clipped


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
            long_entry=(
                Block(
                    input_id="X",
                    weight=0.6,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=ConstantOperand(value=11.5),
                        ),
                    ),
                ),
                Block(
                    input_id="Y",
                    weight=0.4,
                    conditions=(
                        CompareCondition(
                            op="lt",
                            lhs=InstrumentOperand(input_id="Y"),
                            rhs=ConstantOperand(value=99.5),
                        ),
                    ),
                ),
            )
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.positions) == 2
    by_id = {p.input_id: p for p in result.positions}
    assert list(by_id["X"].values) == [0.0, 0.0, 0.6, 0.6, 0.6]
    assert list(by_id["Y"].values) == [0.0, 0.4, 0.4, 0.4, 0.4]


@pytest.mark.asyncio
async def test_unusable_block_without_input_id_skipped():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(
                Block(
                    input_id="",
                    weight=1.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            )
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert result.positions == ()


@pytest.mark.asyncio
async def test_entry_weight_zero_skipped():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(
                Block(
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
            )
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert result.positions == ()


@pytest.mark.asyncio
async def test_clipping_on_weight_gt_one():
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
            long_entry=(
                Block(input_id="X", weight=0.8, conditions=(cond,)),
                Block(input_id="X", weight=0.8, conditions=(cond,)),
            )
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert result.clipped is True
    assert list(result.positions[0].values) == [1.0] * 5
    assert all(result.positions[0].clipped_mask)


@pytest.mark.asyncio
async def test_exit_kills_position():
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
            long_entry=(Block(input_id="X", weight=1.0, conditions=(entry,)),),
            long_exit=(Block(input_id="X", weight=0.0, conditions=(exit_c,)),),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = list(result.positions[0].values)
    assert vals == [1.0, 1.0, 1.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_indicator_operand_binds_through_input():
    """Rebinding an indicator operand's input_id swaps the indicator's
    underlying instrument — the core v3 invariant."""
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
        series_map={"price": ("INDEX", "PLACEHOLDER")},  # primary: overridden by input_id
    )

    def _sig(bind_input: str) -> Signal:
        return Signal(
            id="s",
            name="s",
            inputs=(INPUT_X, INPUT_Y),
            rules=SignalRules(
                long_entry=(
                    Block(
                        input_id="X",
                        weight=1.0,
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
                )
            ),
        )

    r_x = await evaluate_signal(_sig("X"), indicators={"sma": ind_spec}, fetcher=fetcher)
    r_y = await evaluate_signal(_sig("Y"), indicators={"sma": ind_spec}, fetcher=fetcher)
    # SPX vs SMA(SPX): fires from t=1 onward.
    assert r_x.positions[0].values.sum() > 0
    # SPX vs SMA(NDX=100): SPX in [10..14] < 100 → never fires.
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
            long_entry=(
                Block(
                    input_id="X",
                    weight=1.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X"),
                            rhs=IndicatorOperand(
                                indicator_id="sma",
                                input_id="Q",  # unknown input
                            ),
                        ),
                    ),
                ),
            )
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
        inputs=(INPUT_X, INPUT_X),  # duplicate id
        rules=SignalRules(),
    )
    with pytest.raises(SignalValidationError, match="duplicate"):
        await evaluate_signal(signal, indicators={}, fetcher=fetcher)
