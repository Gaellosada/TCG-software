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
    adjust_ratio,
    adjust_difference,
    _find_closest_date_idx,
    _get_close_at_roll,
    _shared_close_at_roll,
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
            volume=np.array(volumes, dtype=np.float64)
            if volumes
            else np.full(n, 1000.0),
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
            "VXF24",
            20240115,
            [20240110, 20240112, 20240115, 20240117, 20240120],
            [20.0, 20.5, 21.0, 21.5, 22.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
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
            "VXF24",
            20240115,
            [20240110, 20240111, 20240112],
            [20.0, 0.0, 21.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
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


class TestAdjustRatio:
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
        result = adjust_ratio(ps, [], [])
        np.testing.assert_array_equal(result.close, ps.close)

    def test_single_roll_ratio(self):
        """Two contracts: verify ratio applied to pre-roll prices.

        DATE-MISMATCH FIX: the gap must come from a single SHARED trading day,
        not from the new contract at the roll date vs the old contract at a
        different (nearest/trimmed) date. Here both contracts quote on the
        shared day 20240115 (old=100, new=105), even though the roll date passed
        by the stitcher is 20240117 (a new-only forward date). Correct ratio is
        therefore 105/100 = 1.05, NOT the old cross-date 107/100 = 1.07.
        """
        # Shared day 20240115: c1 close=100, c2 close=105 → ratio = 1.05.
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240110, 20240112, 20240115],
            [95.0, 98.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
            [20240115, 20240117, 20240120],
            [105.0, 107.0, 110.0],
        )

        # Raw concatenated series (after trim: c1 up to 20240115, c2 from 20240117)
        raw_ps = PriceSeries(
            dates=np.array(
                [20240110, 20240112, 20240115, 20240117, 20240120], dtype=np.int64
            ),
            open=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            high=np.array([96.0, 99.0, 101.0, 108.0, 111.0]),
            low=np.array([94.0, 97.0, 99.0, 106.0, 109.0]),
            close=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            volume=np.array([1000.0, 1000.0, 1000.0, 1000.0, 1000.0]),
        )

        # Roll date = first date of new contract segment
        roll_dates = [20240117]

        result = adjust_ratio(raw_ps, roll_dates, [c1, c2])

        ratio = 105.0 / 100.0  # = 1.05 (shared-day 20240115 gap, NOT 107/100)
        # Dates before 20240117 should be multiplied by ratio
        np.testing.assert_allclose(
            result.close[:3], [95.0 * ratio, 98.0 * ratio, 100.0 * ratio]
        )
        # Dates from 20240117 onward: unchanged
        np.testing.assert_allclose(result.close[3:], [107.0, 110.0])
        # Volume unchanged
        np.testing.assert_array_equal(result.volume, raw_ps.volume)

    def test_volume_unchanged(self):
        """Ratio adjustment must not modify volume."""
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
        result = adjust_ratio(raw_ps, [20240120], [c1, c2])
        np.testing.assert_array_equal(result.volume, [500.0, 600.0, 700.0])


class TestAdjustDifference:
    def test_single_roll_difference(self):
        """Two contracts: verify additive adjustment applied to pre-roll prices.

        DATE-MISMATCH FIX: gap from the shared day 20240115 (old=100, new=105),
        so diff = 105 - 100 = +5, NOT the old cross-date 107 - 100 = +7.
        """
        # Shared day 20240115: c1 close=100, c2 close=105 → diff = +5.
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240110, 20240112, 20240115],
            [95.0, 98.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
            [20240115, 20240117, 20240120],
            [105.0, 107.0, 110.0],
        )

        raw_ps = PriceSeries(
            dates=np.array(
                [20240110, 20240112, 20240115, 20240117, 20240120], dtype=np.int64
            ),
            open=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            high=np.array([96.0, 99.0, 101.0, 108.0, 111.0]),
            low=np.array([94.0, 97.0, 99.0, 106.0, 109.0]),
            close=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            volume=np.array([1000.0, 1000.0, 1000.0, 1000.0, 1000.0]),
        )

        roll_dates = [20240117]
        result = adjust_difference(raw_ps, roll_dates, [c1, c2])

        diff = 105.0 - 100.0  # = 5.0 (shared-day 20240115 gap, NOT 107-100)
        np.testing.assert_allclose(
            result.close[:3], [95.0 + diff, 98.0 + diff, 100.0 + diff]
        )
        np.testing.assert_allclose(result.close[3:], [107.0, 110.0])
        np.testing.assert_array_equal(result.volume, raw_ps.volume)


