"""Tests for continuous futures rolling: calendar, adjustment, and stitching."""

from __future__ import annotations

import numpy as np
import pytest

from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContractPriceData,
    PriceSeries,
    RollStrategy,
)
from tcg.data._rolling.calendar import compute_roll_dates, trim_overlaps
from tcg.data._rolling.adjustment import (
    adjust_proportional,
    adjust_difference,
    _find_closest_date_idx,
    _get_close_at_roll,
)
from tcg.data._rolling.stitcher import ContinuousSeriesBuilder


# ── Helpers ──────────────────────────────────────────────────────────


def _make_contract(
    contract_id: str,
    expiration: int,
    dates: list[int],
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> ContractPriceData:
    """Build a ContractPriceData with known values.

    By default, open=close, high=close*1.01, low=close*0.99, volume=1000.
    """
    n = len(dates)
    assert len(closes) == n
    c = np.array(closes, dtype=np.float64)
    return ContractPriceData(
        contract_id=contract_id,
        expiration=expiration,
        prices=PriceSeries(
            dates=np.array(dates, dtype=np.int64),
            open=np.array(opens, dtype=np.float64) if opens else c.copy(),
            high=np.array(highs, dtype=np.float64) if highs else c * 1.01,
            low=np.array(lows, dtype=np.float64) if lows else c * 0.99,
            close=c,
            volume=np.array(volumes, dtype=np.float64) if volumes else np.full(n, 1000.0),
        ),
    )


def _make_empty_contract(contract_id: str, expiration: int) -> ContractPriceData:
    """Build a ContractPriceData with zero-length arrays."""
    return ContractPriceData(
        contract_id=contract_id,
        expiration=expiration,
        prices=PriceSeries(
            dates=np.array([], dtype=np.int64),
            open=np.array([], dtype=np.float64),
            high=np.array([], dtype=np.float64),
            low=np.array([], dtype=np.float64),
            close=np.array([], dtype=np.float64),
            volume=np.array([], dtype=np.float64),
        ),
    )


# ── Calendar tests ──────────────────────────────────────────────────


class TestComputeRollDates:
    def test_single_contract(self):
        c1 = _make_contract("VXF24", 20240115, [20240101, 20240102], [20.0, 21.0])
        result = compute_roll_dates([c1], RollStrategy.FRONT_MONTH)
        assert result == []

    def test_two_contracts(self):
        c1 = _make_contract("VXF24", 20240115, [20240101, 20240102], [20.0, 21.0])
        c2 = _make_contract("VXG24", 20240215, [20240116, 20240117], [22.0, 23.0])
        result = compute_roll_dates([c1, c2], RollStrategy.FRONT_MONTH)
        assert result == [20240115]

    def test_three_contracts(self):
        c1 = _make_contract("VXF24", 20240115, [20240101], [20.0])
        c2 = _make_contract("VXG24", 20240215, [20240116], [22.0])
        c3 = _make_contract("VXH24", 20240315, [20240216], [24.0])
        result = compute_roll_dates([c1, c2, c3], RollStrategy.FRONT_MONTH)
        assert result == [20240115, 20240215]

    def test_empty_list(self):
        result = compute_roll_dates([], RollStrategy.FRONT_MONTH)
        assert result == []


class TestTrimOverlaps:
    def test_no_overlap(self):
        c1 = _make_contract("VXF24", 20240115, [20240110, 20240111], [20.0, 21.0])
        c2 = _make_contract("VXG24", 20240215, [20240116, 20240117], [22.0, 23.0])
        roll_dates = [20240115]
        trimmed = trim_overlaps([c1, c2], roll_dates)
        assert len(trimmed) == 2
        # c1 dates all <= 20240115, so all kept
        np.testing.assert_array_equal(trimmed[0].prices.dates, [20240110, 20240111])
        # c2 is last, all kept
        np.testing.assert_array_equal(trimmed[1].prices.dates, [20240116, 20240117])

    def test_overlap_trimmed(self):
        """When contracts have overlapping date ranges, trim at roll boundary."""
        # c1 has data through 20240120 but should be trimmed at expiration 20240115
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240112, 20240115, 20240117, 20240120],
            [20.0, 20.5, 21.0, 21.5, 22.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240113, 20240115, 20240117, 20240120],
            [22.0, 22.5, 23.0, 23.5],
        )
        roll_dates = [20240115]
        trimmed = trim_overlaps([c1, c2], roll_dates)
        assert len(trimmed) == 2
        # c1 keeps only dates <= 20240115
        np.testing.assert_array_equal(
            trimmed[0].prices.dates, [20240110, 20240112, 20240115]
        )
        # c2 is last, keeps all
        np.testing.assert_array_equal(
            trimmed[1].prices.dates, [20240113, 20240115, 20240117, 20240120]
        )

    def test_zero_close_stripped(self):
        """Rows with close == 0 are stripped from contracts."""
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240111, 20240112],
            [20.0, 0.0, 21.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240116, 20240117],
            [22.0, 23.0],
        )
        roll_dates = [20240115]
        trimmed = trim_overlaps([c1, c2], roll_dates)
        # c1 should have the zero-close row removed
        np.testing.assert_array_equal(trimmed[0].prices.dates, [20240110, 20240112])
        np.testing.assert_array_equal(trimmed[0].prices.close, [20.0, 21.0])

    def test_all_zeros_excluded(self):
        """Contract with all close==0 is excluded entirely."""
        c1 = _make_contract("VXF24", 20240115, [20240110, 20240111], [0.0, 0.0])
        c2 = _make_contract("VXG24", 20240215, [20240116, 20240117], [22.0, 23.0])
        roll_dates = [20240115]
        trimmed = trim_overlaps([c1, c2], roll_dates)
        assert len(trimmed) == 1
        assert trimmed[0].contract_id == "VXG24"

    def test_empty_contracts(self):
        trimmed = trim_overlaps([], [])
        assert trimmed == []


