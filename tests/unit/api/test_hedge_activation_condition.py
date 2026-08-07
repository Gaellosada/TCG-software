"""P2b — hedge activation as an arbitrary signals-layer Condition (CORE-resolved).

DB-free.  Proves that a :class:`tcg.types.signal.HedgeSpec` carrying an
``activation`` Condition gates the hedge on/off over time — resolved in the CORE
layer to the ``hedge_active`` bool array and ANDed with the roll-pause flag —
instead of the degenerate ``series <op> threshold`` gate.  The degenerate gate
remains the back-compat default (``activation is None``).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tcg.core.api._series_fetch import resolve_hedge_activation_gate
from tcg.core.api.portfolio import _build_delta_hedge_arrays
from tcg.types.market import PriceSeries
from tcg.types.options import ByDelta, NearestToTarget
from tcg.types.signal import (
    CompareCondition,
    ConstantOperand,
    HedgeSpec,
    Input,
    InstrumentContinuous,
    InstrumentOperand,
    InstrumentOptionStream,
    InstrumentSpot,
)

DATES = np.array([20240102, 20240103, 20240104], dtype=np.int64)


def _ps(vals):
    a = np.asarray(vals, dtype=np.float64)
    return PriceSeries(dates=DATES, open=a, high=a, low=a, close=a, volume=np.zeros(3))


@dataclass
class _Cont:
    prices: PriceSeries


class _Svc:
    async def get_continuous(self, collection, roll_config, *, start=None, end=None):
        return _Cont(prices=_ps([50.0, 51.0, 52.0]))

    async def get_prices(self, collection, instrument_id, *, start=None, end=None):
        return _ps([100.0, 100.0, 100.0])


def _gate_fetcher(gate_vals):
    """Fetcher: option-delta second-resolve → delta; the gate InstrumentSpot →
    ``gate_vals`` (the condition operand)."""

    async def _fetch(instrument, field):
        if isinstance(instrument, InstrumentOptionStream):
            return DATES, np.array([0.4, 0.5, 0.6], dtype=np.float64)
        # the activation operand's instrument (VVIX spot)
        return DATES, np.asarray(gate_vals, dtype=np.float64)

    return _fetch


def _opt() -> InstrumentOptionStream:
    return InstrumentOptionStream(
        collection="OPT_VIX",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=0.30, tolerance=0.20),
        stream="mid",
        hold_between_rolls=True,
    )


def _hedge_with_activation(condition, inputs):
    return HedgeSpec(
        hedge_instrument=InstrumentContinuous(
            collection="FUT_ES", adjustment="difference", strategy="front_month"
        ),
        activation=condition,
        activation_inputs=inputs,
    )


async def test_condition_gated_activation_toggles_over_time() -> None:
    """VVIX > 150 as a CompareCondition activates the hedge only on the bar
    where VVIX exceeds 150 (middle bar), off otherwise."""
    cond = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="G", field="close"),
        rhs=ConstantOperand(value=150.0),
    )
    inputs = (
        ("G", Input(id="G", instrument=InstrumentSpot(collection="INDEX", instrument_id="IND_VVIX"))),
    )
    hedge = _hedge_with_activation(cond, inputs)
    _hd, _hp, hedge_active = await _build_delta_hedge_arrays(
        label="cond",
        hedge=hedge,
        instrument=_opt(),
        fetcher=_gate_fetcher([100.0, 200.0, 100.0]),  # only bar 1 > 150
        svc=_Svc(),
        dates_arr=DATES,
        is_roll_mask=np.zeros(3, dtype=np.bool_),
        start_date=None,
        end_date=None,
    )
    np.testing.assert_array_equal(hedge_active, np.array([False, True, False]))


async def test_condition_activation_anded_with_roll_pause() -> None:
    """Even where the condition is True, a roll bar (pause_on_roll default)
    forces the hedge OFF."""
    cond = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="G", field="close"),
        rhs=ConstantOperand(value=150.0),
    )
    inputs = (
        ("G", Input(id="G", instrument=InstrumentSpot(collection="INDEX", instrument_id="IND_VVIX"))),
    )
    hedge = _hedge_with_activation(cond, inputs)
    is_roll = np.array([False, True, False], dtype=np.bool_)  # bar 1 is a roll bar
    _hd, _hp, hedge_active = await _build_delta_hedge_arrays(
        label="cond",
        hedge=hedge,
        instrument=_opt(),
        fetcher=_gate_fetcher([200.0, 200.0, 200.0]),  # condition True everywhere
        svc=_Svc(),
        dates_arr=DATES,
        is_roll_mask=is_roll,
        start_date=None,
        end_date=None,
    )
    # condition True on all bars, but bar 1 is paused by the roll ⇒ [T, F, T]
    np.testing.assert_array_equal(hedge_active, np.array([True, False, True]))


async def test_resolve_hedge_activation_gate_direct() -> None:
    """The core helper aligns a consecutive-days CompareCondition to the axis;
    a bar absent from the condition index is INACTIVE."""
    cond = CompareCondition(
        op="lt",
        lhs=InstrumentOperand(input_id="V", field="close"),
        rhs=ConstantOperand(value=20.0),
        consecutive_days=2,
    )
    inputs = (
        ("V", Input(id="V", instrument=InstrumentSpot(collection="INDEX", instrument_id="IND_VIX"))),
    )

    async def _f(instrument, field):
        return DATES, np.array([25.0, 15.0, 15.0], dtype=np.float64)

    gate = await resolve_hedge_activation_gate(
        label="v",
        condition=cond,
        activation_inputs=inputs,
        fetch_fn=_f,
        axis_dates=DATES,
    )
    # <20 on bars 1,2; run-length>=2 only on bar 2 ⇒ [F, F, T]
    np.testing.assert_array_equal(gate, np.array([False, False, True]))