# ── Stitcher / Builder tests ───────────────────────────────────────


class TestContinuousSeriesBuilder:
    def setup_method(self):
        self.builder = ContinuousSeriesBuilder()

    def test_single_contract(self):
        """Single contract returns unchanged, no roll dates."""
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240101, 20240102, 20240103],
            [20.0, 21.0, 22.0],
        )
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c1], config)

        assert result.roll_dates == ()
        assert result.contracts == ("VXF24",)
        np.testing.assert_array_equal(result.prices.close, [20.0, 21.0, 22.0])

    def test_single_contract_zero_close_stripped(self):
        """Single-contract path strips zero-close rows like multi-contract path."""
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240101, 20240102, 20240103, 20240104],
            [20.0, 0.0, 21.0, 22.0],
        )
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c1], config)

        assert result.roll_dates == ()
        assert result.contracts == ("VXF24",)
        np.testing.assert_array_equal(
            result.prices.dates, [20240101, 20240103, 20240104]
        )
        np.testing.assert_array_equal(result.prices.close, [20.0, 21.0, 22.0])

    def test_single_contract_all_zero_close_returns_empty(self):
        """Single contract with only zero-close rows yields empty series."""
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240101, 20240102],
            [0.0, 0.0],
        )
        config = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
        result = self.builder.build([c1], config)

        assert result.roll_dates == ()
        assert result.contracts == ()
        assert len(result.prices) == 0

    def test_two_contracts_no_adjustment(self):
        """Raw concatenation with no adjustment, verify roll date."""
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240110, 20240112, 20240115],
            [20.0, 20.5, 21.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
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

    def test_three_contracts_ratio_continuous_returns(self):
        """Three contracts with ratio adjustment: returns should be continuous.

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
            "VXF24",
            20240115,
            [20240110, 20240112, 20240115],
            [90.0, 95.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
            [20240115, 20240117, 20240212, 20240215],
            [110.0, 112.0, 108.0, 110.0],
        )
        c3 = _make_contract(
            "VXH24",
            20240315,
            [20240215, 20240218, 20240220],
            [120.0, 122.0, 125.0],
        )

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.RATIO,
        )
        result = self.builder.build([c1, c2, c3], config)

        assert len(result.roll_dates) == 2
        assert result.contracts == ("VXF24", "VXG24", "VXH24")

        # Compute daily returns of the adjusted close series
        closes = result.prices.close
        returns = np.diff(closes) / closes[:-1]

        # Returns should be finite and not contain NaN
        assert np.all(np.isfinite(returns))

        # TIGHTENED from a loose `abs(returns) < 0.10` band into an EXACT
        # continuity assertion (date-mismatch fix). The shared-day factors are
        # known exactly: roll 1 at 20240115 = 110/100 = 1.1 (both contracts
        # quote 20240115); roll 2 at 20240215 = 120/110 = 12/11. The c1 segment
        # (dates < 20240115) carries 1.1 * 12/11 = 1.2; the c2 segment
        # (20240115 <= d < 20240215) carries 12/11; the c3 segment is raw.
        # Any cross-date gap would shift these exact values.
        f2 = 12.0 / 11.0
        expected = np.array(
            [
                90.0 * 1.2,  # 20240110  (c1)
                95.0 * 1.2,  # 20240112  (c1)
                110.0 * f2,  # 20240115  (c2 wins dedup) -> 120.0
                112.0 * f2,  # 20240117  (c2)
                108.0 * f2,  # 20240212  (c2)
                120.0,  # 20240215  (c3 wins dedup over c2's 110, unadjusted)
                122.0,  # 20240218  (c3)
                125.0,  # 20240220  (c3)
            ]
        )
        np.testing.assert_allclose(closes, expected, atol=1e-9)
        # Cross-seam returns equal the NEW contract's own move on the shared day
        # (no artificial jump): at the 20240115 seam the adjusted return is
        # exactly (110*f2)/(95*1.2) - 1, and at 20240215 it is 110/(108*f2) - 1.
        # Spot-check the seam return is bounded by the within-contract band.
        assert np.max(np.abs(returns)) < 0.06, (
            f"Return spike at roll boundary: {returns}"
        )

    def test_two_contracts_difference(self):
        """Two contracts with difference adjustment: dollar diffs preserved."""
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240110, 20240112, 20240115],
            [95.0, 98.0, 100.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
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
            "VXF24",
            20240115,
            [20240110, 20240111, 20240112, 20240115],
            [20.0, 0.0, 0.0, 21.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
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
            "VXG24",
            20240215,
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
            "VXF24",
            20240115,
            [20240110, 20240112, 20240113, 20240114, 20240115, 20240116, 20240117],
            [18.0, 19.0, 19.5, 20.0, 20.5, 21.0, 21.5],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
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

    def test_ratio_cascading_three_rolls(self):
        """Verify backward cascading: adjustment at roll 1 includes roll 2's factor."""
        # 3 contracts, 2 rolls
        # Roll 2: ratio_2 = c3_close / c2_close at c2 expiration
        # Roll 1: ratio_1 = c2_close / c1_close at c1 expiration
        # Pre-roll-1 prices get multiplied by ratio_1 * ratio_2 (backward cascade)

        c1 = _make_contract(
            "A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 50.0]
        )
        c2 = _make_contract(
            "B", 20240120, [20240110, 20240115, 20240120], [60.0, 62.0, 60.0]
        )
        c3 = _make_contract(
            "C", 20240130, [20240120, 20240125, 20240130], [72.0, 75.0, 78.0]
        )

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.RATIO,
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
            "VXF24",
            20240115,
            [20240105, 20240108, 20240110],
            [50.0, 52.0, 50.0],
        )
        # c2 has ONLY zero-close rows within its trim window (dates <= 20240215)
        c2 = _make_contract(
            "VXG24",
            20240215,
            [20240116, 20240120, 20240210],
            [0.0, 0.0, 0.0],
        )
        c3 = _make_contract(
            "VXH24",
            20240315,
            [20240216, 20240220, 20240301],
            [72.0, 75.0, 78.0],
        )

        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.RATIO,
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
            adjustment=AdjustmentMethod.RATIO,
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
        c2 = _make_contract(
            "VXG24", 20240120, [20240110, 20240115, 20240120], [22.0, 23.0, 24.0]
        )
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.RATIO,
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
        c3 = _make_contract(
            "VXH24", 20240120, [20240110, 20240115, 20240120], [30.0, 31.0, 32.0]
        )
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.RATIO,
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

    def test_ratio_new_close_zero_skips_roll(self):
        """Ratio adjustment skips roll when new_close == 0."""
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
        result = adjust_ratio(prices, [20240110], [c1, c2])

        # Roll should be skipped (new_close=0), so prices unchanged
        np.testing.assert_array_equal(result.close, [100.0, 110.0, 50.0])

    def test_difference_new_close_zero_skips_roll(self):
        """Difference adjustment now SKIPS a roll when a reference close is 0.

        ZERO-GUARD FIX (symmetry with ratio): previously difference had NO zero
        guard, so a degenerate new_close==0 produced diff = 0 - old_close =
        -old_close and shifted ALL prior history down by a full contract price
        (the old assertion expected close[0] == 100 - 110 = -10). A zero close
        is data that should have been stripped, not a real $0 quote; treating it
        as a real gap corrupts the series. The guard now skips the roll and
        leaves the prior history unchanged.
        """
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
        # Shared day 20240110: old=110, new=0 → guard trips → roll skipped.
        result = adjust_difference(prices, [20240110], [c1, c2])
        np.testing.assert_array_equal(result.close, [100.0, 110.0, 50.0])


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
    """Mirror of test_ratio_cascading_three_rolls for difference adjustment."""

    def test_cascading_three_rolls(self):
        c1 = _make_contract(
            "A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 50.0]
        )
        c2 = _make_contract(
            "B", 20240120, [20240110, 20240115, 20240120], [60.0, 62.0, 60.0]
        )
        c3 = _make_contract(
            "C", 20240130, [20240120, 20240125, 20240130], [72.0, 75.0, 78.0]
        )

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
        c1 = _make_contract(
            "A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 55.0]
        )
        c2 = _make_contract(
            "B", 20240130, [20240120, 20240125, 20240130], [60.0, 62.0, 65.0]
        )

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

    def test_gap_ratio_adjustment(self):
        """Ratio adjustment across a date gap uses closest-date matching."""
        c1 = _make_contract(
            "A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 55.0]
        )
        c2 = _make_contract(
            "B", 20240130, [20240120, 20240125, 20240130], [60.0, 62.0, 65.0]
        )

        builder = ContinuousSeriesBuilder()
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.RATIO,
        )
        result = builder.build([c1, c2], config)

        # Roll date = 20240120 (first date of c2 segment)
        # _get_close_at_roll(c1, 20240120) → closest date in c1 = 20240110 → close=55
        # _get_close_at_roll(c2, 20240120) → exact match → close=60
        # Ratio = 60/55 = 12/11
        ratio = 60.0 / 55.0
        np.testing.assert_allclose(
            result.prices.close[:3], [50.0 * ratio, 52.0 * ratio, 55.0 * ratio]
        )
        np.testing.assert_allclose(result.prices.close[3:], [60.0, 62.0, 65.0])