# ── Adjustment tests ────────────────────────────────────────────────


class TestAdjustProportional:
    def test_no_roll_dates(self):
        """With no roll dates, returns unchanged series."""
        ps = PriceSeries(
            dates=np.array([20240101, 20240102], dtype=np.int64),
            open=np.array([100.0, 101.0]),
            high=np.array([102.0, 103.0]),
            low=np.array([99.0, 100.0]),
            close=np.array([101.0, 102.0]),
            volume=np.array([1000.0, 1100.0]),
        )
        result = adjust_proportional(ps, [], [])
        np.testing.assert_array_equal(result.close, ps.close)

    def test_single_roll_proportional(self):
        """Two contracts: verify ratio applied to pre-roll prices."""
        # Roll date = 20240117 (first date of new contract segment in concat)
        # _get_close_at_roll(c1, 20240117) → closest date 20240115 → close=100
        # _get_close_at_roll(c2, 20240117) → exact match → close=107
        # Ratio = 107/100 = 1.07
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240112, 20240115],
            [95.0, 98.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240115, 20240117, 20240120],
            [105.0, 107.0, 110.0],
        )

        # Raw concatenated series (after trim: c1 up to 20240115, c2 from 20240117)
        raw_ps = PriceSeries(
            dates=np.array([20240110, 20240112, 20240115, 20240117, 20240120], dtype=np.int64),
            open=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            high=np.array([96.0, 99.0, 101.0, 108.0, 111.0]),
            low=np.array([94.0, 97.0, 99.0, 106.0, 109.0]),
            close=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            volume=np.array([1000.0, 1000.0, 1000.0, 1000.0, 1000.0]),
        )

        # Roll date = first date of new contract segment
        roll_dates = [20240117]

        result = adjust_proportional(raw_ps, roll_dates, [c1, c2])

        ratio = 107.0 / 100.0  # = 1.07
        # Dates before 20240117 should be multiplied by ratio
        np.testing.assert_allclose(result.close[:3], [95.0 * ratio, 98.0 * ratio, 100.0 * ratio])
        # Dates from 20240117 onward: unchanged
        np.testing.assert_allclose(result.close[3:], [107.0, 110.0])
        # Volume unchanged
        np.testing.assert_array_equal(result.volume, raw_ps.volume)

    def test_volume_unchanged(self):
        """Proportional adjustment must not modify volume."""
        c1 = _make_contract("A", 20240115, [20240110, 20240115], [100.0, 100.0])
        c2 = _make_contract("B", 20240215, [20240115, 20240120], [110.0, 115.0])
        raw_ps = PriceSeries(
            dates=np.array([20240110, 20240115, 20240120], dtype=np.int64),
            open=np.array([100.0, 100.0, 115.0]),
            high=np.array([101.0, 101.0, 116.0]),
            low=np.array([99.0, 99.0, 114.0]),
            close=np.array([100.0, 100.0, 115.0]),
            volume=np.array([500.0, 600.0, 700.0]),
        )
        result = adjust_proportional(raw_ps, [20240120], [c1, c2])
        np.testing.assert_array_equal(result.volume, [500.0, 600.0, 700.0])


