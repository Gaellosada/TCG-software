"""Tests for the END_OF_MONTH continuous-futures roll strategy (Issue #3).

Issue #3 (futures side): roll the continuous series on the **last TRADING day
of each month, regardless of contract expiry** — see
``workspace/tasks/reported-issues-fix/output/issue3_futures_diagnosis.md``.

The engine change is a single new branch in ``compute_roll_dates`` that snaps
each outgoing contract's expiration to the last trading day of its expiration
month (then ``roll_offset_days`` shifts it earlier, exactly as FRONT_MONTH).
``trim_overlaps`` / ``_concatenate`` / adjustment stay strategy-agnostic.

The ``_last_trading_day_of_month`` helper is **duplicated** into
``tcg.data._rolling`` (import-linter ``engine-data-isolation`` forbids
``tcg.data`` importing the engine's copy) — these tests pin that the
duplicate matches the engine original exactly so the two cannot silently
diverge.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

import numpy as np
import pytest

from tcg.data._rolling.calendar import (
    _last_trading_day_of_month,
    compute_roll_dates,
)
from tcg.data._rolling.stitcher import ContinuousSeriesBuilder
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContractPriceData,
    PriceSeries,
    RollStrategy,
)


# ── Helpers (mirror tests/unit/test_rolling.py) ────────────────────────


def _make_contract(
    contract_id: str,
    expiration: int,
    dates: list[int],
    closes: list[float],
) -> ContractPriceData:
    n = len(dates)
    assert len(closes) == n
    c = np.array(closes, dtype=np.float64)
    return ContractPriceData(
        contract_id=contract_id,
        expiration=expiration,
        prices=PriceSeries(
            dates=np.array(dates, dtype=np.int64),
            open=c.copy(),
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=np.full(n, 1000.0),
        ),
    )


# ── The duplicated leaf helper ─────────────────────────────────────────


class TestLastTradingDayOfMonth:
    def test_plain_month_end(self):
        """Last trading day of Jan-2024 is the 31st (a Wednesday)."""
        assert _last_trading_day_of_month(2024, 1) == date(2024, 1, 31)

    def test_leap_february(self):
        """Feb-2024 (leap year) ends on the 29th (a Thursday)."""
        assert _last_trading_day_of_month(2024, 2) == date(2024, 2, 29)

    def test_month_end_holiday_rolls_back(self):
        """Mar-29-2024 is Good Friday (CME holiday) → last trading day = the 28th.

        This is the load-bearing case: it proves the helper uses the TRADING
        calendar, not the naive calendar last day (which would be the 29th).
        """
        assert _last_trading_day_of_month(2024, 3) == date(2024, 3, 28)

    def test_matches_engine_helper(self):
        """The duplicate must agree with the engine original on every month of a
        multi-year span — the two copies cannot be allowed to diverge."""
        from tcg.engine.options.maturity.resolver import (
            _calendar,
            _last_business_day_of_month,
        )

        cal = _calendar("CME")
        for year in (2022, 2023, 2024, 2025):
            for month in range(1, 13):
                assert _last_trading_day_of_month(year, month) == (
                    _last_business_day_of_month(year, month, cal)
                ), f"mismatch at {year}-{month:02d}"


# ── compute_roll_dates(END_OF_MONTH) ───────────────────────────────────


class TestComputeRollDatesEndOfMonth:
    def test_single_contract_no_roll(self):
        c1 = _make_contract("ESH24", 20240315, [20240101], [5000.0])
        assert compute_roll_dates([c1], RollStrategy.END_OF_MONTH) == []

    def test_two_contracts_snap_to_month_end(self):
        """Outgoing contract expires 2024-03-15 (mid-month, a 3rd Friday) →
        roll snaps to 2024-03-28 (last trading day of MARCH; the 29th is Good
        Friday). FRONT_MONTH would roll at 20240315 — EOM rolls ~2 weeks later.
        """
        c1 = _make_contract("ESH24", 20240315, [20240101, 20240102], [5000.0, 5001.0])
        c2 = _make_contract("ESM24", 20240621, [20240401, 20240402], [5100.0, 5101.0])

        eom = compute_roll_dates([c1, c2], RollStrategy.END_OF_MONTH)
        front = compute_roll_dates([c1, c2], RollStrategy.FRONT_MONTH)

        assert eom == [20240328]  # last trading day of March 2024
        assert front == [20240315]  # FRONT_MONTH rolls at expiry
        assert eom != front

    def test_three_contracts(self):
        """Three contracts → two roll boundaries, each snapped to its outgoing
        contract's expiration month-end (last trading day)."""
        c1 = _make_contract("a", 20240115, [20240101], [10.0])  # Jan exp
        c2 = _make_contract("b", 20240215, [20240201], [11.0])  # Feb exp
        c3 = _make_contract("c", 20240315, [20240301], [12.0])  # Mar exp
        # Jan→31st, Feb→29th (leap).  c3 is the last contract (no boundary).
        assert compute_roll_dates([c1, c2, c3], RollStrategy.END_OF_MONTH) == [
            20240131,
            20240229,
        ]

    def test_roll_offset_shifts_month_end_earlier(self):
        """roll_offset_days composes: it shifts the month-end roll date earlier,
        exactly as it shifts the expiration for FRONT_MONTH."""
        c1 = _make_contract("a", 20240115, [20240101], [10.0])
        c2 = _make_contract("b", 20240221, [20240201], [11.0])
        # Jan-2024 last trading day = 20240131; minus 3 calendar days = 20240128.
        assert compute_roll_dates(
            [c1, c2], RollStrategy.END_OF_MONTH, roll_offset_days=3
        ) == [20240128]

    def test_empty_list(self):
        assert compute_roll_dates([], RollStrategy.END_OF_MONTH) == []

    def test_duplicate_month_end_collapses_with_guard(self):
        """cycle=None edge: two consecutive contracts expiring in the SAME month
        would resolve to the same month-end roll date → boundaries collapse.

        The guard drops the duplicate boundary so ``trim_overlaps`` does not get
        a degenerate zero-width window.  Four contracts where the first two share
        March: only ONE March boundary survives, then the April one (May is the
        last contract → no boundary).
        """
        c1 = _make_contract("a", 20240308, [20240101], [10.0])  # March exp (early)
        c2 = _make_contract("b", 20240315, [20240201], [11.0])  # March exp (late)
        c3 = _make_contract("c", 20240415, [20240401], [12.0])  # April exp
        c4 = _make_contract("d", 20240515, [20240501], [13.0])  # May exp (last)
        rolls = compute_roll_dates([c1, c2, c3, c4], RollStrategy.END_OF_MONTH)
        # March month-end (20240328) appears once (guard drops the duplicate),
        # then April month-end (20240430).  The collapse means FEWER than
        # len(contracts)-1 boundaries — a real possibility for END_OF_MONTH.
        assert rolls == [20240328, 20240430]

    def test_front_month_still_works(self):
        """Regression: FRONT_MONTH path is completely unchanged by the new branch."""
        c1 = _make_contract("a", 20240115, [20240101], [10.0])
        c2 = _make_contract("b", 20240215, [20240201], [11.0])
        assert compute_roll_dates([c1, c2], RollStrategy.FRONT_MONTH) == [20240115]

    def test_unknown_strategy_still_raises(self):
        """A bogus strategy value still raises (the guard only widened to admit
        END_OF_MONTH, not anything)."""
        c1 = _make_contract("a", 20240115, [20240101], [10.0])
        c2 = _make_contract("b", 20240215, [20240201], [11.0])
        with pytest.raises(ValueError, match="Unsupported roll strategy"):
            compute_roll_dates([c1, c2], "bogus_strategy")  # type: ignore[arg-type]