class TestManyRolls:
    """Test with 10+ rolls to verify accumulation and performance."""

    def test_ten_rolls_ratio(self):
        """10 contracts with ratio adjustment: no NaN, finite results."""
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
            adjustment=AdjustmentMethod.RATIO,
        )
        result = builder.build(contracts, config)

        assert len(result.roll_dates) > 0
        assert np.all(np.isfinite(result.prices.close))
        assert np.all(result.prices.close > 0)  # No negative prices from ratio

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


# ── Roll offset tests ────────────────────────────────────────────────


class TestComputeRollDatesWithOffset:
    """Tests for roll_offset_days parameter in compute_roll_dates."""

    def test_offset_zero_same_as_default(self):
        """offset=0 must produce identical results to the no-offset call."""
        c1 = _make_contract("VXF24", 20240115, [20240101, 20240102], [20.0, 21.0])
        c2 = _make_contract("VXG24", 20240215, [20240116, 20240117], [22.0, 23.0])
        c3 = _make_contract("VXH24", 20240315, [20240216, 20240217], [24.0, 25.0])

        default_result = compute_roll_dates([c1, c2, c3], RollStrategy.FRONT_MONTH)
        offset_0_result = compute_roll_dates(
            [c1, c2, c3], RollStrategy.FRONT_MONTH, roll_offset_days=0
        )

        assert default_result == offset_0_result
        assert offset_0_result == [20240115, 20240215]

    def test_offset_2_shifts_dates(self):
        """offset=2 subtracts 2 calendar days from each expiration."""
        c1 = _make_contract("VXF24", 20240115, [20240101, 20240102], [20.0, 21.0])
        c2 = _make_contract("VXG24", 20240215, [20240116, 20240117], [22.0, 23.0])

        result = compute_roll_dates(
            [c1, c2], RollStrategy.FRONT_MONTH, roll_offset_days=2
        )
        # 20240115 - 2 days = 20240113
        assert result == [20240113]

    def test_offset_crosses_month_boundary(self):
        """offset that crosses a month boundary: exp 20240301 - 2 days = 20240228."""
        c1 = _make_contract("VXH24", 20240301, [20240220, 20240225], [30.0, 31.0])
        c2 = _make_contract("VXJ24", 20240401, [20240302, 20240305], [32.0, 33.0])

        result = compute_roll_dates(
            [c1, c2], RollStrategy.FRONT_MONTH, roll_offset_days=2
        )
        # 2024 is a leap year, so 20240301 - 2 = 20240228
        assert result == [20240228]

    def test_offset_crosses_month_boundary_non_leap(self):
        """Non-leap year: exp 20230301 - 2 days = 20230227."""
        c1 = _make_contract("VXH23", 20230301, [20230220, 20230225], [30.0, 31.0])
        c2 = _make_contract("VXJ23", 20230401, [20230302, 20230305], [32.0, 33.0])

        result = compute_roll_dates(
            [c1, c2], RollStrategy.FRONT_MONTH, roll_offset_days=2
        )
        # 2023 is NOT a leap year: 20230301 - 2 = 20230227
        assert result == [20230227]

    def test_offset_single_contract(self):
        """Single contract returns empty list regardless of offset."""
        c1 = _make_contract("VXF24", 20240115, [20240101], [20.0])
        result = compute_roll_dates([c1], RollStrategy.FRONT_MONTH, roll_offset_days=5)
        assert result == []

    def test_offset_three_contracts(self):
        """Three contracts, offset=3: each expiration shifted by 3 days."""
        c1 = _make_contract("A", 20240115, [20240105], [50.0])
        c2 = _make_contract("B", 20240215, [20240116], [60.0])
        c3 = _make_contract("C", 20240315, [20240216], [70.0])

        result = compute_roll_dates(
            [c1, c2, c3], RollStrategy.FRONT_MONTH, roll_offset_days=3
        )
        # 20240115 - 3 = 20240112, 20240215 - 3 = 20240212
        assert result == [20240112, 20240212]

    def test_offset_through_builder(self):
        """End-to-end: offset flows through ContinuousSeriesBuilder correctly."""
        c1 = _make_contract(
            "VXF24",
            20240115,
            [20240110, 20240112, 20240113, 20240114, 20240115],
            [20.0, 20.5, 21.0, 21.5, 22.0],
        )
        c2 = _make_contract(
            "VXG24",
            20240215,
            [20240114, 20240115, 20240116, 20240117],
            [23.0, 23.5, 24.0, 24.5],
        )

        builder = ContinuousSeriesBuilder()

        # Without offset: roll at 20240115 (c1 keeps dates <= 20240115)
        config_0 = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
            roll_offset_days=0,
        )
        result_0 = builder.build([c1, c2], config_0)

        # With offset=2: roll at 20240113 (c1 keeps dates <= 20240113)
        config_2 = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
            roll_offset_days=2,
        )
        result_2 = builder.build([c1, c2], config_2)

        # With offset, c1 loses more dates (trimmed earlier)
        # result_0 has c1 dates through 20240115; result_2 has c1 dates through 20240113
        dates_0 = set(result_0.prices.dates.tolist())
        dates_2 = set(result_2.prices.dates.tolist())

        # Both should contain c1 early dates
        assert 20240110 in dates_0
        assert 20240110 in dates_2

        # offset=0: c1 has data up to 20240115
        assert 20240115 in dates_0
        # offset=2: c1 trimmed at 20240113, so 20240114 and 20240115 come from c2 only
        assert 20240113 in dates_2


