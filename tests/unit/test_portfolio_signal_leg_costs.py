"""Round-6 review MAJOR: a signal leg inside a portfolio must pay its OWN
internal transaction costs.

``tcg.core.api.portfolio._evaluate_signal_leg`` used to call
``evaluate_signal(signal, indicators, fetcher)`` with NO ``cost_config``, so a
signal leg's internal entries/exits/rolls were never charged and its synthetic
equity ``100·(1+aggregated_pnl)`` was GROSS of costs.  The standalone Signals
path passes ``cost_config``, and composed sub-portfolios recursively receive
costs — so wrapping a strategy as a portfolio signal leg made its costs vanish.

These tests run the REAL ``_evaluate_signal_leg`` (and the REAL
``evaluate_signal``) with only the DB-boundary shims stubbed, and pin:

* at 10 bps the leg's synthetic equity reflects the internal cost drag and
  equals the SAME signal computed standalone at 10 bps (the composed-leg
  semantics: internal cost shows in the leg's synthetic → combined equity);
* at 0 bps the leg synthetic is byte-identical to the pre-cost behaviour
  (``cost_config`` early-skips → the golden-master gate holds).
"""

from __future__ import annotations

from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from tcg.core.api.portfolio import _evaluate_signal_leg
from tcg.engine.costs import CostConfig
from tcg.engine.signal_exec import SignalDataError, evaluate_signal
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

# Async tests auto-marked (asyncio_mode="auto").

DATES = np.array([20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64)
INPUT_X = Input(
    id="X", instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX")
)


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


def _reentry_signal() -> Signal:
    """Enter <160, exit >=200 → enter, ride up, exit, re-enter (multiple trades).

    The re-entry path generates several entry/exit turnover events so the
    internal transaction cost is materially larger than a single initial entry.
    """
    return Signal(
        id="s_cost",
        name="reentry",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E",
                    name="Entry",
                    input_id="X",
                    weight=100.0,
                    conditions=(
                        CompareCondition(
                            op="lt",
                            lhs=InstrumentOperand(input_id="X", field="close"),
                            rhs=ConstantOperand(value=160.0),
                        ),
                    ),
                ),
            ),
            exits=(
                Block(
                    id="X1",
                    input_id="X",
                    weight=0.0,
                    conditions=(
                        CompareCondition(
                            op="ge",
                            lhs=InstrumentOperand(input_id="X", field="close"),
                            rhs=ConstantOperand(value=200.0),
                        ),
                    ),
                    target_entry_block_names=("Entry",),
                ),
            ),
        ),
    )


def _synthetic(result) -> np.ndarray:
    """The exact ``_evaluate_signal_leg`` synthetic-price conversion."""
    agg = np.zeros(len(result.index), dtype=np.float64)
    for pos in result.positions:
        agg += pos.realized_pnl
    return 100.0 * (1.0 + agg)


async def _leg_synthetic(
    signal: Signal, fetcher, cost_config: CostConfig
) -> np.ndarray:
    """Run the REAL ``_evaluate_signal_leg`` with only DB-boundary shims stubbed."""
    leg = MagicMock()
    leg.signal_spec.indicators = []
    with (
        patch(
            "tcg.core.api.portfolio._resolve_basket_inputs",
            new=AsyncMock(return_value={}),
        ),
        patch("tcg.core.api.portfolio.parse_signal", return_value=signal),
        patch(
            "tcg.core.api.portfolio.compute_input_overlap",
            new=AsyncMock(return_value=(None, None)),
        ),
        patch("tcg.core.api.portfolio.make_signal_fetcher", return_value=fetcher),
    ):
        res = await _evaluate_signal_leg(
            "L", leg, MagicMock(), None, None, MagicMock(), cost_config
        )
    return res.synthetic


async def test_signal_leg_pays_its_internal_costs():
    """The wrapped signal leg's synthetic reflects internal costs and equals the
    same signal run standalone at the same bps; at 0 bps it is byte-identical to
    the pre-cost behaviour."""
    closes = np.array([100.0, 200.0, 150.0, 200.0, 400.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    signal = _reentry_signal()

    cfg10 = CostConfig(slippage_bps=10.0, fees_bps=5.0)

    # Standalone signal: costs must actually bite (turnover > 0 here).
    res_gross = await evaluate_signal(signal, {}, fetcher)  # pre-cost (None)
    res_net = await evaluate_signal(signal, {}, fetcher, cfg10)
    assert res_net.total_slippage_paid_pct > 0.0
    assert res_net.total_fees_paid_pct > 0.0
    std_gross = _synthetic(res_gross)
    std_net = _synthetic(res_net)
    # Sanity: the standalone signal's net equity is materially below gross.
    assert std_net[-1] < std_gross[-1] - 1e-6

    # Wrapped as a 100%-weight portfolio signal leg through the REAL path.
    leg_gross = await _leg_synthetic(signal, fetcher, CostConfig())
    leg_net = await _leg_synthetic(signal, fetcher, cfg10)

    # (a) 0 bps: byte-identical to the pre-cost synthetic (golden-master gate).
    np.testing.assert_array_equal(leg_gross, std_gross)

    # (b) 10 bps: the leg's internal costs fold into its synthetic — it drops
    #     materially below the 0-bps leg and matches the standalone net signal
    #     (the composed-leg semantics the user chose: charge children).
    assert leg_net[-1] < leg_gross[-1] - 1e-6
    np.testing.assert_allclose(leg_net, std_net, rtol=1e-12, atol=1e-12)
