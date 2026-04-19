"""Unit tests for the Signal evaluator — one scenario per primitive plus
composition and alignment edges. Pure-evaluator tests: the ``fetcher`` is
a fake dict lookup, indicator specs are injected directly.
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.signal_exec import (
    IndicatorSpecInput,
    SignalDataError,
    SignalValidationError,
    _first_instrument_operand,
    evaluate_signal,
)
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    CrossCondition,
    InRangeCondition,
    IndicatorOperand,
    InstrumentOperand,
    RollingCondition,
    Signal,
    SignalRules,
)


# ── Helpers ────────────────────────────────────────────────────────────────


DATES_10 = np.array(
    [
        20240102, 20240103, 20240104, 20240105, 20240108,
        20240109, 20240110, 20240111, 20240112, 20240115,
    ],
    dtype=np.int64,
)


def make_fetcher(data: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]):
    async def fetch(collection, instrument_id, field):
        return data[(collection, instrument_id)]

    return fetch


def inst(col: str, sid: str) -> InstrumentOperand:
    return InstrumentOperand(collection=col, instrument_id=sid)


def const(x: float) -> ConstantOperand:
    return ConstantOperand(value=x)


def sig(
    *,
    long_entry: tuple[Block, ...] = (),
    long_exit: tuple[Block, ...] = (),
    short_entry: tuple[Block, ...] = (),
    short_exit: tuple[Block, ...] = (),
) -> Signal:
    return Signal(
        id="s",
        name="s",
        rules=SignalRules(
            long_entry=long_entry,
            long_exit=long_exit,
            short_entry=short_entry,
            short_exit=short_exit,
        ),
    )


# ── Condition primitives ───────────────────────────────────────────────────


class TestCompare:
    async def test_gt_true_and_false(self):
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})

        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(3.0)),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Gt 3 → [F,F,F,T,T]; long_exit empty so long_pos = long_entry_score.
        assert r.long_score.tolist() == [0.0, 0.0, 0.0, 1.0, 1.0]
        assert r.position.tolist() == [0.0, 0.0, 0.0, 1.0, 1.0]
        assert r.entries_long == [3]
        assert r.exits_long == []


class TestCross:
    async def test_cross_above(self):
        # A crosses above B at t=2 (A[1]=1<=B[1]=2, A[2]=3>B[2]=2).
        a = np.array([0.0, 1.0, 3.0, 4.0, 1.0], dtype=np.float64)
        b = np.array([2.0, 2.0, 2.0, 2.0, 2.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher(
            {("X", "A"): (dates, a), ("X", "B"): (dates, b)}
        )
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CrossCondition(
                            op="cross_above", lhs=inst("X", "A"), rhs=inst("X", "B")
                        ),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Only t=2 fires; t=0 is False by definition.
        assert r.long_score.tolist() == [0.0, 0.0, 1.0, 0.0, 0.0]
        assert r.entries_long == [2]
        assert r.exits_long == [3]

    async def test_cross_below_directional(self):
        """cross_below must not fire on an upward cross."""
        a = np.array([0.0, 1.0, 3.0, 4.0, 1.0], dtype=np.float64)
        b = np.array([2.0, 2.0, 2.0, 2.0, 2.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher(
            {("X", "A"): (dates, a), ("X", "B"): (dates, b)}
        )
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CrossCondition(
                            op="cross_below", lhs=inst("X", "A"), rhs=inst("X", "B")
                        ),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # A drops from 4→1 at t=4 crossing below 2 → cross_below fires at t=4.
        # Upward cross at t=2 must NOT fire.
        assert r.long_score.tolist() == [0.0, 0.0, 0.0, 0.0, 1.0]
        assert r.entries_long == [4]


class TestInRange:
    async def test_in_range_inclusive(self):
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        InRangeCondition(
                            op="in_range",
                            operand=inst("X", "A"),
                            min=const(2.0),
                            max=const(4.0),
                        ),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # [1,2,3,4,5] in [2,4] → [F,T,T,T,F]
        assert r.long_score.tolist() == [0.0, 1.0, 1.0, 1.0, 0.0]


class TestRolling:
    async def test_rolling_gt_lookback_edge(self):
        """t < lookback must be False; no NaN→0 side-effect in warmup window.

        With only a rolling condition and no NaN data, the warmup window
        must produce False (score 0) without zeroing out downstream fires.
        """
        values = np.array([10.0, 11.0, 12.0, 11.0, 15.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, values)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        RollingCondition(
                            op="rolling_gt",
                            operand=inst("X", "A"),
                            lookback=2,
                        ),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # t=0,1 → False; t=2: 12>10 T; t=3: 11>11 F; t=4: 15>12 T.
        assert r.long_score.tolist() == [0.0, 0.0, 1.0, 0.0, 1.0]
        # Position must follow (not be force-zeroed by NaN rule).
        assert r.position.tolist() == [0.0, 0.0, 1.0, 0.0, 1.0]

    async def test_rolling_with_interior_nan_false_at_t_and_t_plus_lookback(
        self,
    ):
        """An interior NaN at index ``n`` poisons the rolling output at both
        ``t == n`` (because ``x[t]`` is NaN) and ``t == n + lookback``
        (because ``x[t - lookback]`` is NaN). Both positions must read
        False in the score even though the surrounding data would
        otherwise fire.
        """
        values = np.array(
            [1.0, 2.0, np.nan, 4.0, 5.0, 6.0], dtype=np.float64
        )
        dates = DATES_10[:6]
        fetcher = make_fetcher({("X", "A"): (dates, values)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        RollingCondition(
                            op="rolling_gt",
                            operand=inst("X", "A"),
                            lookback=2,
                        ),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Warm-up window: t=0,1 → False (t < lookback).
        # t=2: x[t] is NaN → False.
        # t=3: cur=4, prev=x[1]=2 → True.
        # t=4: prev=x[2]=NaN → False (NaN at t+lookback, as per brief).
        # t=5: cur=6, prev=x[3]=4 → True.
        assert r.long_score.tolist() == [0.0, 0.0, 0.0, 1.0, 0.0, 1.0]
        # The NaN→0 rule force-zeros position at t=2 (x[t] NaN) AND at t=4
        # (x[t-lookback] NaN) — both must be zero regardless of upstream
        # fires.
        assert r.position[2] == 0.0
        assert r.position[4] == 0.0
        # And the clean firing timesteps still carry position=1.
        assert r.position[3] == 1.0
        assert r.position[5] == 1.0

    async def test_rolling_lt_lookback_larger_than_series(self):
        values = np.array([5.0, 4.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, values)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        RollingCondition(
                            op="rolling_lt",
                            operand=inst("X", "A"),
                            lookback=5,
                        ),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert r.long_score.tolist() == [0.0, 0.0, 0.0]
        assert r.position.tolist() == [0.0, 0.0, 0.0]


# ── Composition ────────────────────────────────────────────────────────────


class TestComposition:
    async def test_fraction_score_two_of_three_blocks(self):
        """Three blocks, two firing ⇒ score = 2/3."""
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        # Build 3 long_entry blocks that all collapse to a plain comparison on A.
        # At t=3 (A=4): gt 3 T, gt 5 F, gt 0 T → 2 of 3 → 2/3.
        s = sig(
            long_entry=(
                Block(
                    conditions=(CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(3.0)),)
                ),
                Block(
                    conditions=(CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(5.0)),)
                ),
                Block(
                    conditions=(CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),)
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # At t=3: 2/3. At t=4 (A=5): gt3 T, gt5 F, gt0 T → 2/3.
        assert r.long_score[3] == pytest.approx(2.0 / 3.0)
        assert r.long_score[4] == pytest.approx(2.0 / 3.0)
        # At t=0 (A=1): only gt0 fires → 1/3.
        assert r.long_score[0] == pytest.approx(1.0 / 3.0)

    async def test_kill_on_exit(self):
        """long_pos must be 0 whenever long_exit fires, regardless of entry."""
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),)
                ),
            ),
            long_exit=(
                Block(
                    conditions=(CompareCondition(op="ge", lhs=inst("X", "A"), rhs=const(4.0)),)
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Entry fires everywhere (A>0). Exit fires at t=3,4 (A>=4) → long_pos = 0 there.
        assert r.position.tolist() == [1.0, 1.0, 1.0, 0.0, 0.0]
        # Entry at t=0; exit at t=3.
        assert r.entries_long == [0]
        assert r.exits_long == [3]

    async def test_long_and_short_net(self):
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        # Long when A>3, short when A<2 → net position 0 in the middle.
        s = sig(
            long_entry=(
                Block(
                    conditions=(CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(3.0)),)
                ),
            ),
            short_entry=(
                Block(
                    conditions=(CompareCondition(op="lt", lhs=inst("X", "A"), rhs=const(2.0)),)
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # t=0: A=1 short; t=1..3 middle; t=3..4: A>3 long. Actually t=3: 4>3 long.
        assert r.position.tolist() == [-1.0, 0.0, 0.0, 1.0, 1.0]
        assert r.entries_short == [0]
        assert r.exits_short == [1]
        assert r.entries_long == [3]


# ── NaN handling ───────────────────────────────────────────────────────────


class TestNaN:
    async def test_nan_forces_position_zero(self):
        closes = np.array([1.0, 2.0, np.nan, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),)
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Entry would fire everywhere (>0) except t=2 (NaN → False).
        # Position=1 at t=0,1,3,4 and 0 at t=2 (NaN→0 rule).
        assert r.position.tolist() == [1.0, 1.0, 0.0, 1.0, 1.0]
        # Entries/exits reflect the hole.
        assert r.entries_long == [0, 3]
        assert r.exits_long == [2]


# ── Union alignment ────────────────────────────────────────────────────────


class TestUnionAlignment:
    async def test_two_series_different_indices_unioned(self):
        """Operand A has dates {1,2,3}, operand B has dates {2,3,4}.

        Union index = {1,2,3,4}. Each operand is NaN outside its own index.
        A condition referencing only A should still span the union (with
        NaN→0 applied at {4}). A condition referencing only B spans the
        union with NaN→0 applied at {1}.
        """
        dates_a = np.array([20240102, 20240103, 20240104], dtype=np.int64)
        dates_b = np.array([20240103, 20240104, 20240105], dtype=np.int64)
        vals_a = np.array([10.0, 20.0, 30.0], dtype=np.float64)
        vals_b = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        fetcher = make_fetcher(
            {("X", "A"): (dates_a, vals_a), ("X", "B"): (dates_b, vals_b)}
        )
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),
                        CompareCondition(op="gt", lhs=inst("X", "B"), rhs=const(0.0)),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Union = [0102, 0103, 0104, 0105]. Both A and B non-NaN only at
        # {0103, 0104} → long_pos = 1 there, 0 elsewhere (NaN→0).
        assert r.index.tolist() == [20240102, 20240103, 20240104, 20240105]
        assert r.position.tolist() == [0.0, 1.0, 1.0, 0.0]

    async def test_constant_broadcast_against_series(self):
        """A constant operand must broadcast to length T without producing
        NaN-driven zeroing on its own."""
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(1.5)),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert r.position.tolist() == [0.0, 1.0, 1.0]


# ── End-to-end with an inline indicator spec ───────────────────────────────


class TestIndicatorOperand:
    async def test_indicator_sma_vs_price(self):
        """Signal: go long when price > SMA(3, price)."""
        closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0], dtype=np.float64)
        dates = DATES_10[:7]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})

        sma_code = (
            "def compute(series, window: int = 3):\n"
            "    s = series['price']\n"
            "    out = np.full_like(s, np.nan, dtype=float)\n"
            "    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')\n"
            "    return out\n"
        )

        indicators = {
            "sma3": IndicatorSpecInput(
                code=sma_code,
                params={"window": 3},
                series_map={"price": ("X", "A")},
            ),
        }

        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=inst("X", "A"),
                            rhs=IndicatorOperand(indicator_id="sma3"),
                        ),
                    ),
                ),
            ),
        )
        r = await evaluate_signal(s, indicators, fetcher)
        # At t=2 (first SMA): SMA=(10+11+12)/3=11, price=12 → long.
        # At t=5 (price=13): SMA=(13+14+13)/3=13.333 → price<SMA → False.
        # At t=0,1: SMA is NaN → NaN→0 rule forces position = 0.
        assert r.position[0] == 0.0
        assert r.position[1] == 0.0
        assert r.position[2] == 1.0
        assert r.position[3] == 1.0
        assert r.position[5] == 0.0

    async def test_missing_indicator_spec_raises_data_error(self):
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=inst("X", "A"),
                            rhs=IndicatorOperand(indicator_id="missing"),
                        ),
                    ),
                ),
            ),
        )
        with pytest.raises(SignalDataError):
            await evaluate_signal(s, {}, fetcher)


# ── _first_instrument_operand — direct walk-order unit tests ──────────────


class TestFirstInstrumentOperand:
    """Exercise the stable walk order of ``_first_instrument_operand``
    without routing through the full evaluator — see signal_exec.py:171–187.

    Order: long_entry → long_exit → short_entry → short_exit; block index;
    condition index; lhs before rhs; operand before min before max for
    in_range; operand-only for rolling.
    """

    def test_none_when_no_instrument_operands(self):
        # Indicator-only and constant-only conditions → no InstrumentOperand.
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=IndicatorOperand(indicator_id="ind1"),
                            rhs=const(0.0),
                        ),
                    ),
                ),
            ),
        )
        assert _first_instrument_operand(s) is None

    def test_direction_priority_long_entry_first(self):
        target = inst("X", "LE")
        s = sig(
            long_entry=(Block(conditions=(
                CompareCondition(op="gt", lhs=target, rhs=const(0.0)),
            )),),
            long_exit=(Block(conditions=(
                CompareCondition(op="gt", lhs=inst("X", "LX"), rhs=const(0.0)),
            )),),
            short_entry=(Block(conditions=(
                CompareCondition(op="gt", lhs=inst("X", "SE"), rhs=const(0.0)),
            )),),
            short_exit=(Block(conditions=(
                CompareCondition(op="gt", lhs=inst("X", "SX"), rhs=const(0.0)),
            )),),
        )
        assert _first_instrument_operand(s) is target

    def test_direction_priority_skips_empty_to_long_exit(self):
        target = inst("X", "LX")
        s = sig(
            long_exit=(Block(conditions=(
                CompareCondition(op="gt", lhs=target, rhs=const(0.0)),
            )),),
            short_entry=(Block(conditions=(
                CompareCondition(op="gt", lhs=inst("X", "SE"), rhs=const(0.0)),
            )),),
        )
        assert _first_instrument_operand(s) is target

    def test_block_index_precedes_later_blocks(self):
        target = inst("X", "B0")
        s = sig(
            long_entry=(
                Block(conditions=(
                    CompareCondition(op="gt", lhs=target, rhs=const(0.0)),
                )),
                Block(conditions=(
                    CompareCondition(op="gt", lhs=inst("X", "B1"), rhs=const(0.0)),
                )),
            ),
        )
        assert _first_instrument_operand(s) is target

    def test_condition_index_precedes_later_conditions(self):
        target = inst("X", "C0")
        s = sig(
            long_entry=(Block(conditions=(
                CompareCondition(
                    op="gt",
                    lhs=IndicatorOperand(indicator_id="ind"),
                    rhs=const(0.0),
                ),  # no instrument operand here
                CompareCondition(op="gt", lhs=target, rhs=const(0.0)),
                CompareCondition(op="gt", lhs=inst("X", "C2"), rhs=const(0.0)),
            )),),
        )
        assert _first_instrument_operand(s) is target

    def test_lhs_before_rhs(self):
        lhs_target = inst("X", "LHS")
        s = sig(
            long_entry=(Block(conditions=(
                CompareCondition(op="gt", lhs=lhs_target, rhs=inst("X", "RHS")),
            )),),
        )
        assert _first_instrument_operand(s) is lhs_target

    def test_cross_condition_lhs_before_rhs(self):
        lhs_target = inst("X", "CA_L")
        s = sig(
            long_entry=(Block(conditions=(
                CrossCondition(op="cross_above", lhs=lhs_target, rhs=inst("X", "CA_R")),
            )),),
        )
        assert _first_instrument_operand(s) is lhs_target

    def test_in_range_operand_then_min_then_max(self):
        op_target = inst("X", "OP")
        s = sig(
            long_entry=(Block(conditions=(
                InRangeCondition(
                    op="in_range",
                    operand=op_target,
                    min=inst("X", "MIN"),
                    max=inst("X", "MAX"),
                ),
            )),),
        )
        assert _first_instrument_operand(s) is op_target

    def test_in_range_min_precedes_max_when_operand_not_instrument(self):
        min_target = inst("X", "MIN")
        s = sig(
            long_entry=(Block(conditions=(
                InRangeCondition(
                    op="in_range",
                    operand=IndicatorOperand(indicator_id="ind"),
                    min=min_target,
                    max=inst("X", "MAX"),
                ),
            )),),
        )
        assert _first_instrument_operand(s) is min_target

    def test_rolling_condition_operand(self):
        op_target = inst("X", "ROLL")
        s = sig(
            long_entry=(Block(conditions=(
                RollingCondition(op="rolling_gt", operand=op_target, lookback=3),
            )),),
        )
        assert _first_instrument_operand(s) is op_target
