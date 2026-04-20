"""Unit tests for the Signal evaluator -- one scenario per primitive plus
composition, alignment, and v2-specific features (clipping, multi-instrument,
indicator override caching, sentinel-block skipping).

v2 (iter-3): every block now carries ``instrument`` + ``weight``; the
evaluator emits one position series per unique instrument and a
``clipped`` flag. Existing iter-1 cases were updated to the v2 shape --
each test sets ``instrument=ref("X","A")`` (or similar) on its blocks
and asserts ``positions[0].values`` instead of the old flat ``position``.
"""

from __future__ import annotations

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
    CrossCondition,
    InRangeCondition,
    IndicatorOperand,
    InstrumentOperand,
    InstrumentRef,
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


def ref(col: str, sid: str) -> InstrumentRef:
    """Shortcut for an :class:`InstrumentRef` block identifier."""
    return InstrumentRef(collection=col, instrument_id=sid)


def blk(
    *conds,
    instrument: InstrumentRef | None,
    weight: float = 1.0,
) -> Block:
    """Compact Block constructor for v2 tests."""
    return Block(conditions=tuple(conds), instrument=instrument, weight=weight)


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
        # v2 update: block carries instrument + weight=1.0; we assert
        # positions[0].values instead of the old flat position vector.
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})

        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(3.0)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert len(r.positions) == 1
        assert r.positions[0].values.tolist() == [0.0, 0.0, 0.0, 1.0, 1.0]
        assert r.clipped is False