# ── Shared-date gap (date-mismatch bugfix) ─────────────────────────────


class TestSharedCloseAtRoll:
    """Unit tests for _shared_close_at_roll — the contemporaneous gap helper."""

    def test_roll_date_is_shared(self):
        """When the roll date itself is in both contracts, use it directly."""
        old = _make_contract(
            "A", 20240115, [20240110, 20240113, 20240115], [90.0, 95.0, 100.0]
        )
        new = _make_contract("B", 20240215, [20240115, 20240117], [105.0, 107.0])
        result = _shared_close_at_roll(old, new, 20240115)
        assert result == (100.0, 105.0)

    def test_picks_latest_shared_before_roll(self):
        """rd not shared, but an earlier shared day exists → use the LATEST such day."""
        # Both quote on 20240113 and 20240115; rd=20240117 (a new-only forward date).
        old = _make_contract(
            "A", 20240115, [20240110, 20240113, 20240115], [90.0, 95.0, 100.0]
        )
        new = _make_contract(
            "B", 20240215, [20240113, 20240115, 20240117], [103.0, 105.0, 107.0]
        )
        result = _shared_close_at_roll(old, new, 20240117)
        # Latest shared day <= 20240117 is 20240115 → (old=100, new=105), NOT (95,103) or cross-date.
        assert result == (100.0, 105.0)

    def test_no_shared_date_returns_none(self):
        """Pure abutment / disjoint dates → None (caller falls back + warns)."""
        old = _make_contract(
            "A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 55.0]
        )
        new = _make_contract(
            "B", 20240130, [20240120, 20240125, 20240130], [60.0, 62.0, 65.0]
        )
        assert _shared_close_at_roll(old, new, 20240120) is None

    def test_shared_only_after_roll_returns_none(self):
        """Shared dates exist but all are strictly AFTER the roll date → None."""
        old = _make_contract("A", 20240115, [20240110, 20240120], [90.0, 95.0])
        new = _make_contract("B", 20240215, [20240120, 20240125], [105.0, 107.0])
        # The only shared day (20240120) is > rd=20240117, so no eligible day at/before roll.
        assert _shared_close_at_roll(old, new, 20240117) is None

    def test_empty_contract_returns_none(self):
        old = ContractPriceData("A", 20240115, PriceSeries.empty())
        new = _make_contract("B", 20240215, [20240115], [105.0])
        assert _shared_close_at_roll(old, new, 20240115) is None