class TestAdjustDifference:
    def test_single_roll_difference(self):
        """Two contracts: verify additive adjustment applied to pre-roll prices."""
        # Roll date = 20240117 (first date of new segment)
        # _get_close_at_roll(c1, 20240117) → closest date 20240115 → close=100
        # _get_close_at_roll(c2, 20240117) → exact match → close=107
        # Diff = 107 - 100 = +7
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240112, 20240115],
            [95.0, 98.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240115, 20240117, 20240120],
            [105.0, 107.0, 110.0],
        )

        raw_ps = PriceSeries(
            dates=np.array([20240110, 20240112, 20240115, 20240117, 20240120], dtype=np.int64),
            open=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            high=np.array([96.0, 99.0, 101.0, 108.0, 111.0]),
            low=np.array([94.0, 97.0, 99.0, 106.0, 109.0]),
            close=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            volume=np.array([1000.0, 1000.0, 1000.0, 1000.0, 1000.0]),
        )

        roll_dates = [20240117]
        result = adjust_difference(raw_ps, roll_dates, [c1, c2])

        diff = 107.0 - 100.0  # = 7.0
        np.testing.assert_allclose(result.close[:3], [95.0 + diff, 98.0 + diff, 100.0 + diff])
        np.testing.assert_allclose(result.close[3:], [107.0, 110.0])
        np.testing.assert_array_equal(result.volume, raw_ps.volume)


# ── Stitcher / Builder tests ───────────────────────────────────────


