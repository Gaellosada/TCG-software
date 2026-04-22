"""NaN-poisoning warmup-window tests for RollingCondition.

These tests verify that the first ``kk`` positions of ``nan_at_t`` from
a RollingCondition evaluation are marked True (poisoned), so that the
block-latching logic treats them as *unknown* rather than a hard False.
A hard False during warmup would allow an exit condition to claim it has
fired before any data existed, which is incorrect.
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.signal_exec import (
    SignalValidationError,
    evaluate_signal,
)
from tcg.types.signal import (
    Block,
    ConstantOperand,
    Input,
    InstrumentOperand,
    InstrumentSpot,
    RollingCondition,
    Signal,
    SignalRules,
)


DATES = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108, 20240109, 20240110],
    dtype=np.int64,
)


def _make_fetcher(dates, closes):
    async def fetch(instrument, field):
        return dates, closes
    return fetch


INPUT_X = Input(
    id="X",
    instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
)


# ---------------------------------------------------------------------------
# Direct nan_at_t poisoning via evaluate_signal output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rolling_warmup_no_signal_during_warmup_window():
    """With lookback kk=3, no long signal should fire during positions [0..2].

    The RollingCondition marks the warmup window as NaN, so the block
    cannot latch during that period regardless of whether the comparison
    would be True.
    """
    # Make closes strictly increasing so rolling_gt (cur > prev) is always
    # True where data exists — without warmup poisoning, t=3 onward would fire.
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
    kk = 3
    fetcher = _make_fetcher(DATES, closes)

    cond = RollingCondition(op="rolling_gt", operand=InstrumentOperand(input_id="X"), lookback=kk)
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(Block(input_id="X", weight=1.0, conditions=(cond,)),),
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.positions) == 1
    vals = result.positions[0].values

    # Warmup window: positions [0..kk-1] must be 0 (not latched)
    warmup = vals[:kk]
    assert np.all(warmup == 0.0), (
        f"Expected no signal during warmup window [0..{kk-1}], got {warmup}"
    )

    # After warmup: signal may fire (closes are increasing, so rolling_gt fires)
    post_warmup = vals[kk:]
    assert np.any(post_warmup > 0.0), (
        f"Expected signal after warmup, got {post_warmup}"
    )


@pytest.mark.asyncio
async def test_rolling_warmup_kk1_only_first_position_suppressed():
    """With kk=1, only position [0] is suppressed."""
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
    kk = 1
    fetcher = _make_fetcher(DATES, closes)

    cond = RollingCondition(op="rolling_gt", operand=InstrumentOperand(input_id="X"), lookback=kk)
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(Block(input_id="X", weight=1.0, conditions=(cond,)),),
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = result.positions[0].values

    # Position 0 must be 0 (warmup)
    assert vals[0] == 0.0, f"Expected vals[0]=0.0, got {vals[0]}"

    # Positions 1 onward: closes are increasing so rolling_gt fires; block latches
    assert np.all(vals[1:] > 0.0), f"Expected latched signal after kk=1 warmup, got {vals[1:]}"


@pytest.mark.asyncio
async def test_rolling_warmup_does_not_generate_false_negative_signals():
    """Warmup-poisoned positions must not generate false negative (exit) signals.

    If warmup positions were False rather than NaN, a long_exit block
    would see the exit condition as True (exit fires) during warmup,
    which could spuriously clear a latch that was set by another block.
    Here we verify that an exit block using a RollingCondition does NOT
    clear the long position during the warmup window.
    """
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
    kk = 3
    fetcher = _make_fetcher(DATES, closes)

    # Entry: close > constant (always fires once data exists)
    entry_cond = RollingCondition(
        op="rolling_gt",
        operand=InstrumentOperand(input_id="X"),
        lookback=kk,
    )
    # Exit: rolling_lt (never actually fires because closes strictly increase),
    # but with kk warmup poisoning the exit block cannot fire during warmup.
    exit_cond = RollingCondition(
        op="rolling_lt",
        operand=InstrumentOperand(input_id="X"),
        lookback=kk,
    )

    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(Block(input_id="X", weight=1.0, conditions=(entry_cond,)),),
            long_exit=(Block(input_id="X", weight=0.0, conditions=(exit_cond,)),),
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = result.positions[0].values

    # Warmup positions must be 0 (entry block hasn't latched yet)
    for i in range(kk):
        assert vals[i] == 0.0, f"Position {i} should be 0 during warmup, got {vals[i]}"

    # After warmup: entry fires and latches; exit never fires (closes increasing).
    # Key assertion: once latched, it should stay latched (no spurious exit clearing).
    post_warmup = vals[kk:]
    assert np.all(post_warmup == 1.0), (
        f"Expected latched position after warmup, got {post_warmup}"
    )


@pytest.mark.asyncio
async def test_rolling_warmup_full_window_larger_than_data():
    """If kk >= T, all positions are in the warmup window — no signal fires."""
    closes = np.array([10.0, 11.0, 12.0])
    kk = 5  # larger than len(closes)=3
    fetcher = _make_fetcher(DATES[:3], closes)

    cond = RollingCondition(op="rolling_gt", operand=InstrumentOperand(input_id="X"), lookback=kk)
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(Block(input_id="X", weight=1.0, conditions=(cond,)),),
        ),
    )

    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    vals = result.positions[0].values

    assert np.all(vals == 0.0), f"Expected all-zero with kk > T, got {vals}"


@pytest.mark.asyncio
async def test_rolling_invalid_lookback_zero_raises():
    """lookback < 1 must raise SignalValidationError."""
    closes = np.array([10.0, 11.0, 12.0])
    fetcher = _make_fetcher(DATES[:3], closes)

    cond = RollingCondition(op="rolling_gt", operand=InstrumentOperand(input_id="X"), lookback=0)
    signal = Signal(
        id="s",
        name="s",
        inputs=(INPUT_X,),
        rules=SignalRules(
            long_entry=(Block(input_id="X", weight=1.0, conditions=(cond,)),),
        ),
    )

    with pytest.raises(SignalValidationError, match="rolling lookback"):
        await evaluate_signal(signal, indicators={}, fetcher=fetcher)