class TestNoJumpAtSeam:
    """THE property that catches the date-mismatch bug.

    The roll gap must be computed from BOTH contracts' closes on a single
    SHARED trading day. With the old (buggy) code the gap mixed the new
    contract's close at the roll date with the old contract's close at a
    DIFFERENT (trimmed-expiration / nearest) date, leaving a residual
    artificial jump at the seam. After adjustment the cumulative factor on the
    old segment must EXACTLY equal the true contemporaneous gap on the shared
    day, i.e. adjusted_old(shared_day) == new_close(shared_day).
    """

    # Canonical single-roll fixture. Old and new both quote on the shared day
    # 20240115 (old=100, new=105). The roll date passed by the stitcher is a
    # NEW-only forward date (20240117), reproducing the trimmed-expiration
    # mismatch that the builder's dedup also produces with sparse/abutting data.
    @staticmethod
    def _fixture():
        old = _make_contract(
            "VXF24", 20240115, [20240110, 20240112, 20240115], [95.0, 98.0, 100.0]
        )
        new = _make_contract(
            "VXG24", 20240215, [20240115, 20240117, 20240120], [105.0, 107.0, 110.0]
        )
        # Raw series as the stitcher concatenates it: old segment owns dates up
        # to (and including) the shared day 20240115; new segment from 20240117.
        raw = PriceSeries(
            dates=np.array(
                [20240110, 20240112, 20240115, 20240117, 20240120], dtype=np.int64
            ),
            open=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            high=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            low=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            close=np.array([95.0, 98.0, 100.0, 107.0, 110.0]),
            volume=np.full(5, 1000.0),
        )
        return old, new, raw

    def test_ratio_no_jump_at_shared_day(self):
        old, new, raw = self._fixture()
        # rd = 20240117 (new-only forward date) — the mismatch trigger.
        result = adjust_ratio(raw, [20240117], [old, new])
        # Shared-day continuity: adjusted old close on 20240115 must equal the
        # new contract's close on 20240115 (105). Factor = 105/100 = 1.05.
        idx_shared = 2  # 20240115
        assert result.close[idx_shared] == pytest.approx(105.0, abs=1e-9)
        # And it must NOT be the buggy 107 (107/100 cross-date factor).
        assert abs(result.close[idx_shared] - 107.0) > 1.0
        # Whole old segment scaled by the contemporaneous ratio 1.05.
        np.testing.assert_allclose(
            result.close[:3], [95.0 * 1.05, 98.0 * 1.05, 100.0 * 1.05], atol=1e-9
        )

    def test_difference_no_jump_at_shared_day(self):
        old, new, raw = self._fixture()
        result = adjust_difference(raw, [20240117], [old, new])
        # Shared-day continuity: 100 + (105-100) = 105. Buggy diff would be +7.
        idx_shared = 2
        assert result.close[idx_shared] == pytest.approx(105.0, abs=1e-9)
        assert abs(result.close[idx_shared] - 107.0) > 1.0
        np.testing.assert_allclose(
            result.close[:3], [95.0 + 5.0, 98.0 + 5.0, 100.0 + 5.0], atol=1e-9
        )