class TestContinuousSeriesBuilder:
    def setup_method(self):
        self.builder = ContinuousSeriesBuilder()

    def test_single_contract(self):
        """Single contract returns unchanged, no roll dates."""
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240101, 20240102, 20240103],
            [20.0, 21.0, 22.0],
        )
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c1], config)

        assert result.roll_dates == ()
        assert result.contracts == ("VXF24",)
        np.testing.assert_array_equal(result.prices.close, [20.0, 21.0, 22.0])

    def test_two_contracts_no_adjustment(self):
        """Raw concatenation with no adjustment, verify roll date."""
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240112, 20240115],
            [20.0, 20.5, 21.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240116, 20240117, 20240120],
            [22.0, 22.5, 23.0],
        )
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
        )
        result = self.builder.build([c1, c2], config)

        assert len(result.roll_dates) == 1
        assert result.contracts == ("VXF24", "VXG24")
        # 6 dates total, no overlap
        assert len(result.prices) == 6
        np.testing.assert_array_equal(
            result.prices.close, [20.0, 20.5, 21.0, 22.0, 22.5, 23.0]
        )

    def test_three_contracts_proportional_continuous_returns(self):
        """Three contracts with proportional adjustment: returns should be continuous.

        This is THE key validation from the architecture doc — no return spikes
        at roll boundaries.
        """
        # Design contracts so we know the exact adjustment factors:
        # c1: closes at 100 on expiration 20240115
        # c2: close=110 on 20240115, closes at 110 on expiration 20240215
        # c3: close=120 on 20240215
        #
        # Roll 1 ratio (c2/c1 at 20240115): 110/100 = 1.1
        # Roll 2 ratio (c3/c2 at 20240215): 120/110 = 12/11
        # Pre-roll-2 gets multiplied by 12/11
        # Pre-roll-1 gets multiplied by 12/11 * 1.1 = 1.2

        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240112, 20240115],
            [90.0, 95.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240115, 20240117, 20240212, 20240215],
            [110.0, 112.0, 108.0, 110.0],
        )
        c3 = _make_contract(
            "VXH24", 20240315,
            [20240215, 20240218, 20240220],
            [120.0, 122.0, 125.0],
        )

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
        )
        result = self.builder.build([c1, c2, c3], config)

        assert len(result.roll_dates) == 2
        assert result.contracts == ("VXF24", "VXG24", "VXH24")

        # Compute daily returns of the adjusted close series
        closes = result.prices.close
        returns = np.diff(closes) / closes[:-1]

        # Also compute the "expected" returns from raw contract data within
        # each segment (no adjustment needed within a segment).
        # The key check: returns at roll boundaries should NOT be spikes.
        # Specifically, the return across the roll boundary should reflect
        # the actual price movement of the NEW contract, not the gap.

        # Find roll boundary indices in the result
        roll_date_set = set(result.roll_dates)
        dates = result.prices.dates

        # Returns should be finite and not contain NaN
        assert np.all(np.isfinite(returns))

        # Verify no return exceeds a reasonable threshold (raw gap would be ~10%)
        # With proper adjustment, the max return should be much smaller
        # The largest raw within-contract daily move is about 5/95 ~ 5.3%
        # Without adjustment, the roll gap would cause a 10%+ return
        assert np.all(np.abs(returns) < 0.10), (
            f"Return spike detected at roll boundary: {returns}"
        )

    def test_two_contracts_difference(self):
        """Two contracts with difference adjustment: dollar diffs preserved."""
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240112, 20240115],
            [95.0, 98.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240115, 20240117, 20240120],
            [105.0, 107.0, 110.0],
        )

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.DIFFERENCE,
        )
        result = self.builder.build([c1, c2], config)

        # Diff at roll = 105 - 100 = 5
        # Pre-roll closes should be shifted up by 5
        closes = result.prices.close

        # Find the boundary
        assert len(result.roll_dates) == 1

        # Dollar changes within segments should be preserved
        # c1 segment: 95->98->100, diffs = +3, +2
        # After +5 shift: 100->103->105
        # c2 segment: 107->110, diff = +3
        # Cross-boundary: 105->107, diff = +2

        # The key property: dollar differences within each segment are preserved
        pre_roll = closes[:3]
        np.testing.assert_allclose(pre_roll, [100.0, 103.0, 105.0])
        post_roll = closes[3:]
        np.testing.assert_allclose(post_roll, [107.0, 110.0])

    def test_zero_close_stripped(self):
        """Contracts with close=0 rows are cleaned before stitching."""
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240111, 20240112, 20240115],
            [20.0, 0.0, 0.0, 21.0],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240116, 20240117],
            [22.0, 23.0],
        )
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c1, c2], config)

        # c1 should have zero-close rows removed: only 20240110, 20240115
        assert len(result.prices) == 4  # 2 from c1 + 2 from c2
        # No zeros in the output
        assert np.all(result.prices.close != 0.0)

    def test_empty_contracts_filtered(self):
        """Empty/no-data contracts are skipped."""
        c_empty = _make_empty_contract("VXF24", 20240115)
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240116, 20240117],
            [22.0, 23.0],
        )
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c_empty, c2], config)

        assert result.contracts == ("VXG24",)
        assert result.roll_dates == ()
        assert len(result.prices) == 2

    def test_all_empty_contracts(self):
        """All empty contracts produce empty series."""
        c1 = _make_empty_contract("VXF24", 20240115)
        c2 = _make_empty_contract("VXG24", 20240215)
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c1, c2], config)

        assert result.contracts == ()
        assert result.roll_dates == ()
        assert len(result.prices) == 0

    def test_overlap_trimmed_correctly(self):
        """Overlapping date ranges handled: earlier contract trimmed at roll."""
        # Both contracts have data on 20240113-20240115
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240110, 20240112, 20240113, 20240114, 20240115, 20240116, 20240117],
            [18.0, 19.0, 19.5, 20.0, 20.5, 21.0, 21.5],
        )
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240113, 20240114, 20240115, 20240116, 20240117, 20240120],
            [22.0, 22.5, 23.0, 23.5, 24.0, 25.0],
        )
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
        )
        result = self.builder.build([c1, c2], config)

        # c1 trimmed to dates <= 20240115
        # c2 keeps all dates
        # Overlap dates (20240113-20240115): c2 (later contract) wins in dedup
        # Final: c1's unique dates + all of c2
        dates = list(result.prices.dates)
        # c1 unique: 20240110, 20240112 (20240113-20240115 overlap with c2, c2 wins)
        # c2: 20240113, 20240114, 20240115, 20240116, 20240117, 20240120
        assert 20240110 in dates
        assert 20240112 in dates
        # On overlap dates, c2's prices should be used
        idx_113 = dates.index(20240113)
        assert result.prices.close[idx_113] == 22.0  # c2's price, not c1's 19.5

    def test_proportional_cascading_three_rolls(self):
        """Verify backward cascading: adjustment at roll 1 includes roll 2's factor."""
        # 3 contracts, 2 rolls
        # Roll 2: ratio_2 = c3_close / c2_close at c2 expiration
        # Roll 1: ratio_1 = c2_close / c1_close at c1 expiration
        # Pre-roll-1 prices get multiplied by ratio_1 * ratio_2 (backward cascade)

        c1 = _make_contract("A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 50.0])
        c2 = _make_contract("B", 20240120, [20240110, 20240115, 20240120], [60.0, 62.0, 60.0])
        c3 = _make_contract("C", 20240130, [20240120, 20240125, 20240130], [72.0, 75.0, 78.0])

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
        )
        result = self.builder.build([c1, c2, c3], config)

        # Roll 2 (at c2 exp 20240120): ratio_2 = 72/60 = 1.2
        # Roll 1 (at c1 exp 20240110): ratio_1 = 60/50 = 1.2
        # Segment c1 dates: before roll_date_1 (first date of c2 segment)
        # Segment c2 dates: between roll boundaries
        # Segment c3 dates: after last roll boundary

        # c2 segment gets ratio_2 = 1.2
        # c1 segment gets ratio_1 * ratio_2 = 1.2 * 1.2 = 1.44
        # (because backward processing: ratio_2 applied first to c1+c2, then ratio_1 to c1)
        # Wait — let me re-derive:
        # Backward: process roll 2 first, then roll 1.
        # Roll 2: multiply everything before roll_date_2 by ratio_2=1.2
        #   → c1 prices *= 1.2, c2 prices (before roll_date_2) *= 1.2
        # Roll 1: multiply everything before roll_date_1 by ratio_1=1.2
        #   → c1 prices *= 1.2 (they're already * 1.2, so now * 1.44)

        closes = result.prices.close

        # c3 segment (unchanged): 75, 78  (20240125, 20240130)
        # (20240120 is deduped — c3 wins but trimmed c2 also has it)
        # Actually let's just check the first segment's values
        # c1 original: [50, 52, 50] → dates [20240105, 20240108, 20240110]
        # c1 after roll2 (1.2): [60, 62.4, 60]
        # c1 after roll1 (1.2): [72, 74.88, 72]

        # But the roll_date used in adjustment is the first date of the new segment,
        # not the expiration. Let me think about what dates end up in which segment...
        # After trim: c1 keeps dates <= 20240110, c2 keeps dates <= 20240120, c3 keeps all
        # Concatenation + dedup (later wins):
        #   20240105(c1), 20240108(c1), 20240110(c2 wins over c1), 20240115(c2), 20240120(c3 wins), 20240125(c3), 20240130(c3)
        # Roll dates (first date of new segment):
        #   c1->c2 boundary: 20240110 (where c2 takes over)
        #   c2->c3 boundary: 20240120 (where c3 takes over)

        # Adjustment uses these roll_dates:
        # Roll at 20240120: ratio = c3_close(at 20240120)/c2_close(at 20240120) = 72/60 = 1.2
        #   Multiply dates < 20240120 by 1.2
        # Roll at 20240110: ratio = c2_close(at 20240110)/c1_close(at 20240110) = 60/50 = 1.2
        #   Multiply dates < 20240110 by 1.2

        # Final close values:
        # 20240105: 50 * 1.2 (roll2) * 1.2 (roll1) = 72.0
        # 20240108: 52 * 1.2 * 1.2 = 74.88
        # 20240110: 60 * 1.2 (roll2 only, date >= roll1_date) = 72.0
        # 20240115: 62 * 1.2 = 74.4
        # 20240120: 72 (no adjustment)
        # 20240125: 75
        # 20240130: 78

        expected_closes = [72.0, 74.88, 72.0, 74.4, 72.0, 75.0, 78.0]
        np.testing.assert_allclose(closes, expected_closes, rtol=1e-10)

    def test_intermediate_contract_dropped_by_trim(self):
        """When an intermediate contract is all-zero, trim drops it.

        Adjustment must still work correctly: the roll_dates from
        concatenation align with the surviving (trimmed) contracts,
        not the original list.
        """
        c1 = _make_contract(
            "VXF24", 20240115,
            [20240105, 20240108, 20240110],
            [50.0, 52.0, 50.0],
        )
        # c2 has ONLY zero-close rows within its trim window (dates <= 20240215)
        c2 = _make_contract(
            "VXG24", 20240215,
            [20240116, 20240120, 20240210],
            [0.0, 0.0, 0.0],
        )
        c3 = _make_contract(
            "VXH24", 20240315,
            [20240216, 20240220, 20240301],
            [72.0, 75.0, 78.0],
        )

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
        )
        result = self.builder.build([c1, c2, c3], config)

        # c2 is dropped (all zeros), so we get c1 → c3 with 1 roll
        assert result.contracts == ("VXF24", "VXH24")
        assert len(result.roll_dates) == 1

        # Adjustment should use c1 and c3 prices at the roll boundary
        # No IndexError or misalignment
        closes = result.prices.close
        assert np.all(np.isfinite(closes))
        assert len(closes) == 6  # 3 from c1 + 3 from c3

    def test_collection_passed_through(self):
        """Collection name is preserved in output."""
        c1 = _make_contract("VXF24", 20240115, [20240101], [20.0])
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c1], config, collection="vix_futures")
        assert result.collection == "vix_futures"

    def test_config_passed_through(self):
        """Roll config is preserved in output."""
        c1 = _make_contract("VXF24", 20240115, [20240101], [20.0])
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
            cycle="HMUZ",
        )
        result = self.builder.build([c1], config)
        assert result.roll_config == config

    def test_dedup_subsumes_entire_contract(self):
        """When dedup eliminates all rows from a contract, adjustment still works.

        Contract A: dates [10, 15], expires 15
        Contract B: dates [10, 15, 20], expires 20
        After trim: A keeps [10, 15], B keeps all.
        After dedup (later contract wins): B's data for [10, 15, 20].
        Contract A is fully subsumed — 0 roll transitions for that boundary.
        """
        c1 = _make_contract("VXF24", 20240115, [20240110, 20240115], [20.0, 21.0])
        c2 = _make_contract("VXG24", 20240120, [20240110, 20240115, 20240120], [22.0, 23.0, 24.0])
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
        )
        result = self.builder.build([c1, c2], config)

        # c1 is entirely subsumed by c2 in dedup — only c2 survives
        assert result.contracts == ("VXG24",)
        assert len(result.roll_dates) == 0
        assert len(result.prices) == 3
        # Should be c2's raw prices (no adjustment needed, no rolls)
        np.testing.assert_array_equal(result.prices.close, [22.0, 23.0, 24.0])

    def test_dedup_subsumes_middle_contract(self):
        """Middle contract subsumed, only first and last survive."""
        c1 = _make_contract("VXF24", 20240110, [20240101, 20240105], [10.0, 11.0])
        # c2's dates are all within c3's range, and c3 is later → c2 subsumed
        c2 = _make_contract("VXG24", 20240115, [20240110, 20240115], [20.0, 21.0])
        c3 = _make_contract("VXH24", 20240120, [20240110, 20240115, 20240120], [30.0, 31.0, 32.0])
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
        )
        result = self.builder.build([c1, c2, c3], config)

        # c2 subsumed by c3 → surviving are c1 and c3
        assert result.contracts == ("VXF24", "VXH24")
        assert len(result.roll_dates) == 1
        # Prices should be finite (no assertion crash)
        assert np.all(np.isfinite(result.prices.close))