# ── End-to-end through ContinuousSeriesBuilder ─────────────────────────


class TestEndOfMonthThroughBuilder:
    def setup_method(self):
        self.builder = ContinuousSeriesBuilder()

    def _two_contracts(self):
        # c1 (front) trades Jan–through March; c2 (deferred) trades from Feb on.
        # c1 expires 2024-03-15; EOM rolls at 2024-03-28 (last trading day Mar).
        c1 = _make_contract(
            "ESH24",
            20240315,
            [20240115, 20240215, 20240328, 20240401],
            [5000.0, 5050.0, 5100.0, 5110.0],
        )
        c2 = _make_contract(
            "ESM24",
            20240621,
            [20240215, 20240328, 20240401, 20240501],
            [5200.0, 5260.0, 5270.0, 5300.0],
        )
        return c1, c2

    def test_no_adjustment_rolls_at_month_end(self):
        """The continuous series holds c1 until the March month-end (20240328),
        then switches to c2 — NOT at c1's 20240315 expiry."""
        c1, c2 = self._two_contracts()
        config = ContinuousRollConfig(
            strategy=RollStrategy.END_OF_MONTH,
            adjustment=AdjustmentMethod.NONE,
        )
        result = self.builder.build([c1, c2], config)

        assert result.contracts == ("ESH24", "ESM24")
        assert len(result.roll_dates) == 1
        # The roll boundary lands at the March month-end seam (20240328), which
        # _concatenate reports as the first date of the c2 segment.
        assert result.roll_dates == (20240328,)
        dates = list(result.prices.dates)
        closes = list(result.prices.close)
        # 20240215: c1 is STILL front (EOM has not rolled yet) → c1's 5050, not
        # c2's 5200.  This is the whole point of EOM vs FRONT_MONTH.
        assert closes[dates.index(20240215)] == 5050.0
        # 20240328 (seam, dedup → later contract c2 wins) and 20240401 → c2.
        assert closes[dates.index(20240328)] == 5260.0
        assert closes[dates.index(20240401)] == 5270.0

    def test_ratio_adjustment_at_eom_seam(self):
        """Back-adjustment is exact at the EOM seam: the shared day 20240328 is
        quoted by BOTH contracts (c1=5100, c2=5260) → ratio 5260/5100 applied to
        the pre-roll c1 history.  Adjustment is strategy-agnostic and unchanged.
        """
        c1, c2 = self._two_contracts()
        config = ContinuousRollConfig(
            strategy=RollStrategy.END_OF_MONTH,
            adjustment=AdjustmentMethod.RATIO,
        )
        result = self.builder.build([c1, c2], config)
        ratio = 5260.0 / 5100.0
        dates = list(result.prices.dates)
        closes = result.prices.close
        # Pre-roll c1 days scaled by the shared-day ratio.
        np.testing.assert_allclose(closes[dates.index(20240115)], 5000.0 * ratio)
        np.testing.assert_allclose(closes[dates.index(20240215)], 5050.0 * ratio)
        # c2 segment unadjusted.
        np.testing.assert_allclose(closes[dates.index(20240401)], 5270.0)
        assert np.all(np.isfinite(closes))

    def test_difference_adjustment_at_eom_seam(self):
        """Difference adjustment at the EOM seam: shared-day gap 5260-5100=+160
        added to the pre-roll c1 history."""
        c1, c2 = self._two_contracts()
        config = ContinuousRollConfig(
            strategy=RollStrategy.END_OF_MONTH,
            adjustment=AdjustmentMethod.DIFFERENCE,
        )
        result = self.builder.build([c1, c2], config)
        diff = 5260.0 - 5100.0
        dates = list(result.prices.dates)
        closes = result.prices.close
        np.testing.assert_allclose(closes[dates.index(20240115)], 5000.0 + diff)
        np.testing.assert_allclose(closes[dates.index(20240215)], 5050.0 + diff)
        np.testing.assert_allclose(closes[dates.index(20240401)], 5270.0)