class TestSeamContinuityEndToEnd:
    """End-to-end (ContinuousSeriesBuilder.build) continuity at every seam.

    For a back-adjusted series, the cumulative factor applied to each old
    segment must equal the product/sum of the CONTEMPORANEOUS (shared-day) gaps
    at the rolls after it — no cross-date artifact. We verify this by comparing
    the full builder output against an independent reference that recomputes the
    gap explicitly from a shared trading day at each roll, AND by checking that
    the adjusted return across each real roll seam equals the new contract's own
    move on that shared day (tol 1e-9).
    """

    def setup_method(self):
        self.builder = ContinuousSeriesBuilder()

    def _overlapping_three(self):
        # Modest within-contract moves so any cross-date artifact is unmistakable.
        # Each successive contract OVERLAPS the prior on two trading days, so a
        # shared boundary day always exists (the no-fallback path).
        c1 = _make_contract(
            "A", 20240115, [20240110, 20240113, 20240115], [98.0, 99.0, 100.0]
        )
        c2 = _make_contract(
            "B",
            20240215,
            [20240113, 20240115, 20240213, 20240215],
            [104.0, 105.0, 109.0, 110.0],
        )
        c3 = _make_contract(
            "C", 20240315, [20240213, 20240215, 20240218], [114.0, 115.0, 116.0]
        )
        return c1, c2, c3

    def _reference_factor(self, old_c, new_c, rd, *, ratio: bool):
        """Independent contemporaneous-gap factor on the latest shared day <= rd."""
        shared = np.intersect1d(old_c.prices.dates, new_c.prices.dates)
        shared = shared[shared <= rd]
        ref = int(shared[-1])
        oc = float(old_c.prices.close[np.searchsorted(old_c.prices.dates, ref)])
        nc = float(new_c.prices.close[np.searchsorted(new_c.prices.dates, ref)])
        return (nc / oc) if ratio else (nc - oc)

    def test_ratio_multi_roll_matches_reference(self):
        c1, c2, c3 = self._overlapping_three()
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.RATIO
        )
        result = self.builder.build([c1, c2, c3], config)
        assert result.contracts == ("A", "B", "C")
        rd1, rd2 = result.roll_dates
        f1 = self._reference_factor(c1, c2, rd1, ratio=True)
        f2 = self._reference_factor(c2, c3, rd2, ratio=True)

        dates = result.prices.dates
        closes = result.prices.close
        # Segment c1 (dates < rd1) carries f1*f2; segment c2 (rd1<=d<rd2) carries
        # f2; segment c3 (d>=rd2) carries 1.0. Compare each adjusted close to the
        # raw contract close times its expected cumulative factor.
        raw_by_date = {}
        for c in (c1, c2, c3):
            for d, cl in zip(c.prices.dates.tolist(), c.prices.close.tolist()):
                raw_by_date[d] = cl  # later contract overwrites on shared days
        for d, adj in zip(dates.tolist(), closes.tolist()):
            if d < rd1:
                factor = f1 * f2
            elif d < rd2:
                factor = f2
            else:
                factor = 1.0
            assert adj == pytest.approx(raw_by_date[d] * factor, abs=1e-9), (
                f"date {d}: adjusted {adj} != raw {raw_by_date[d]} * {factor}"
            )

    def test_difference_multi_roll_matches_reference(self):
        c1, c2, c3 = self._overlapping_three()
        config = ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.DIFFERENCE
        )
        result = self.builder.build([c1, c2, c3], config)
        assert result.contracts == ("A", "B", "C")
        rd1, rd2 = result.roll_dates
        d1 = self._reference_factor(c1, c2, rd1, ratio=False)
        d2 = self._reference_factor(c2, c3, rd2, ratio=False)

        dates = result.prices.dates
        closes = result.prices.close
        raw_by_date = {}
        for c in (c1, c2, c3):
            for d, cl in zip(c.prices.dates.tolist(), c.prices.close.tolist()):
                raw_by_date[d] = cl
        for d, adj in zip(dates.tolist(), closes.tolist()):
            if d < rd1:
                shift = d1 + d2
            elif d < rd2:
                shift = d2
            else:
                shift = 0.0
            assert adj == pytest.approx(raw_by_date[d] + shift, abs=1e-9), (
                f"date {d}: adjusted {adj} != raw {raw_by_date[d]} + {shift}"
            )