class TestFindClosestDateIdx:
    """Edge case tests for _find_closest_date_idx."""

    def test_single_element(self):
        dates = np.array([20240101], dtype=np.int64)
        assert _find_closest_date_idx(dates, 20240101) == 0
        assert _find_closest_date_idx(dates, 20231201) == 0
        assert _find_closest_date_idx(dates, 20240201) == 0

    def test_target_before_all(self):
        dates = np.array([20240110, 20240120, 20240130], dtype=np.int64)
        assert _find_closest_date_idx(dates, 20240101) == 0

    def test_target_after_all(self):
        dates = np.array([20240110, 20240120, 20240130], dtype=np.int64)
        assert _find_closest_date_idx(dates, 20240201) == 2

    def test_exact_match(self):
        dates = np.array([20240110, 20240120, 20240130], dtype=np.int64)
        assert _find_closest_date_idx(dates, 20240120) == 1

    def test_equidistant_favors_later(self):
        """When equidistant, <= in the comparison favors the later date."""
        dates = np.array([20240110, 20240120], dtype=np.int64)
        # 20240115 is equidistant between 10 and 20
        assert _find_closest_date_idx(dates, 20240115) == 1

    def test_closer_to_earlier(self):
        dates = np.array([20240110, 20240120], dtype=np.int64)
        assert _find_closest_date_idx(dates, 20240112) == 0

    def test_closer_to_later(self):
        dates = np.array([20240110, 20240120], dtype=np.int64)
        assert _find_closest_date_idx(dates, 20240118) == 1


