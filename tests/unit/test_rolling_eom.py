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
    clamp_roll_dates_to_data,
    collapse_to_one_per_month,
    compute_roll_dates,
)
from tcg.data._rolling.stitcher import ContinuousSeriesBuilder
from tcg.data._utils import int_to_date
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
    expiration_cycle: str | None = None,
) -> ContractPriceData:
    n = len(dates)
    assert len(closes) == n
    c = np.array(closes, dtype=np.float64)
    return ContractPriceData(
        contract_id=contract_id,
        expiration=expiration,
        expiration_cycle=expiration_cycle,
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


# ── collapse_to_one_per_month (the VIX-weekly fix) ─────────────────────


class TestCollapseToOnePerMonth:
    def test_single_contract_per_month_is_noop(self):
        """ES-style / pre-2015-VIX: already one contract per month → unchanged."""
        c1 = _make_contract("a", 20240115, [20240101], [10.0])
        c2 = _make_contract("b", 20240221, [20240201], [11.0])
        c3 = _make_contract("c", 20240315, [20240301], [12.0])
        out = collapse_to_one_per_month([c1, c2, c3])
        assert [c.contract_id for c in out] == ["a", "b", "c"]

    def test_no_cycle_keeps_latest_expiring_with_data(self):
        """No expiration_cycle marker (e.g. EURUSD/NASDAQ): fall back to the
        latest-expiring contract that actually traded."""
        c1 = _make_contract("mar_early", 20240308, [20240301], [10.0])
        c2 = _make_contract("mar_late", 20240328, [20240302], [11.0])
        c3 = _make_contract("apr_early", 20240405, [20240401], [12.0])
        c4 = _make_contract("apr_late", 20240426, [20240402], [13.0])
        out = collapse_to_one_per_month([c1, c2, c3, c4])
        assert [c.contract_id for c in out] == ["mar_late", "apr_late"]

    def test_prefers_monthly_cycle_over_later_weekly(self):
        """VIX: the canonical monthly ('M') contract wins over a weekly ('W')
        that expires LATER in the same month. This is the correctness fix —
        the continuous series must ride the monthly future, not an end-of-month
        weekly a day from expiry."""
        monthly = _make_contract(
            "VX_M", 20240320, [20240101, 20240320], [14.0, 14.5], expiration_cycle="M"
        )
        weekly_later = _make_contract(
            "VXW_late",
            20240327,
            [20240301, 20240327],
            [15.0, 15.2],
            expiration_cycle="W",
        )
        out = collapse_to_one_per_month([monthly, weekly_later])
        assert [c.contract_id for c in out] == ["VX_M"]

    def test_prefers_contract_with_usable_data(self):
        """The latest-expiring contract is skipped when it never traded
        (all-zero closes), so a real month is not silently dropped."""
        dead_late = _make_contract("dead", 20240328, [20240320, 20240328], [0.0, 0.0])
        live_early = _make_contract(
            "live", 20240315, [20240301, 20240315], [11.0, 11.5]
        )
        out = collapse_to_one_per_month([live_early, dead_late])
        assert [c.contract_id for c in out] == ["live"]

    def test_output_sorted_by_expiration_regardless_of_input_order(self):
        c_may = _make_contract("may", 20240515, [20240501], [12.0])
        c_mar = _make_contract("mar", 20240328, [20240301], [10.0])
        c_apr = _make_contract("apr", 20240430, [20240401], [11.0])
        out = collapse_to_one_per_month([c_may, c_mar, c_apr])
        assert [c.expiration for c in out] == [20240328, 20240430, 20240515]

    def test_deterministic_tiebreak_on_equal_expiration(self):
        """A monthly and weekly sharing the SAME expiration day: deterministic
        regardless of input order (monthly preferred, then contract_id)."""
        m = _make_contract("VX_M", 20240320, [20240320], [14.0], expiration_cycle="M")
        w = _make_contract("VX_W", 20240320, [20240320], [14.1], expiration_cycle="W")
        assert [c.contract_id for c in collapse_to_one_per_month([m, w])] == ["VX_M"]
        assert [c.contract_id for c in collapse_to_one_per_month([w, m])] == ["VX_M"]

    def test_empty(self):
        assert collapse_to_one_per_month([]) == []


# ── clamp_roll_dates_to_data (the large-roll-offset guard) ─────────────


class TestClampRollDatesToData:
    def test_noop_when_incoming_listed_before_boundary(self):
        """Normal case: incoming contract already trades before the roll date →
        boundaries unchanged."""
        c1 = _make_contract("a", 20240315, [20240101, 20240315], [10.0, 10.5])
        c2 = _make_contract("b", 20240415, [20240201, 20240415], [11.0, 11.5])
        # boundary well inside both contracts' data → unchanged
        assert clamp_roll_dates_to_data([c1, c2], [20240301]) == [20240301]

    def test_clamps_up_to_incoming_first_tradeable_day(self):
        """A boundary before the incoming contract's first tradeable day is
        pushed up to that day, so the incoming contract's window is never empty
        (no silent hole from a large roll_offset)."""
        c1 = _make_contract("a", 20240315, [20230101, 20240315], [10.0, 10.5])
        # incoming contract first trades 2024-02-01
        c2 = _make_contract("b", 20240415, [20240201, 20240415], [11.0, 11.5])
        # a far-back boundary (huge offset) clamps up to 20240201
        assert clamp_roll_dates_to_data([c1, c2], [20230115]) == [20240201]

    def test_skips_leading_zero_close_of_incoming(self):
        """First *tradeable* day ignores leading zero-close (unlisted) rows."""
        c1 = _make_contract("a", 20240315, [20230101, 20240315], [10.0, 10.5])
        c2 = _make_contract(
            "b", 20240415, [20240201, 20240210, 20240415], [0.0, 11.0, 11.5]
        )
        assert clamp_roll_dates_to_data([c1, c2], [20230115]) == [20240210]

    def test_result_stays_non_decreasing(self):
        c1 = _make_contract("a", 20240315, [20240101], [10.0])
        c2 = _make_contract("b", 20240415, [20240301], [11.0])
        c3 = _make_contract("c", 20240515, [20240301], [12.0])
        out = clamp_roll_dates_to_data([c1, c2, c3], [20230101, 20230201])
        assert out == sorted(out)


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

    def test_multiple_contracts_per_month_does_not_crash(self):
        """Regression for the VIX weekly-futures crash.

        VIX lists 4-5 contracts PER MONTH (weeklies). Under END_OF_MONTH every
        contract in a month resolves to the same month-end, the old duplicate-
        guard dropped boundaries, and ``trim_overlaps`` then indexed past the
        (now shorter) roll_dates list → ``IndexError: list index out of range``
        → HTTP 500 on the Data page. The builder must collapse to one contract
        per month first, so this must build cleanly instead.
        """
        # Three weekly contracts in March, three in April, one in May.
        march = [
            _make_contract(
                "W_MAR_1", 20240308, [20240201, 20240301, 20240308], [10.0, 10.1, 10.2]
            ),
            _make_contract(
                "W_MAR_2", 20240315, [20240201, 20240308, 20240315], [10.3, 10.4, 10.5]
            ),
            _make_contract(
                "W_MAR_3", 20240328, [20240301, 20240315, 20240328], [10.6, 10.7, 10.8]
            ),
        ]
        april = [
            _make_contract("W_APR_1", 20240405, [20240328, 20240405], [11.0, 11.1]),
            _make_contract("W_APR_2", 20240412, [20240405, 20240412], [11.2, 11.3]),
            _make_contract("W_APR_3", 20240430, [20240412, 20240430], [11.4, 11.5]),
        ]
        may = [_make_contract("W_MAY_1", 20240515, [20240430, 20240515], [12.0, 12.1])]
        contracts = march + april + may

        config = ContinuousRollConfig(
            strategy=RollStrategy.END_OF_MONTH, adjustment=AdjustmentMethod.NONE
        )
        # Must NOT raise IndexError.
        result = self.builder.build(contracts, config)

        # Collapsed to the latest-expiring contract per month: the March 28th
        # weekly, the April 30th weekly, and the sole May contract.
        assert result.contracts == ("W_MAR_3", "W_APR_3", "W_MAY_1")
        # One boundary per contract transition (invariant restored).
        assert len(result.roll_dates) == len(result.contracts) - 1
        assert np.all(np.isfinite(result.prices.close))
        # Series is non-empty and strictly date-ordered.
        assert len(result.prices) > 0
        assert list(result.prices.dates) == sorted(set(result.prices.dates))

    def test_multi_per_month_rides_the_monthly_cycle_contract(self):
        """When the root marks a monthly-cycle contract, the collapsed EOM
        series rides the monthly ('M'), NOT the later end-of-month weekly."""
        contracts = [
            _make_contract(
                "VX_M_MAR",
                20240320,
                [20240115, 20240320],
                [14.0, 14.5],
                expiration_cycle="M",
            ),
            _make_contract(
                "VXW_MAR",
                20240327,
                [20240301, 20240327],
                [15.0, 15.2],
                expiration_cycle="W",
            ),
            _make_contract(
                "VX_M_APR",
                20240417,
                [20240115, 20240417],
                [16.0, 16.5],
                expiration_cycle="M",
            ),
            _make_contract(
                "VXW_APR",
                20240424,
                [20240401, 20240424],
                [17.0, 17.2],
                expiration_cycle="W",
            ),
        ]
        config = ContinuousRollConfig(
            strategy=RollStrategy.END_OF_MONTH, adjustment=AdjustmentMethod.NONE
        )
        result = self.builder.build(contracts, config)
        assert result.contracts == ("VX_M_MAR", "VX_M_APR")

    def test_multi_per_month_with_ratio_adjustment_is_finite(self):
        """collapse + ratio adjustment on a multi-per-month root: no NaN/inf even
        when the collapsed neighbours share no seam day (nearest-date fallback)."""
        contracts = [
            _make_contract("W_MAR_a", 20240308, [20240201, 20240308], [10.0, 10.2]),
            _make_contract("W_MAR_b", 20240328, [20240301, 20240328], [10.6, 10.8]),
            _make_contract("W_APR_a", 20240405, [20240402, 20240405], [11.0, 11.1]),
            _make_contract("W_APR_b", 20240430, [20240412, 20240430], [11.4, 11.5]),
        ]
        for method in (AdjustmentMethod.RATIO, AdjustmentMethod.DIFFERENCE):
            config = ContinuousRollConfig(
                strategy=RollStrategy.END_OF_MONTH, adjustment=method
            )
            result = self.builder.build(contracts, config)
            assert np.all(np.isfinite(result.prices.close))
            assert len(result.prices) > 0

    def test_large_roll_offset_does_not_disintegrate_series(self):
        """The clamp guard: a roll_offset far larger than each contract's history
        must NOT collapse the series to a single contract with a multi-year hole.
        Every contract should still contribute, rolling as early as data allows.
        """
        contracts = [
            _make_contract(
                "M1", 20240315, [20240101, 20240201, 20240315], [10.0, 10.1, 10.2]
            ),
            _make_contract(
                "M2", 20240415, [20240201, 20240301, 20240415], [11.0, 11.1, 11.2]
            ),
            _make_contract(
                "M3", 20240515, [20240301, 20240401, 20240515], [12.0, 12.1, 12.2]
            ),
        ]
        config = ContinuousRollConfig(
            strategy=RollStrategy.END_OF_MONTH,
            adjustment=AdjustmentMethod.NONE,
            roll_offset_days=365,  # far exceeds each contract's ~2-3 months history
        )
        result = self.builder.build(contracts, config)
        # All three contracts survive (no silent collapse to just the last one).
        assert len(result.contracts) == 3
        assert result.contracts == ("M1", "M2", "M3")
        # No multi-year hole: consecutive bars are within a normal roll spacing.
        dates = [int_to_date(int(d)) for d in result.prices.dates]
        max_gap = max((b - a).days for a, b in zip(dates, dates[1:]))
        assert max_gap < 120, f"unexpected {max_gap}d hole from large roll_offset"