class TestAbutmentFallbackWarns:
    """No shared trading day → approximate fallback + a clear warning."""

    def test_gap_logs_approximate_warning(self, caplog):
        import logging

        old = _make_contract(
            "A", 20240110, [20240105, 20240108, 20240110], [50.0, 52.0, 55.0]
        )
        new = _make_contract(
            "B", 20240130, [20240120, 20240125, 20240130], [60.0, 62.0, 65.0]
        )
        raw = PriceSeries(
            dates=np.array(
                [20240105, 20240108, 20240110, 20240120, 20240125, 20240130],
                dtype=np.int64,
            ),
            open=np.array([50.0, 52.0, 55.0, 60.0, 62.0, 65.0]),
            high=np.array([50.0, 52.0, 55.0, 60.0, 62.0, 65.0]),
            low=np.array([50.0, 52.0, 55.0, 60.0, 62.0, 65.0]),
            close=np.array([50.0, 52.0, 55.0, 60.0, 62.0, 65.0]),
            volume=np.full(6, 1000.0),
        )
        with caplog.at_level(logging.WARNING):
            result = adjust_ratio(raw, [20240120], [old, new])
        # Fallback factor = new(20240120)/old(nearest=20240110) = 60/55 (approx).
        np.testing.assert_allclose(
            result.close[:3],
            [50.0 * 60.0 / 55.0, 52.0 * 60.0 / 55.0, 55.0 * 60.0 / 55.0],
        )
        assert any("APPROXIMATE" in rec.message for rec in caplog.records), (
            "Expected an APPROXIMATE-gap warning when no shared day exists"
        )