class TestGetCloseAtRoll:
    """Edge case tests for _get_close_at_roll."""

    def test_empty_contract_returns_zero(self):
        """Empty contract should return 0.0, not crash."""
        contract = ContractPriceData(
            contract_id="VXF24",
            expiration=20240115,
            prices=PriceSeries.empty(),
        )
        assert _get_close_at_roll(contract, 20240115) == 0.0


class TestNewCloseZero:
    """Verify adjustment handles new_close == 0 gracefully."""

    def test_proportional_new_close_zero_skips_roll(self):
        """Proportional adjustment skips roll when new_close == 0."""
        # Contract A: closes [100, 110]
        # Contract B: close on roll date is 0 (shouldn't zero out history)
        c1 = _make_contract("A", 20240110, [20240101, 20240110], [100.0, 110.0])
        c2 = _make_contract("B", 20240120, [20240110, 20240120], [0.0, 50.0])

        # Use stitcher to get raw series, then test adjustment directly
        prices = PriceSeries(
            dates=np.array([20240101, 20240110, 20240120], dtype=np.int64),
            open=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            high=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            low=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            close=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            volume=np.array([1000.0, 1000.0, 1000.0], dtype=np.float64),
        )
        result = adjust_proportional(prices, [20240110], [c1, c2])

        # Roll should be skipped (new_close=0), so prices unchanged
        np.testing.assert_array_equal(result.close, [100.0, 110.0, 50.0])

    def test_difference_new_close_zero_still_adjusts(self):
        """Difference adjustment works with new_close == 0 (diff is just -old_close)."""
        c1 = _make_contract("A", 20240110, [20240101, 20240110], [100.0, 110.0])
        c2 = _make_contract("B", 20240120, [20240110, 20240120], [0.0, 50.0])

        prices = PriceSeries(
            dates=np.array([20240101, 20240110, 20240120], dtype=np.int64),
            open=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            high=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            low=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            close=np.array([100.0, 110.0, 50.0], dtype=np.float64),
            volume=np.array([1000.0, 1000.0, 1000.0], dtype=np.float64),
        )
        # Difference: diff = 0 - 110 = -110, applied to dates < 20240110
        result = adjust_difference(prices, [20240110], [c1, c2])
        np.testing.assert_allclose(result.close[0], 100.0 - 110.0)  # -10.0


