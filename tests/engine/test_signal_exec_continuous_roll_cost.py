"""Slippage/fees on a SIGNAL continuous-futures leg that ROLLS in-window.

The signal engine historically charged transaction costs only via
``establish_turnover`` (entries/exits + daily drift) plus option HOLD-leg
rolls.  A priced *continuous-future* leg's back-adjusted return stream makes the
roll date an ordinary price bar with an UNCHANGED target weight, so
``establish_turnover`` adds ~0 at the roll and the 2-side round-trip on the
rolled notional was never charged -- disagreeing with the PORTFOLIO engine,
which does charge it (``portfolio.py`` §7).

These tests drive ``evaluate_signal`` with an ``InstrumentContinuous`` input and
a fetcher exposing ``fetch_continuous_roll_info`` (the new plumbing hook), and
assert the hand-computed roll drag.
"""

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
    InstrumentContinuous,
    InstrumentOperand,
    Signal,
    SignalRules,
)

# 4 bars, constant +10%/step continuous-future price.
_DATES = np.array([20240101, 20240102, 20240103, 20240104], dtype=np.int64)
_PRICES = np.array([100.0, 110.0, 121.0, 133.1], dtype=np.float64)
# One INTERIOR roll boundary, on bar index 2 (20240103).
_ROLL_DATES = np.array([20240103], dtype=np.int64)


def _always_long_continuous_signal() -> Signal:
    return Signal(
        id="s",
        name="s",
        inputs=(
            Input(
                id="X",
                instrument=InstrumentContinuous(collection="FUT_SP_500"),
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


def _fetcher(roll_dates: np.ndarray | None = _ROLL_DATES):
    """Continuous fetcher: prices for every field + a roll-info side-channel."""

    async def fetch(instrument, field):
        return _DATES, _PRICES

    async def fetch_continuous_roll_info(instrument):
        return np.asarray(roll_dates if roll_dates is not None else [], dtype=np.int64)

    fetch.fetch_continuous_roll_info = fetch_continuous_roll_info  # type: ignore[attr-defined]
    return fetch


@pytest.mark.asyncio
async def test_zero_bps_byte_identical():
    """0 bps (and the default no-cost_config path) must be byte-identical -- the
    continuous-roll overlay is fully gated behind a non-zero CostConfig."""
    sig = _always_long_continuous_signal()
    base = await evaluate_signal(sig, {}, _fetcher())
    zero = await evaluate_signal(sig, {}, _fetcher(), CostConfig(0.0, 0.0))
    assert np.array_equal(base.equity_ratio, zero.equity_ratio)
    assert zero.total_slippage_paid_pct == 0.0
    assert zero.total_fees_paid_pct == 0.0
    assert base.total_slippage_paid_pct == 0.0


@pytest.mark.asyncio
async def test_continuous_roll_charges_round_trip_slippage():
    """Hand-computed worked example (slippage_bps=100 -> rate 0.01).

    pos = +1.0 every bar; gross net_step = [0.10, 0.10, 0.10].
    establish_turnover (single leg, no weight change) = [1.0, 0.0, 0.0].
    Interior roll on bar 2 adds ``2*|pos[2]| = 2.0`` -> turnover = [1.0, 0.0, 2.0].
    slip_drag = [0.01, 0.0, 0.02]; net_adj = [0.09, 0.10, 0.08].
    equity_ratio = [1, 1.09, 1.199, 1.199*1.08 = 1.29492].
    total_slippage_pct = 100*(0.01*1.0 + 0.0*1.09 + 0.02*1.199) = 3.398 %.
    """
    sig = _always_long_continuous_signal()
    res = await evaluate_signal(
        sig, {}, _fetcher(), CostConfig(slippage_bps=100.0, fees_bps=0.0)
    )
    np.testing.assert_allclose(res.equity_ratio, [1.0, 1.09, 1.199, 1.29492], atol=1e-9)
    assert abs(res.total_slippage_paid_pct - 3.398) < 1e-9
    assert res.total_fees_paid_pct == 0.0


def _latch_above_115_continuous_signal() -> Signal:
    """Same continuous leg, but the entry only latches once close > 115 -> the
    position FRESHLY establishes on bar 2 (price 121), which is the roll bar."""
    sig = _always_long_continuous_signal()
    block = sig.rules.entries[0]
    new_block = Block(
        id=block.id,
        input_id=block.input_id,
        weight=block.weight,
        conditions=(
            CompareCondition(
                op="gt",
                lhs=InstrumentOperand(input_id="X", field="close"),
                rhs=ConstantOperand(value=115.0),  # false @100,110 ; true @121,133.1
            ),
        ),
    )
    return Signal(
        id=sig.id,
        name=sig.name,
        inputs=sig.inputs,
        rules=SignalRules(entries=(new_block,)),
    )


@pytest.mark.asyncio
async def test_fresh_establish_on_roll_bar_not_double_charged():
    """FIX: a position that first LATCHES exactly on the roll bar is charged the
    single entry side only -- NOT entry + a full round-trip (~3x overcharge).

    pos = [0, 0, 1, 1] (latches on bar 2, the roll bar).
    establish_turnover bills the entry on step 2: |1 - drift(0)| = 1.0.
    The roll overlay adds ``2*min(|pos[1]|,|pos[2]|) = 2*min(0,1) = 0`` (nothing
    held THROUGH the boundary) -> turnover = [0, 0, 1.0].
    slippage_bps=100 (rate 0.01): slip_drag = [0,0,0.01]; only step 2 has a
    return (pos[2]*0.10 = 0.10) -> net_adj[2] = 0.09 ; equity_ratio[-1] = 1.09.
    total_slippage = 100*(0.01 * er_start[2]=1.0) = 1.0 %.
    (Pre-fix the overlay added 2*|pos[2]|=2.0 -> turnover[2]=3.0, cost 3.0%,
    equity 1.07.)
    """
    sig = _latch_above_115_continuous_signal()
    res = await evaluate_signal(
        sig, {}, _fetcher(), CostConfig(slippage_bps=100.0, fees_bps=0.0)
    )
    np.testing.assert_allclose(res.equity_ratio, [1.0, 1.0, 1.0, 1.09], atol=1e-9)
    assert abs(res.total_slippage_paid_pct - 1.0) < 1e-9


@pytest.mark.asyncio
async def test_roll_reduces_equity_and_raises_cost_vs_no_roll():
    """Isolate the roll: the SAME signal with NO interior roll charges strictly
    less slippage and ends with strictly higher equity."""
    sig = _always_long_continuous_signal()
    cfg = CostConfig(10.0, 5.0)
    with_roll = await evaluate_signal(sig, {}, _fetcher(), cfg)
    no_roll = await evaluate_signal(sig, {}, _fetcher(roll_dates=None), cfg)

    assert with_roll.total_slippage_paid_pct > no_roll.total_slippage_paid_pct
    assert with_roll.total_fees_paid_pct > no_roll.total_fees_paid_pct
    assert with_roll.equity_ratio[-1] < no_roll.equity_ratio[-1]
    # 10 bps slippage vs 5 bps fees over identical turnover -> exactly 2:1.
    assert (
        abs(with_roll.total_slippage_paid_pct - 2.0 * with_roll.total_fees_paid_pct)
        < 1e-12
    )
    # vs the 0-bps run: equity drops.
    zero = await evaluate_signal(sig, {}, _fetcher())
    assert with_roll.equity_ratio[-1] < zero.equity_ratio[-1]