class TestZeroNaNGuardSymmetry:
    """Both ratio and difference must skip-with-warning on a 0 or NaN ref close."""

    def _series(self, closes):
        c = np.array(closes, dtype=np.float64)
        return PriceSeries(
            dates=np.array([20240101, 20240110, 20240120], dtype=np.int64),
            open=c.copy(),
            high=c.copy(),
            low=c.copy(),
            close=c.copy(),
            volume=np.full(3, 1000.0),
        )

    def test_difference_zero_new_close_is_skipped(self, caplog):
        """A 0 new-close at the roll must NOT shift all history by -old_close.

        (Previously adjust_difference had no zero guard, so new_close==0 gave
        diff=-old_close and corrupted the whole series. Now it skips + warns.)
        """
        import logging

        old = _make_contract("A", 20240110, [20240101, 20240110], [100.0, 110.0])
        # New contract's shared-day close is 0 (degenerate). It is the only day
        # shared with `old`, so the gap helper returns (110, 0).
        new = _make_contract("B", 20240120, [20240110, 20240120], [0.0, 50.0])
        prices = self._series([100.0, 110.0, 50.0])
        with caplog.at_level(logging.WARNING):
            result = adjust_difference(prices, [20240110], [old, new])
        # Skipped → pre-roll history unchanged (NOT shifted by -110).
        np.testing.assert_array_equal(result.close, [100.0, 110.0, 50.0])
        assert any("skipped" in rec.message for rec in caplog.records)

    def test_difference_nan_close_is_skipped(self):
        old = _make_contract("A", 20240110, [20240101, 20240110], [100.0, 110.0])
        new = _make_contract("B", 20240120, [20240110, 20240120], [np.nan, 50.0])
        prices = self._series([100.0, 110.0, 50.0])
        result = adjust_difference(prices, [20240110], [old, new])
        # NaN must not poison the series.
        np.testing.assert_array_equal(result.close, [100.0, 110.0, 50.0])

    def test_ratio_nan_close_is_skipped(self):
        old = _make_contract("A", 20240110, [20240101, 20240110], [100.0, 110.0])
        new = _make_contract("B", 20240120, [20240110, 20240120], [np.nan, 50.0])
        prices = self._series([100.0, 110.0, 50.0])
        result = adjust_ratio(prices, [20240110], [old, new])
        np.testing.assert_array_equal(result.close, [100.0, 110.0, 50.0])