# ── Additional coverage tests ─────────────────────────────────────


class TestUnsortedContractsRejected:
    """Contracts not sorted by expiration must raise ValueError."""

    def test_reverse_order_raises(self):
        c1 = _make_contract("A", 20240215, [20240201], [100.0])
        c2 = _make_contract("B", 20240115, [20240101], [90.0])
        builder = ContinuousSeriesBuilder()
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        with pytest.raises(ValueError, match="not sorted by expiration"):
            builder.build([c1, c2], config)

    def test_duplicate_expiration_ok(self):
        """Two contracts with same expiration should not raise."""
        c1 = _make_contract("A", 20240115, [20240101], [100.0])
        c2 = _make_contract("B", 20240115, [20240110], [105.0])
        builder = ContinuousSeriesBuilder()
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        # Should not raise — equal expirations are fine
        builder.build([c1, c2], config)


class TestDifferenceCascadingThreeRolls:
    """Mirror of test_proportional_cascading_three_rolls for difference adjustment."""

    def test_cascading_three_rolls(self):
        c1 = _make_contract("A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 50.0])
        c2 = _make_contract("B", 20240120, [20240110, 20240115, 20240120], [60.0, 62.0, 60.0])
        c3 = _make_contract("C", 20240130, [20240120, 20240125, 20240130], [72.0, 75.0, 78.0])

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.DIFFERENCE,
        )
        builder = ContinuousSeriesBuilder()
        result = builder.build([c1, c2, c3], config)

        # After dedup: c1=[20240105,20240108], c2=[20240110,20240115], c3=[20240120,20240125,20240130]
        # Roll dates: [20240110, 20240120]
        # Roll 2 (at 20240120): diff = c3_close(72) - c2_close(60) = +12
        #   dates < 20240120 shifted by +12
        # Roll 1 (at 20240110): diff = c2_close(60) - c1_close(50) = +10
        #   dates < 20240110 shifted by +10
        #
        # Final:
        #   20240105: 50 + 12 + 10 = 72
        #   20240108: 52 + 12 + 10 = 74
        #   20240110: 60 + 12 = 72
        #   20240115: 62 + 12 = 74
        #   20240120: 72 (unchanged)
        #   20240125: 75
        #   20240130: 78

        expected = [72.0, 74.0, 72.0, 74.0, 72.0, 75.0, 78.0]
        np.testing.assert_allclose(result.prices.close, expected, rtol=1e-10)

        # Dollar differences within each segment should be preserved
        closes = result.prices.close
        assert np.isclose(closes[1] - closes[0], 2.0)  # c1: 52-50
        assert np.isclose(closes[3] - closes[2], 2.0)  # c2: 62-60
        assert np.isclose(closes[5] - closes[4], 3.0)  # c3: 75-72


