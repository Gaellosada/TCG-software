"""Engine tests for the default-off ``Input.signal_lag_days`` feature (PR #92).

Legacy applies a signal-driven regime with a 1-business-day lag: the regime
RESOLVED from close[D] is the position HELD on day D+1 ("act on yesterday's
signal").  Our engine resolves + holds it same-bar.  ``signal_lag_days`` is an
OPT-IN per-input shift of the resolved NET position series forward by N bars so
the position held on day D is the state resolved from D-N.  Default 0 == the
historical same-bar behaviour (BYTE-IDENTICAL).

  L0  DEFAULT (signal_lag_days=0) — position appears at the SIGNAL bar k
      (control, byte-identical to the no-field path).
  L1  signal_lag_days=1 — the SAME regime is held one bar later (k+1); the
      signal bar k is FLAT.
  L2  series start — a regime already ON at bar 0 is FLAT on bar 0 under lag
      (no D-1 state) and appears at bar 1.
  L3  lag flows into the return / equity path (a lagged short avoids the very
      first adverse step it would otherwise book).
  L4  lag >= series length → entirely FLAT (no out-of-range indexing).
  L5  a NaN/gap bar in the priced input is handled sanely under lag (the
      poison mask still zeroes its own bar; no crash / no leaked exposure).
  L6  default-off byte-identity: lag=0 positions, realized_pnl AND equity are
      element-for-element identical to the no-field signal.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

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


def _make_fetcher(prices: np.ndarray, dates: np.ndarray) -> Callable:
    async def fetch(instrument, field):
        return dates, np.asarray(prices, dtype=np.float64)

    return fetch


def _close(iid: str = "X") -> InstrumentOperand:
    return InstrumentOperand(input_id=iid, field="close")


def _const(v: float) -> ConstantOperand:
    return ConstantOperand(value=v)


def _input(iid: str = "X", *, signal_lag_days: int = 0, position_cap=None) -> Input:
    return Input(
        id=iid,
        instrument=InstrumentSpot(collection="I", instrument_id=iid),
        signal_lag_days=signal_lag_days,
        position_cap=position_cap,
    )


def _gt(iid: str, thr: float) -> CompareCondition:
    return CompareCondition(op="gt", lhs=_close(iid), rhs=_const(thr))


def _long_signal(*, signal_lag_days: int = 0, weight: float = 100.0) -> Signal:
    """One +weight entry on X, condition ``close > 100``, no exit."""
    return Signal(
        id="s",
        name="s",
        inputs=(_input("X", signal_lag_days=signal_lag_days),),
        rules=SignalRules(
            entries=(
                Block(id="e1", input_id="X", weight=weight, conditions=(_gt("X", 100.0),)),
            )
        ),
    )


async def _positions(signal: Signal, prices, dates):
    return await evaluate_signal(
        signal, {}, _make_fetcher(np.asarray(prices, dtype=np.float64), dates)
    )


# condition True on bars 2,3,4 (price crosses 100 at bar 2)
_PRICES = [90.0, 90.0, 110.0, 111.0, 112.0]
_DATES = np.arange(20240101, 20240101 + len(_PRICES), dtype=np.int64)


@pytest.mark.asyncio
async def test_l0_default_no_lag_position_at_signal_bar():
    res = await _positions(_long_signal(signal_lag_days=0), _PRICES, _DATES)
    assert res.positions[0].values.tolist() == [0.0, 0.0, 1.0, 1.0, 1.0]


@pytest.mark.asyncio
async def test_l1_lag_one_delays_position_one_bar():
    res = await _positions(_long_signal(signal_lag_days=1), _PRICES, _DATES)
    # regime ON at bar 2 → HELD from bar 3; bar 2 (the signal bar) is FLAT.
    assert res.positions[0].values.tolist() == [0.0, 0.0, 0.0, 1.0, 1.0]


@pytest.mark.asyncio
async def test_l2_series_start_is_flat_under_lag():
    prices = [110.0, 111.0, 112.0]  # condition True from bar 0
    dates = np.arange(20240101, 20240101 + 3, dtype=np.int64)
    on = await _positions(_long_signal(signal_lag_days=0), prices, dates)
    lagged = await _positions(_long_signal(signal_lag_days=1), prices, dates)
    assert on.positions[0].values.tolist() == [1.0, 1.0, 1.0]
    # bar 0 has no D-1 state → FLAT; the regime appears at bar 1.
    assert lagged.positions[0].values.tolist() == [0.0, 1.0, 1.0]


@pytest.mark.asyncio
async def test_l3_lag_flows_into_returns_and_equity():
    # A SHORT that turns on at bar 2 while price keeps RISING loses money each
    # held bar.  Lagging it one bar skips the bar2->bar3 adverse step, so the
    # lagged short's final equity is strictly HIGHER (less loss) than same-bar.
    same = await _positions(_long_signal(signal_lag_days=0, weight=-100.0), _PRICES, _DATES)
    lag = await _positions(_long_signal(signal_lag_days=1, weight=-100.0), _PRICES, _DATES)
    assert lag.equity_ratio[-1] > same.equity_ratio[-1]
    # realized_pnl also reflects the shift: the same-bar short books a loss at
    # the bar2->bar3 step (pos[2]=-1 * +return); the lagged short books 0 there.
    assert same.positions[0].realized_pnl[3] < lag.positions[0].realized_pnl[3]


@pytest.mark.asyncio
async def test_l4_lag_ge_length_is_all_flat():
    res = await _positions(_long_signal(signal_lag_days=10), _PRICES, _DATES)
    assert res.positions[0].values.tolist() == [0.0, 0.0, 0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_l5_nan_gap_bar_handled_under_lag():
    # A NaN price on bar 3 poisons that input bar; the lagged position must
    # still resolve without error and must not leak exposure onto the poison bar.
    prices = [90.0, 110.0, 111.0, np.nan, 113.0]
    dates = np.arange(20240101, 20240101 + 5, dtype=np.int64)
    res = await _positions(_long_signal(signal_lag_days=1), prices, dates)
    vals = res.positions[0].values
    assert np.all(np.isfinite(vals))
    # bar 3 is a NaN/gap → zeroed regardless of the lagged regime.
    assert vals[3] == 0.0


@pytest.mark.asyncio
async def test_l6_lag_zero_byte_identical_to_no_field():
    lag0 = await _positions(_long_signal(signal_lag_days=0), _PRICES, _DATES)
    # explicit no-field construction (default): must be identical arrays.
    base_sig = Signal(
        id="s",
        name="s",
        inputs=(Input(id="X", instrument=InstrumentSpot(collection="I", instrument_id="X")),),
        rules=SignalRules(
            entries=(Block(id="e1", input_id="X", weight=100.0, conditions=(_gt("X", 100.0),)),)
        ),
    )
    base = await _positions(base_sig, _PRICES, _DATES)
    np.testing.assert_array_equal(lag0.positions[0].values, base.positions[0].values)
    np.testing.assert_array_equal(lag0.positions[0].realized_pnl, base.positions[0].realized_pnl)
    np.testing.assert_array_equal(lag0.equity_ratio, base.equity_ratio)