class TestCross:
    async def test_cross_above(self):
        # v2: single-instrument assertion through positions[0].
        a = np.array([0.0, 1.0, 3.0, 4.0, 1.0], dtype=np.float64)
        b = np.array([2.0, 2.0, 2.0, 2.0, 2.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher(
            {("X", "A"): (dates, a), ("X", "B"): (dates, b)}
        )
        s = sig(
            long_entry=(
                blk(
                    CrossCondition(
                        op="cross_above", lhs=inst("X", "A"), rhs=inst("X", "B")
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Only t=2 fires; t=0 is False by definition.
        assert r.positions[0].values.tolist() == [0.0, 0.0, 1.0, 0.0, 0.0]

    async def test_cross_below_directional(self):
        """cross_below must not fire on an upward cross.

        v2: single-instrument path; weight=1.0 ⇒ score == fire mask.
        """
        a = np.array([0.0, 1.0, 3.0, 4.0, 1.0], dtype=np.float64)
        b = np.array([2.0, 2.0, 2.0, 2.0, 2.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher(
            {("X", "A"): (dates, a), ("X", "B"): (dates, b)}
        )
        s = sig(
            long_entry=(
                blk(
                    CrossCondition(
                        op="cross_below", lhs=inst("X", "A"), rhs=inst("X", "B")
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # A drops from 4→1 at t=4 crossing below 2 → cross_below fires at t=4.
        assert r.positions[0].values.tolist() == [0.0, 0.0, 0.0, 0.0, 1.0]


class TestInRange:
    async def test_in_range_inclusive(self):
        # v2: block carries instrument + weight=1.0.
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    InRangeCondition(
                        op="in_range",
                        operand=inst("X", "A"),
                        min=const(2.0),
                        max=const(4.0),
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # [1,2,3,4,5] in [2,4] → [F,T,T,T,F]
        assert r.positions[0].values.tolist() == [0.0, 1.0, 1.0, 1.0, 0.0]


class TestRolling:
    async def test_rolling_gt_lookback_edge(self):
        """t < lookback must be False; no NaN→0 side-effect in warmup window.

        v2 update: positions[0].values (single instrument).
        """
        values = np.array([10.0, 11.0, 12.0, 11.0, 15.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, values)})
        s = sig(
            long_entry=(
                blk(
                    RollingCondition(
                        op="rolling_gt",
                        operand=inst("X", "A"),
                        lookback=2,
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # t=0,1 warmup → False; t=2: 12>10 T; t=3: 11>11 F; t=4: 15>12 T.
        assert r.positions[0].values.tolist() == [0.0, 0.0, 1.0, 0.0, 1.0]

    async def test_rolling_with_interior_nan_false_at_t_and_t_plus_lookback(
        self,
    ):
        """An interior NaN at index ``n`` poisons the rolling output at both
        ``t == n`` and ``t == n + lookback``.  Under v2 the per-instrument
        NaN→0 rule also forces position=0 at those steps.
        """
        values = np.array(
            [1.0, 2.0, np.nan, 4.0, 5.0, 6.0], dtype=np.float64
        )
        dates = DATES_10[:6]
        fetcher = make_fetcher({("X", "A"): (dates, values)})
        s = sig(
            long_entry=(
                blk(
                    RollingCondition(
                        op="rolling_gt",
                        operand=inst("X", "A"),
                        lookback=2,
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        values = r.positions[0].values
        # NaN-poison forces t=2 and t=4 to 0; clean firings at t=3 and t=5.
        assert values[0] == 0.0
        assert values[1] == 0.0
        assert values[2] == 0.0
        assert values[3] == 1.0
        assert values[4] == 0.0
        assert values[5] == 1.0

    async def test_rolling_lt_lookback_larger_than_series(self):
        # v2 update: positions[0].values.
        values = np.array([5.0, 4.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, values)})
        s = sig(
            long_entry=(
                blk(
                    RollingCondition(
                        op="rolling_lt",
                        operand=inst("X", "A"),
                        lookback=5,
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert r.positions[0].values.tolist() == [0.0, 0.0, 0.0]


# ── Composition ────────────────────────────────────────────────────────────


class TestComposition:
    async def test_weighted_score_two_of_three_blocks(self):
        """Three entry blocks with weight=0.25 each on the same instrument.
        Two firing ⇒ long_score = 0.5.

        v2: old "fraction of blocks" score replaced by Σ weight of active
        blocks. Three blocks @ 0.25 each → max 0.75 (no clipping).
        """
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(3.0)),
                    instrument=ref("X", "A"),
                    weight=0.25,
                ),
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(5.0)),
                    instrument=ref("X", "A"),
                    weight=0.25,
                ),
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),
                    instrument=ref("X", "A"),
                    weight=0.25,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        p = r.positions[0].values
        # At t=3 (A=4): gt3 T, gt5 F, gt0 T → score 0.5.
        # At t=4 (A=5): gt3 T, gt5 F, gt0 T → 0.5.
        # At t=0 (A=1): only gt0 fires → 0.25.
        assert p[3] == pytest.approx(0.5)
        assert p[4] == pytest.approx(0.5)
        assert p[0] == pytest.approx(0.25)
        assert r.clipped is False

    async def test_kill_on_exit(self):
        """long_pos = 0 whenever long_exit fires, regardless of entry.

        v2 update: entry block weight=1.0, exit block weight=0.0 (ignored
        for exits). clipped is False because exit fires before score>1.
        """
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
            long_exit=(
                blk(
                    CompareCondition(op="ge", lhs=inst("X", "A"), rhs=const(4.0)),
                    instrument=ref("X", "A"),
                    weight=0.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Entry everywhere (A>0). Exit at t=3,4 (A>=4) → long_pos=0 there.
        assert r.positions[0].values.tolist() == [1.0, 1.0, 1.0, 0.0, 0.0]

    async def test_long_and_short_net(self):
        # v2: two directions on same instrument → single positions entry.
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(3.0)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
            short_entry=(
                blk(
                    CompareCondition(op="lt", lhs=inst("X", "A"), rhs=const(2.0)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert len(r.positions) == 1
        assert r.positions[0].values.tolist() == [-1.0, 0.0, 0.0, 1.0, 1.0]


# ── v2: Clipping ───────────────────────────────────────────────────────────


class TestClipping:
    async def test_three_entry_blocks_weight_sum_gt_one_clips(self):
        """3 entry blocks summing weight > 1 on same instrument ⇒
        ``clipped_mask`` True on those t's; post-clip position = 1.0."""
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        # All three fire on t=2 (A=3): score = 0.5+0.4+0.3 = 1.2 > 1.
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(2.5)),
                    instrument=ref("X", "A"),
                    weight=0.5,
                ),
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(2.5)),
                    instrument=ref("X", "A"),
                    weight=0.4,
                ),
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(2.5)),
                    instrument=ref("X", "A"),
                    weight=0.3,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        values = r.positions[0].values
        mask = r.positions[0].clipped_mask
        # t=0,1 no fire; t=2 all three fire → clipped, pos=1.0.
        assert values.tolist() == [0.0, 0.0, 1.0]
        assert mask.tolist() == [False, False, True]
        assert r.clipped is True

    async def test_clipping_with_exit_firing_not_flagged(self):
        """Clipping with a matching exit firing ⇒ clipped=False at those t,
        position = 0 (exit wins)."""
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(2.5)),
                    instrument=ref("X", "A"),
                    weight=0.7,
                ),
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(2.5)),
                    instrument=ref("X", "A"),
                    weight=0.7,
                ),
            ),
            long_exit=(
                blk(
                    # Exit fires at t=2 (same time entries fire).
                    CompareCondition(op="ge", lhs=inst("X", "A"), rhs=const(3.0)),
                    instrument=ref("X", "A"),
                    weight=0.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert r.positions[0].values.tolist() == [0.0, 0.0, 0.0]
        assert r.positions[0].clipped_mask.tolist() == [False, False, False]
        assert r.clipped is False


# ── v2: Multi-instrument ───────────────────────────────────────────────────


class TestMultiInstrument:
    async def test_two_instruments_interleaved_activations(self):
        """2 instruments A and B with different weights and interleaved
        block activation ⇒ 2 entries in positions, each with correct values.
        """
        closes_a = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        closes_b = np.array([5.0, 4.0, 3.0, 2.0, 1.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher(
            {("X", "A"): (dates, closes_a), ("X", "B"): (dates, closes_b)}
        )
        s = sig(
            long_entry=(
                # Fires when A > 2.5 (t=2..4). Weight 0.5.
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(2.5)),
                    instrument=ref("X", "A"),
                    weight=0.5,
                ),
                # Fires when B > 2.5 (t=0..2). Weight 0.3.
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "B"), rhs=const(2.5)),
                    instrument=ref("X", "B"),
                    weight=0.3,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert len(r.positions) == 2
        # Order: A first (long_entry block 0), then B.
        a_pos = r.positions[0]
        b_pos = r.positions[1]
        assert (a_pos.instrument.collection, a_pos.instrument.instrument_id) == ("X", "A")
        assert (b_pos.instrument.collection, b_pos.instrument.instrument_id) == ("X", "B")
        assert a_pos.values.tolist() == [0.0, 0.0, 0.5, 0.5, 0.5]
        assert b_pos.values.tolist() == pytest.approx([0.3, 0.3, 0.3, 0.0, 0.0])
        assert r.clipped is False


# ── v2: Indicator override ─────────────────────────────────────────────────


class TestIndicatorOverride:
    async def test_different_params_override_produces_different_firings(
        self,
    ):
        """Same indicator id, different ``params_override.window`` → SMA
        is computed with a different window for each operand and the
        ``cross_above`` condition fires at different points. The override
        cache MUST key on the merged params so both variants coexist.
        """
        # Craft a series that makes close cross above SMA(5) at t≈6 but
        # cross above SMA(20)-based signal never in the window. Use 40
        # points so both windows have warmup room.
        n = 40
        # Steady downtrend, then flip up: crosses SMA(5) quickly, SMA(20)
        # much later.
        closes = np.concatenate(
            [np.linspace(100.0, 80.0, 20), np.linspace(80.1, 100.0, 20)]
        )
        dates = np.arange(20240101, 20240101 + n, dtype=np.int64)
        # Ensure strictly monotonic (calendar doesn't matter for unit test).
        fetcher = make_fetcher({("X", "A"): (dates, closes)})

        sma_code = (
            "def compute(series, window: int = 5):\n"
            "    s = series['price']\n"
            "    out = np.full_like(s, np.nan, dtype=float)\n"
            "    w = int(window)\n"
            "    if w <= len(s):\n"
            "        out[w-1:] = np.convolve(s, np.ones(w)/w, mode='valid')\n"
            "    return out\n"
        )

        indicators = {
            "sma": IndicatorSpecInput(
                code=sma_code,
                params={"window": 5},  # base default
                series_map={"price": ("X", "A")},
            ),
        }

        # Block 1: cross_above(close, SMA(5)) -- short window, reacts fast.
        block_sma5 = blk(
            CrossCondition(
                op="cross_above",
                lhs=inst("X", "A"),
                rhs=IndicatorOperand(
                    indicator_id="sma", params_override={"window": 5}
                ),
            ),
            instrument=ref("X", "A"),
            weight=1.0,
        )

        # Block 2: cross_above(close, SMA(20)) -- long window.
        block_sma20 = blk(
            CrossCondition(
                op="cross_above",
                lhs=inst("X", "A"),
                rhs=IndicatorOperand(
                    indicator_id="sma", params_override={"window": 20}
                ),
            ),
            instrument=ref("X", "A"),
            weight=1.0,
        )

        # Evaluate each block in isolation to get its firing pattern.
        r5 = await evaluate_signal(
            sig(long_entry=(block_sma5,)), indicators, fetcher
        )
        r20 = await evaluate_signal(
            sig(long_entry=(block_sma20,)), indicators, fetcher
        )
        fires5 = r5.positions[0].values > 0.0
        fires20 = r20.positions[0].values > 0.0

        # SMA(5) must fire at least once; SMA(20) may fire later or never.
        assert bool(fires5.any()), "SMA(5) cross_above must fire at least once"
        # The two firing patterns must DIFFER -- if they matched, the
        # override-aware cache key would be broken (both operands would
        # share the SMA(5) result).
        assert not np.array_equal(fires5, fires20), (
            "SMA(5) and SMA(20) produced identical cross firings; the "
            "override-aware cache is probably collapsing them."
        )

    async def test_same_override_reuses_cache(self):
        """Two conditions with identical overrides ⇒ the evaluator still
        produces consistent output (smoke test that the cache does not
        corrupt the shared slot).
        """
        closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        sma_code = (
            "def compute(series, window: int = 3):\n"
            "    s = series['price']\n"
            "    out = np.full_like(s, np.nan, dtype=float)\n"
            "    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')\n"
            "    return out\n"
        )
        indicators = {
            "sma": IndicatorSpecInput(
                code=sma_code,
                params={"window": 3},
                series_map={"price": ("X", "A")},
            ),
        }
        # Two blocks, each referencing the same override — same cache slot.
        op = IndicatorOperand(
            indicator_id="sma", params_override={"window": 3}
        )
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=op),
                    instrument=ref("X", "A"),
                    weight=0.5,
                ),
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=op),
                    instrument=ref("X", "A"),
                    weight=0.5,
                ),
            ),
        )
        r = await evaluate_signal(s, indicators, fetcher)
        p = r.positions[0].values
        # Both blocks fire identically once SMA is finite (t>=2 since window=3).
        # Score = 1.0 where close>sma → no clipping (sum == 1.0 exactly).
        assert p[0] == 0.0
        assert p[1] == 0.0
        assert p[2] == pytest.approx(1.0)  # close=12, sma=11 → fires, both blocks
        assert r.clipped is False


# ── v2: Sentinel / empty-block handling ────────────────────────────────────


class TestSentinelBlocks:
    async def test_empty_block_never_fires(self):
        """Block with no conditions ⇒ ignored (does not contribute score).

        v2 update: a block with zero conditions is not usable even with
        a valid instrument + weight; the evaluator skips it.
        """
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(instrument=ref("X", "A"), weight=0.5),  # empty conditions
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(1.5)),
                    instrument=ref("X", "A"),
                    weight=0.5,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Only the conditioned block fires → max score 0.5 at t=1,2.
        assert r.positions[0].values.tolist() == [0.0, 0.5, 0.5]

    async def test_block_with_instrument_none_skipped(self):
        """Block with ``instrument=None`` is SILENTLY SKIPPED.

        Design choice: the evaluator treats ``instrument=None`` as a
        sentinel "not yet picked" and skips the block rather than raising.
        The frontend's Run gate (blockShape helpers) prevents incomplete
        blocks from ever reaching compute, so this is defence-in-depth
        rather than a hard error. Documented as a skip in the engine
        module docstring.
        """
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                Block(
                    conditions=(
                        CompareCondition(
                            op="gt", lhs=inst("X", "A"), rhs=const(0.0)
                        ),
                    ),
                    instrument=None,  # sentinel
                    weight=1.0,
                ),
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # Only the second (valid) block contributes to instrument X/A.
        assert len(r.positions) == 1
        assert r.positions[0].values.tolist() == [1.0, 1.0, 1.0]

    async def test_entry_block_with_weight_zero_skipped(self):
        """Entry block with ``weight=0.0`` is skipped (sentinel "not picked").

        Exit blocks ignore weight, so this only applies on entry tabs.
        """
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),
                    instrument=ref("X", "A"),
                    weight=0.0,  # sentinel
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        # The only block is skipped → no instrument is referenced → empty
        # positions. The evaluator still returns a valid result.
        assert r.positions == ()
        assert r.clipped is False


# ── NaN handling ───────────────────────────────────────────────────────────


class TestNaN:
    async def test_nan_forces_position_zero(self):
        # v2: per-instrument NaN→0 on positions[0].values.
        closes = np.array([1.0, 2.0, np.nan, 4.0, 5.0], dtype=np.float64)
        dates = DATES_10[:5]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert r.positions[0].values.tolist() == [1.0, 1.0, 0.0, 1.0, 1.0]


# ── Union alignment ────────────────────────────────────────────────────────


class TestUnionAlignment:
    async def test_two_series_different_indices_unioned(self):
        """Operand A dates {1,2,3}, operand B dates {2,3,4} ⇒ union {1,2,3,4}.

        v2: both operands in same block, same instrument target, weight=1.0.
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
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(0.0)),
                    CompareCondition(op="gt", lhs=inst("X", "B"), rhs=const(0.0)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert r.index.tolist() == [20240102, 20240103, 20240104, 20240105]
        assert r.positions[0].values.tolist() == [0.0, 1.0, 1.0, 0.0]

    async def test_constant_broadcast_against_series(self):
        """A constant operand must broadcast to length T.

        v2 update: positions[0].values assertion.
        """
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(op="gt", lhs=inst("X", "A"), rhs=const(1.5)),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, {}, fetcher)
        assert r.positions[0].values.tolist() == [0.0, 1.0, 1.0]


# ── End-to-end with an inline indicator spec ───────────────────────────────


class TestIndicatorOperand:
    async def test_indicator_sma_vs_price(self):
        """Signal: go long when price > SMA(3, price).

        v2 update: block carries instrument + weight=1.0.
        """
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
                blk(
                    CompareCondition(
                        op="gt",
                        lhs=inst("X", "A"),
                        rhs=IndicatorOperand(indicator_id="sma3"),
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        r = await evaluate_signal(s, indicators, fetcher)
        p = r.positions[0].values
        # t=0,1 SMA NaN → 0; t=2 price=12 > SMA=11 → 1; t=5 price=13 < SMA=13.33 → 0.
        assert p[0] == 0.0
        assert p[1] == 0.0
        assert p[2] == 1.0
        assert p[3] == 1.0
        assert p[5] == 0.0

    async def test_missing_indicator_spec_raises_data_error(self):
        # v2 update: block instrument + weight supplied.
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        dates = DATES_10[:3]
        fetcher = make_fetcher({("X", "A"): (dates, closes)})
        s = sig(
            long_entry=(
                blk(
                    CompareCondition(
                        op="gt",
                        lhs=inst("X", "A"),
                        rhs=IndicatorOperand(indicator_id="missing"),
                    ),
                    instrument=ref("X", "A"),
                    weight=1.0,
                ),
            ),
        )
        with pytest.raises(SignalDataError):
            await evaluate_signal(s, {}, fetcher)