class TestDateGapBetweenContracts:
    """When contracts have a calendar gap (no overlap), stitching still works."""

    def test_gap_between_contracts(self):
        # c1 ends 20240110, c2 starts 20240120 — 10-day gap
        c1 = _make_contract("A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 55.0])
        c2 = _make_contract("B", 20240130, [20240120, 20240125, 20240130], [60.0, 62.0, 65.0])

        builder = ContinuousSeriesBuilder()
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
        )
        result = builder.build([c1, c2], config)

        # Both contracts contribute all their data, gap preserved
        assert len(result.prices) == 6
        assert result.roll_dates == (20240120,)
        np.testing.assert_array_equal(
            result.prices.close, [50.0, 52.0, 55.0, 60.0, 62.0, 65.0]
        )

    def test_gap_proportional_adjustment(self):
        """Proportional adjustment across a date gap uses closest-date matching."""
        c1 = _make_contract("A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 55.0])
        c2 = _make_contract("B", 20240130, [20240120, 20240125, 20240130], [60.0, 62.0, 65.0])

        builder = ContinuousSeriesBuilder()
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
        )
        result = builder.build([c1, c2], config)

        # Roll date = 20240120 (first date of c2 segment)
        # _get_close_at_roll(c1, 20240120) → closest date in c1 = 20240110 → close=55
        # _get_close_at_roll(c2, 20240120) → exact match → close=60
        # Ratio = 60/55 = 12/11
        ratio = 60.0 / 55.0
        np.testing.assert_allclose(result.prices.close[:3], [50.0 * ratio, 52.0 * ratio, 55.0 * ratio])
        np.testing.assert_allclose(result.prices.close[3:], [60.0, 62.0, 65.0])


class TestManyRolls:
    """Test with 10+ rolls to verify accumulation and performance."""

    def test_ten_rolls_proportional(self):
        """10 contracts with proportional adjustment: no NaN, finite results."""
        contracts = []
        for i in range(10):
            base_date = 20240101 + i * 100  # Spread contracts across months
            exp_date = base_date + 50
            dates = [base_date + d for d in range(0, 40, 5)]
            base_price = 100.0 + i * 5
            closes = [base_price + j * 0.5 for j in range(len(dates))]
            contracts.append(_make_contract(f"C{i}", exp_date, dates, closes))

        builder = ContinuousSeriesBuilder()
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.PROPORTIONAL,
        )
        result = builder.build(contracts, config)

        assert len(result.roll_dates) > 0
        assert np.all(np.isfinite(result.prices.close))
        assert np.all(result.prices.close > 0)  # No negative prices from proportional

    def test_ten_rolls_difference(self):
        """10 contracts with difference adjustment: no NaN, finite results."""
        contracts = []
        for i in range(10):
            base_date = 20240101 + i * 100
            exp_date = base_date + 50
            dates = [base_date + d for d in range(0, 40, 5)]
            base_price = 100.0 + i * 5
            closes = [base_price + j * 0.5 for j in range(len(dates))]
            contracts.append(_make_contract(f"C{i}", exp_date, dates, closes))

        builder = ContinuousSeriesBuilder()
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.DIFFERENCE,
        )
        result = builder.build(contracts, config)

        assert len(result.roll_dates) > 0
        assert np.all(np.isfinite(result.prices.close))
