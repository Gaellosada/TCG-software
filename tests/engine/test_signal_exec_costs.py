"""Functional tests for slippage/fees in the signal engine (signal_exec)."""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.costs import CostConfig
from tcg.engine.signal_exec import evaluate_signal
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    Input,
    InstrumentOperand,
    InstrumentSpot,
    Signal,
    SignalRules,
)

_DATES = np.array([20240101, 20240102, 20240103], dtype=np.int64)
_PRICES = np.array([100.0, 110.0, 121.0])  # constant +10% each step


def _always_long_signal() -> Signal:
    return Signal(
        id="s",
        name="s",
        inputs=(
            Input(
                id="X",
                instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
            ),
        ),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="X",
                    weight=100.0,  # signed position = +1.0
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X", field="close"),
                            rhs=ConstantOperand(value=1.0),  # always true
                        ),
                    ),
                ),
            )
        ),
    )


def _fetcher():
    async def fetch(instrument, field):
        return _DATES, _PRICES

    return fetch


@pytest.mark.asyncio
async def test_zero_bps_byte_identical():
    sig = _always_long_signal()
    base = await evaluate_signal(sig, {}, _fetcher())
    zero = await evaluate_signal(sig, {}, _fetcher(), CostConfig(0.0, 0.0))
    assert np.array_equal(base.equity_ratio, zero.equity_ratio)
    assert zero.total_slippage_paid_pct == 0.0
    assert zero.total_fees_paid_pct == 0.0
    assert base.total_slippage_paid_pct == 0.0  # default (no cost_config)


@pytest.mark.asyncio
async def test_signal_worked_example_slippage_only():
    """Always-long single leg (pos=1.0), prices +10%/step, slippage_bps=100.

    net_step gross = [0.10, 0.10]; single-leg turnover = [1.0, 0.0] (only entry).
    slip_drag = [0.01, 0.0]; net_step_adj = [0.09, 0.10].
    equity_ratio = [1.0, 1.09, 1.09*1.10 = 1.199].
    total_slippage_pct = 100 * (0.01 * er[0]=1.0) = 1.0 %.
    """
    sig = _always_long_signal()
    res = await evaluate_signal(
        sig, {}, _fetcher(), CostConfig(slippage_bps=100.0, fees_bps=0.0)
    )
    np.testing.assert_allclose(res.equity_ratio, [1.0, 1.09, 1.199], atol=1e-9)
    assert abs(res.total_slippage_paid_pct - 1.0) < 1e-9
    assert res.total_fees_paid_pct == 0.0

    base = await evaluate_signal(sig, {}, _fetcher())
    assert res.equity_ratio[-1] < base.equity_ratio[-1]


@pytest.mark.asyncio
async def test_signal_slippage_and_fees_isolate():
    sig = _always_long_signal()
    slip = await evaluate_signal(sig, {}, _fetcher(), CostConfig(100.0, 0.0))
    fees = await evaluate_signal(sig, {}, _fetcher(), CostConfig(0.0, 100.0))
    assert abs(slip.total_slippage_paid_pct - fees.total_fees_paid_pct) < 1e-12
    assert slip.total_fees_paid_pct == 0.0
    assert fees.total_slippage_paid_pct == 0.0
