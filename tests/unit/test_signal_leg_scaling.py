"""Integration test pinning signal-leg → portfolio synthetic-price scaling.

Locks in the v4 weight semantics disclosed in CHANGELOG:

    block.weight = +100  ⇒  full-long unleveraged ⇒ synthetic mirrors underlying
    block.weight = -100  ⇒  full-short unleveraged ⇒ synthetic inverts underlying
    block.weight = +50   ⇒  half-long             ⇒ synthetic ≈ half the return

The test runs the real engine (no mocks of ``evaluate_signal``) and
applies the exact synthetic-price conversion used by
``tcg.core.api.portfolio._evaluate_signal_leg``:

    aggregated_pnl = Σ_positions pos.realized_pnl
    synthetic      = 100.0 * (1.0 + aggregated_pnl)

The signal backtest COMPOUNDS (Issue #4): per-input ``realized_pnl`` are
cumulative contributions whose sum is ``equity_ratio - 1``, so this
transform yields the compounded equity ``100 · equity_ratio``. The
expectations below therefore compound bar-to-bar (``Π(1 + pos·r)``), not
the old additive ``Σ pos·r``.

If anyone reintroduces the pre-v4 100× scaling (where ``weight=100``
over-amplified the underlying return by a factor of 100), these
assertions fail. That was the B3 review's concrete request: pin the
(block.weight → realized_pnl → synthetic) contract with one end-to-end
fixture.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

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


# Five consecutive business days — the same grid used across engine tests.
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


def _always_long_signal(weight: float) -> Signal:
    """A single-entry signal that opens a position on the first bar.

    The entry condition (``close > 0``) is always true, so the latch
    opens at t=0 and stays open forever (no exits). This gives a
    ``position`` series equal to ``weight/100`` on every bar.
    """
    return Signal(
        id="s_scale",
        name="scaling test",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="X",
                    weight=weight,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="X", field="close"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            ),
        ),
    )


def _synthetic_from_result(result) -> np.ndarray:
    """Replicate the portfolio ``_evaluate_signal_leg`` conversion.

    ``tcg.core.api.portfolio._evaluate_signal_leg`` aggregates
    ``realized_pnl`` across inputs then emits
    ``synthetic = 100.0 * (1.0 + aggregated_pnl)``. We replicate that
    verbatim so the assertion is an end-to-end pin on the exact
    synthetic series the portfolio layer consumes.
    """
    T = len(result.index)
    aggregated_pnl = np.zeros(T, dtype=np.float64)
    for pos in result.positions:
        aggregated_pnl += pos.realized_pnl
    return 100.0 * (1.0 + aggregated_pnl)


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weight_100_full_long_synthetic_mirrors_underlying_return():
    """weight=+100 ⇒ synthetic return == underlying return.

    Underlying goes 100 → 101 → 102 → 103 → 104 (+1%, +~0.99%, ~0.98%, ~0.97%).
    The entry opens at t=0; position is 1.0 thereafter. realized_pnl at
    bar t accumulates each bar's return. Synthetic starts at 100.0 and
    tracks the underlying's cumulative simple-return compounded
    step-wise.

    Concrete check: at the final bar, synthetic_return ≈
    (underlying_final - underlying_first) / underlying_first.
    """
    closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    signal = _always_long_signal(weight=100.0)
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)

    # Engine invariants first: position = 1.0 on every bar from t=0
    # onward (entry condition close>0 is always true, single block).
    assert list(result.positions[0].values) == pytest.approx([1.0, 1.0, 1.0, 1.0, 1.0])

    synthetic = _synthetic_from_result(result)

    # synthetic[0] == 100.0 by construction.
    assert synthetic[0] == pytest.approx(100.0)

    # The signal backtest COMPOUNDS (Issue #4): for a fully-long single
    # input the synthetic equity is the underlying COMPOUNDED bar-to-bar:
    #   equity(t) = 100 · Π_{k=1..t} (1 + (closes[k]-closes[k-1])/closes[k-1])
    # i.e. it tracks the underlying's own price ratio (100·closes[t]/closes[0]).
    expected_synthetic = 100.0 * (closes / closes[0])
    np.testing.assert_allclose(synthetic, expected_synthetic, rtol=1e-12, atol=1e-12)

    # Critical regression pin: the one-bar-move case. A +1% move on
    # bar 1 must produce synthetic[1] ≈ 101.0, NOT 200.0 (the pre-v4
    # 100× scaling bug) and NOT 100.01 (a hypothetical /100 bug).
    assert synthetic[1] == pytest.approx(101.0, abs=1e-10)


@pytest.mark.asyncio
async def test_weight_minus_100_full_short_synthetic_inverts_underlying_return():
    """weight=-100 ⇒ synthetic returns are the inverse of the underlying.

    Same price path but short: a +1% underlying move must map to a -1%
    synthetic move (synthetic[1] ≈ 99.0).
    """
    closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    signal = _always_long_signal(weight=-100.0)
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)

    assert list(result.positions[0].values) == pytest.approx(
        [-1.0, -1.0, -1.0, -1.0, -1.0]
    )

    synthetic = _synthetic_from_result(result)

    # Compounded (Issue #4): each bar multiplies equity by (1 + pos·r) =
    # (1 - r) for a full short. Expected = 100·Π(1 - r_k).
    expected_synthetic = np.empty_like(closes)
    expected_synthetic[0] = 100.0
    for t in range(1, len(closes)):
        r = (closes[t] - closes[t - 1]) / closes[t - 1]
        expected_synthetic[t] = expected_synthetic[t - 1] * (1.0 - r)

    np.testing.assert_allclose(synthetic, expected_synthetic, rtol=1e-12, atol=1e-12)

    # One-bar regression pin: +1% underlying ⇒ short synthetic at -1%.
    assert synthetic[1] == pytest.approx(99.0, abs=1e-10)


@pytest.mark.asyncio
async def test_weight_50_half_long_synthetic_is_half_the_return():
    """weight=+50 ⇒ synthetic return is half the underlying return.

    A +1% underlying move must produce synthetic ≈ 100.5 (0.5% up), not
    101.0 and not 100.01.
    """
    closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    signal = _always_long_signal(weight=50.0)
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)

    assert list(result.positions[0].values) == pytest.approx([0.5, 0.5, 0.5, 0.5, 0.5])

    synthetic = _synthetic_from_result(result)

    # Compounded (Issue #4): each bar multiplies equity by (1 + 0.5·r).
    # Expected = 100·Π(1 + 0.5·r_k).
    expected_synthetic = np.empty_like(closes)
    expected_synthetic[0] = 100.0
    for t in range(1, len(closes)):
        r = (closes[t] - closes[t - 1]) / closes[t - 1]
        expected_synthetic[t] = expected_synthetic[t - 1] * (1.0 + 0.5 * r)

    np.testing.assert_allclose(synthetic, expected_synthetic, rtol=1e-12, atol=1e-12)

    # One-bar pin.
    assert synthetic[1] == pytest.approx(100.5, abs=1e-10)


@pytest.mark.asyncio
async def test_signal_leg_synthetic_equals_signal_compounded_equity():
    """Parity (Issue #4): the portfolio signal-leg synthetic (start 100) must
    equal the signal's OWN compounded equity ``100 · equity_ratio``.

    This locks the ``core/api/portfolio._evaluate_signal_leg`` consumption to
    the engine's compounded curve: a portfolio holding a signal sees exactly
    the equity the Signals page shows. Uses a re-entry path (enter, double,
    exit, re-enter) so a non-compounding regression would diverge.
    """
    # 100 → 200 (double) → 150 → 200 (re-enter, +33%) → 400.
    closes = np.array([100.0, 200.0, 150.0, 200.0, 400.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})

    enter = CompareCondition(
        op="lt",
        lhs=InstrumentOperand(input_id="X", field="close"),
        rhs=ConstantOperand(value=160.0),
    )
    exit_c = CompareCondition(
        op="ge",
        lhs=InstrumentOperand(input_id="X", field="close"),
        rhs=ConstantOperand(value=200.0),
    )
    signal = Signal(
        id="s_parity",
        name="parity",
        inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(
                Block(
                    id="E",
                    name="Entry",
                    input_id="X",
                    weight=100.0,
                    conditions=(enter,),
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

    synthetic = _synthetic_from_result(result)
    # Portfolio leg synthetic (start 100) == signal's compounded equity.
    np.testing.assert_allclose(
        synthetic, 100.0 * result.equity_ratio, rtol=1e-12, atol=1e-12
    )
    # And it matches the worked numbers: re-entry redeploys accrued $200.
    assert synthetic[-1] == pytest.approx(266.6666666, rel=1e-9)
