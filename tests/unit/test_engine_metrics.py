"""Tests for tcg.engine.metrics -- returns, equity curves, rebalancing, metrics, aggregation."""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.metrics import (
    aggregate_returns,
    compute_daily_returns,
    compute_equity_curve,
    compute_metrics,
    compute_weighted_portfolio,
)
from tcg.types.metrics import MetricsSuite


# ── Helpers ─────────────────────────────────────────────────────────


def _make_dates_monthly(n_months: int = 12, days_per_month: int = 21) -> np.ndarray:
    """Generate YYYYMMDD dates spanning n_months, ~21 trading days each."""
    dates = []
    year, month = 2020, 1
    for _ in range(n_months):
        for day in range(1, days_per_month + 1):
            dates.append(year * 10000 + month * 100 + day)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return np.array(dates, dtype=np.int64)


def _make_dates_weekly() -> np.ndarray:
    """Generate 20 trading days in Jan 2024 (Mon-Fri, 4 weeks)."""
    # Jan 2024: Mon Jan 1 ... Fri Jan 26
    # Week 1: 2,3,4,5 (1=Mon holiday, skip); Week 2: 8-12; Week 3: 15-19; Week 4: 22-26
    # For simplicity, just use 5 days per week starting Mon Jan 1
    dates = []
    for week_start_day in [1, 8, 15, 22]:
        for offset in range(5):
            dates.append(20240100 + week_start_day + offset)
    return np.array(dates, dtype=np.int64)


# ── compute_daily_returns ───────────────────────────────────────────


class TestComputeDailyReturns:
    def test_normal_returns(self):
        prices = np.array([100.0, 110.0, 99.0, 115.0])
        ret = compute_daily_returns(prices, "normal")
        assert np.isnan(ret[0])
        np.testing.assert_allclose(ret[1], 0.10, atol=1e-12)
        np.testing.assert_allclose(ret[2], (99.0 - 110.0) / 110.0, atol=1e-12)
        np.testing.assert_allclose(ret[3], (115.0 - 99.0) / 99.0, atol=1e-12)

    def test_log_returns(self):
        prices = np.array([100.0, 110.0, 99.0])
        ret = compute_daily_returns(prices, "log")
        assert np.isnan(ret[0])
        np.testing.assert_allclose(ret[1], np.log(110.0 / 100.0), atol=1e-12)
        np.testing.assert_allclose(ret[2], np.log(99.0 / 110.0), atol=1e-12)

    def test_single_price(self):
        prices = np.array([42.0])
        ret = compute_daily_returns(prices, "normal")
        assert len(ret) == 1
        assert np.isnan(ret[0])

    def test_empty_prices(self):
        prices = np.array([], dtype=np.float64)
        ret = compute_daily_returns(prices, "normal")
        assert len(ret) == 0

    def test_invalid_return_type(self):
        with pytest.raises(ValueError, match="return_type"):
            compute_daily_returns(np.array([1.0, 2.0]), "arithmetic")


# ── compute_equity_curve ────────────────────────────────────────────


class TestComputeEquityCurve:
    def test_normal_equity_curve(self):
        returns = np.array([np.nan, 0.10, -0.05, 0.02])
        curve = compute_equity_curve(returns, "normal", initial_value=100.0)
        assert curve[0] == 100.0
        np.testing.assert_allclose(curve[1], 110.0, atol=1e-10)
        np.testing.assert_allclose(curve[2], 110.0 * 0.95, atol=1e-10)
        np.testing.assert_allclose(curve[3], 110.0 * 0.95 * 1.02, atol=1e-10)

    def test_log_equity_curve(self):
        returns = np.array([np.nan, 0.05, -0.02])
        curve = compute_equity_curve(returns, "log", initial_value=100.0)
        assert curve[0] == 100.0
        np.testing.assert_allclose(curve[1], 100.0 * np.exp(0.05), atol=1e-10)
        np.testing.assert_allclose(curve[2], 100.0 * np.exp(0.05 - 0.02), atol=1e-10)

    def test_empty_returns(self):
        curve = compute_equity_curve(np.array([]), "normal")
        assert len(curve) == 0

    def test_single_return(self):
        curve = compute_equity_curve(np.array([np.nan]), "normal", initial_value=50.0)
        assert len(curve) == 1
        assert curve[0] == 50.0

    def test_invalid_return_type(self):
        with pytest.raises(ValueError, match="return_type"):
            compute_equity_curve(np.array([np.nan, 0.01]), "bad")


# ── compute_weighted_portfolio ──────────────────────────────────────


class TestComputeWeightedPortfolio:
    """Core tests for weighted portfolio computation with rebalancing."""

    def test_single_leg_no_rebalance(self):
        """Single leg with no rebalance should match the leg itself."""
        prices = np.array([100.0, 110.0, 105.0, 115.0])
        dates = np.array([20240101, 20240102, 20240103, 20240104], dtype=np.int64)

        port_ret, leg_ret, port_eq, leg_eq, raw_leg_eq, rb_dates = compute_weighted_portfolio(
            aligned_closes={"A": prices},
            weights={"A": 1.0},
            rebalance_freq="none",
            return_type="normal",
            dates=dates,
        )
        assert len(port_ret) == 4
        assert np.isnan(port_ret[0])
        assert port_eq[0] == 100.0
        # Portfolio equity should track the single leg
        np.testing.assert_allclose(port_eq, leg_eq["A"], atol=1e-10)
        # No rebalance -> raw legs are same as leg equities, no rebalance dates
        assert raw_leg_eq is leg_eq
        assert rb_dates == []

    def test_equal_weight_daily_rebalance(self):
        """Two legs with equal weight and daily rebalance."""
        prices_a = np.array([100.0, 110.0, 120.0])
        prices_b = np.array([100.0, 90.0, 100.0])
        dates = np.array([20240101, 20240102, 20240103], dtype=np.int64)

        port_ret, leg_ret, port_eq, leg_eq, _, rb_dates = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.5, "B": 0.5},
            rebalance_freq="daily",
            return_type="normal",
            dates=dates,
        )

        # Day 1: A=+10%, B=-10%, portfolio = 0.5*0.10 + 0.5*(-0.10) = 0%
        np.testing.assert_allclose(port_ret[1], 0.0, atol=1e-12)
        # Day 2: A=+9.09%, B=+11.11%, portfolio = 0.5*0.0909 + 0.5*0.1111 = 10.10%
        expected_day2 = 0.5 * (120.0 / 110.0 - 1) + 0.5 * (100.0 / 90.0 - 1)
        np.testing.assert_allclose(port_ret[2], expected_day2, atol=1e-10)
        # Daily rebalance returns no rebalance dates
        assert rb_dates == []

    def test_daily_rebalance_equity_starts_at_100(self):
        prices_a = np.array([100.0, 105.0])
        dates = np.array([20240101, 20240102], dtype=np.int64)

        _, _, port_eq, _, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a},
            weights={"A": 1.0},
            rebalance_freq="daily",
            return_type="normal",
            dates=dates,
        )
        assert port_eq[0] == 100.0
        np.testing.assert_allclose(port_eq[1], 105.0, atol=1e-10)

    def test_buy_and_hold_drift(self):
        """Buy-and-hold: legs drift independently."""
        # A doubles, B stays flat
        prices_a = np.array([100.0, 200.0])
        prices_b = np.array([100.0, 100.0])
        dates = np.array([20240101, 20240102], dtype=np.int64)

        _, _, port_eq, leg_eq, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.5, "B": 0.5},
            rebalance_freq="none",
            return_type="normal",
            dates=dates,
        )
        # A starts at 50, doubles to 100. B starts at 50, stays at 50.
        np.testing.assert_allclose(leg_eq["A"][0], 50.0, atol=1e-10)
        np.testing.assert_allclose(leg_eq["A"][1], 100.0, atol=1e-10)
        np.testing.assert_allclose(leg_eq["B"][0], 50.0, atol=1e-10)
        np.testing.assert_allclose(leg_eq["B"][1], 50.0, atol=1e-10)
        np.testing.assert_allclose(port_eq[1], 150.0, atol=1e-10)

    def test_monthly_rebalance(self):
        """Monthly rebalance redistributes at month boundary."""
        # 3 days in Jan, 2 days in Feb
        dates = np.array([20240102, 20240103, 20240104, 20240201, 20240202], dtype=np.int64)
        prices_a = np.array([100.0, 110.0, 120.0, 130.0, 140.0])
        prices_b = np.array([100.0, 100.0, 100.0, 100.0, 100.0])

        _, _, port_eq, leg_eq, _, rb_dates = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.5, "B": 0.5},
            rebalance_freq="monthly",
            return_type="normal",
            dates=dates,
        )

        # Day 0: A=50, B=50, total=100
        assert port_eq[0] == 100.0

        # End of Jan (day 2): A has grown by 20%, B flat
        # A leg: 50 * 1.1 * (120/110) = 50 * 1.1 * 1.0909 = 60
        # B leg: 50
        # Total at end of Jan = 110
        np.testing.assert_allclose(leg_eq["A"][2], 60.0, atol=1e-10)
        np.testing.assert_allclose(leg_eq["B"][2], 50.0, atol=1e-10)

        # Day 3 (Feb 1): rebalance. Total=110, each leg gets 55
        np.testing.assert_allclose(leg_eq["A"][3], 55.0 * (130.0 / 120.0), atol=1e-10)
        np.testing.assert_allclose(leg_eq["B"][3], 55.0 * (100.0 / 100.0), atol=1e-10)

        # Monthly rebalance should report Feb 1 as a rebalance date
        assert rb_dates == [20240201]

    def test_quarterly_rebalance(self):
        """Quarterly boundaries at months 1, 4, 7, 10."""
        # March 31 to April 1 = quarter boundary
        dates = np.array([20240329, 20240401], dtype=np.int64)
        prices_a = np.array([100.0, 110.0])
        prices_b = np.array([100.0, 90.0])

        _, _, port_eq, leg_eq, _, rb_dates = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.6, "B": 0.4},
            rebalance_freq="quarterly",
            return_type="normal",
            dates=dates,
        )

        # Day 0: A=60, B=40, total=100
        # Day 1: quarterly boundary -> rebalance first (total still 100), then apply returns
        # After rebalance: A=60, B=40 (same because total didn't change yet from day 0)
        # Then A grows by 10%, B falls by 10%
        np.testing.assert_allclose(leg_eq["A"][1], 60.0 * 1.10, atol=1e-10)
        np.testing.assert_allclose(leg_eq["B"][1], 40.0 * 0.90, atol=1e-10)
        # Quarter boundary at April 1 -> rebalance date
        assert rb_dates == [20240401]

    def test_weekly_rebalance(self):
        """Weekly rebalance at Monday boundary."""
        # Week 1: Mon-Fri Jan 1-5, Week 2: Mon Jan 8
        dates = np.array([20240101, 20240102, 20240103, 20240104, 20240105, 20240108],
                         dtype=np.int64)
        # Constant prices for simplicity of structure test
        n = 6
        prices_a = np.linspace(100.0, 112.0, n)
        prices_b = np.ones(n) * 100.0

        port_ret, leg_ret, port_eq, leg_eq, _, rb_dates = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.5, "B": 0.5},
            rebalance_freq="weekly",
            return_type="normal",
            dates=dates,
        )

        # Basic structural checks
        assert len(port_ret) == n
        assert np.isnan(port_ret[0])
        assert port_eq[0] == 100.0
        # Portfolio should grow since A grows and B is flat
        assert port_eq[-1] > 100.0
        # Week boundary at Jan 8 -> rebalance date
        assert rb_dates == [20240108]

    def test_annually_rebalance(self):
        """Annual rebalance at year boundary."""
        dates = np.array([20231229, 20240102], dtype=np.int64)
        prices_a = np.array([100.0, 110.0])
        prices_b = np.array([100.0, 95.0])

        _, _, port_eq, leg_eq, _, rb_dates = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.7, "B": 0.3},
            rebalance_freq="annually",
            return_type="normal",
            dates=dates,
        )

        # Year changes -> rebalance on day 1
        # Day 0: A=70, B=30, total=100
        # Rebalance: same (total still 100)
        # A: 70 * 1.10 = 77, B: 30 * 0.95 = 28.5
        np.testing.assert_allclose(leg_eq["A"][1], 70.0 * 1.10, atol=1e-10)
        np.testing.assert_allclose(leg_eq["B"][1], 30.0 * 0.95, atol=1e-10)
        # Year boundary at Jan 2 2024 -> rebalance date
        assert rb_dates == [20240102]

    def test_weight_normalization(self):
        """Weights normalized by sum of absolute values."""
        prices_a = np.array([100.0, 110.0])
        dates = np.array([20240101, 20240102], dtype=np.int64)

        # Weight of 2.0 should be normalized to 1.0 (only one leg)
        _, _, port_eq_2, _, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a},
            weights={"A": 2.0},
            rebalance_freq="daily",
            return_type="normal",
            dates=dates,
        )

        _, _, port_eq_1, _, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a},
            weights={"A": 1.0},
            rebalance_freq="daily",
            return_type="normal",
            dates=dates,
        )

        np.testing.assert_allclose(port_eq_2, port_eq_1, atol=1e-10)

    def test_short_leg_buy_and_hold(self):
        """Negative weight = short position: gains when underlying falls."""
        prices_a = np.array([100.0, 90.0])  # falls 10%
        dates = np.array([20240101, 20240102], dtype=np.int64)

        _, _, port_eq, leg_eq, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a},
            weights={"A": -1.0},
            rebalance_freq="none",
            return_type="normal",
            dates=dates,
        )

        # Short leg starts at 100 (abs(w)*100).
        # Underlying falls 10%, short position gains: 2*100 - 90 = 110
        np.testing.assert_allclose(leg_eq["A"][0], 100.0, atol=1e-10)
        np.testing.assert_allclose(leg_eq["A"][1], 110.0, atol=1e-10)

    def test_log_returns_portfolio(self):
        """Log return mode works end-to-end."""
        prices = np.array([100.0, 105.0, 110.0])
        dates = np.array([20240101, 20240102, 20240103], dtype=np.int64)

        port_ret, _, port_eq, _, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices},
            weights={"A": 1.0},
            rebalance_freq="daily",
            return_type="log",
            dates=dates,
        )

        np.testing.assert_allclose(port_ret[1], np.log(105.0 / 100.0), atol=1e-12)
        np.testing.assert_allclose(port_ret[2], np.log(110.0 / 105.0), atol=1e-12)

    def test_validation_empty_closes(self):
        with pytest.raises(ValueError, match="empty"):
            compute_weighted_portfolio({}, {}, "none", "normal",
                                       np.array([], dtype=np.int64))

    def test_validation_missing_weights(self):
        with pytest.raises(ValueError, match="Weights missing"):
            compute_weighted_portfolio(
                {"A": np.array([1.0])}, {}, "none", "normal",
                np.array([20240101], dtype=np.int64),
            )

    def test_validation_zero_weights(self):
        with pytest.raises(ValueError, match="zero"):
            compute_weighted_portfolio(
                {"A": np.array([1.0])}, {"A": 0.0}, "none", "normal",
                np.array([20240101], dtype=np.int64),
            )

    def test_validation_length_mismatch(self):
        with pytest.raises(ValueError, match="Length mismatch"):
            compute_weighted_portfolio(
                {"A": np.array([1.0, 2.0]), "B": np.array([1.0])},
                {"A": 0.5, "B": 0.5},
                "none", "normal",
                np.array([20240101, 20240102], dtype=np.int64),
            )

    def test_validation_dates_length(self):
        with pytest.raises(ValueError, match="dates length"):
            compute_weighted_portfolio(
                {"A": np.array([1.0, 2.0])},
                {"A": 1.0},
                "none", "normal",
                np.array([20240101], dtype=np.int64),
            )

    def test_raw_leg_equities_diverge_from_rebalanced(self):
        """raw_leg_equities should differ from leg_equities when rebalancing is active."""
        dates = np.array([20240102, 20240103, 20240104, 20240201, 20240202], dtype=np.int64)
        prices_a = np.array([100.0, 110.0, 120.0, 130.0, 140.0])
        prices_b = np.array([100.0, 100.0, 100.0, 100.0, 100.0])

        _, _, _, leg_eq, raw_leg_eq, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.5, "B": 0.5},
            rebalance_freq="monthly",
            return_type="normal",
            dates=dates,
        )

        # raw_leg_eq is buy-and-hold — should NOT be the same object
        assert raw_leg_eq is not leg_eq

        # Before the rebalance boundary (day 2), values should match
        np.testing.assert_allclose(leg_eq["A"][0], raw_leg_eq["A"][0], atol=1e-10)
        np.testing.assert_allclose(leg_eq["A"][2], raw_leg_eq["A"][2], atol=1e-10)

        # After rebalance (day 3+), rebalanced legs diverge from raw
        # Rebalanced A gets reset to target weight; raw A keeps drifting
        assert not np.allclose(leg_eq["A"][3:], raw_leg_eq["A"][3:], atol=1e-10)

    def test_rebalance_preserves_total_value(self):
        """Rebalancing should not change total portfolio value."""
        dates = np.array([20240102, 20240103, 20240201, 20240202], dtype=np.int64)
        prices_a = np.array([100.0, 120.0, 130.0, 140.0])
        prices_b = np.array([100.0, 80.0, 70.0, 60.0])

        _, _, port_eq_rebal, leg_eq_rebal, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.5, "B": 0.5},
            rebalance_freq="monthly",
            return_type="normal",
            dates=dates,
        )

        # At rebalance point (Feb 1), sum of leg equities should equal portfolio equity
        for i in range(len(dates)):
            total_legs = sum(leg_eq_rebal[lbl][i] for lbl in leg_eq_rebal)
            np.testing.assert_allclose(total_legs, port_eq_rebal[i], atol=1e-10,
                                       err_msg=f"Mismatch at index {i}")


# ── compute_metrics ─────────────────────────────────────────────────


class TestComputeMetrics:
    def test_returns_metrics_suite(self):
        """Verify compute_metrics returns a MetricsSuite instance."""
        equity = np.array([100.0, 105.0, 110.0, 108.0, 115.0])
        m = compute_metrics(equity)
        assert isinstance(m, MetricsSuite)
        assert m.num_trades == 0
        assert m.win_rate is None

    def test_total_return(self):
        equity = np.array([100.0, 150.0])
        m = compute_metrics(equity)
        np.testing.assert_allclose(m.total_return, 0.5, atol=1e-10)

    def test_annualized_return(self):
        """252 days of constant equity growth."""
        n = 253  # 252 trading days + initial
        daily_r = 0.001
        equity = 100.0 * np.cumprod(np.concatenate([[1.0], np.full(n - 1, 1.0 + daily_r)]))
        m = compute_metrics(equity)
        expected_cagr = (equity[-1] / equity[0]) ** (252.0 / (n - 1)) - 1.0
        np.testing.assert_allclose(m.annualized_return, expected_cagr, atol=1e-10)

    def test_max_drawdown_negative(self):
        """Max drawdown is negative by convention."""
        equity = np.array([100.0, 110.0, 80.0, 90.0])
        m = compute_metrics(equity)
        # Peak at 110, trough at 80: drawdown = (80-110)/110 = -0.2727...
        expected_dd = (80.0 - 110.0) / 110.0
        np.testing.assert_allclose(m.max_drawdown, expected_dd, atol=1e-10)

    def test_sharpe_ratio_positive(self):
        """Consistently positive returns should produce positive Sharpe."""
        n = 253
        equity = 100.0 * np.cumprod(
            np.concatenate([[1.0], np.full(n - 1, 1.001)])
        )
        m = compute_metrics(equity)
        assert m.sharpe_ratio > 0

    def test_sortino_ratio(self):
        """Sortino should be higher than Sharpe when upside vol > downside vol."""
        np.random.seed(42)
        # Generate returns with more upside than downside
        n = 253
        returns = np.abs(np.random.normal(0.001, 0.01, n - 1))  # all positive
        returns[::10] = -0.005  # occasional small losses
        equity = 100.0 * np.cumprod(np.concatenate([[1.0], 1.0 + returns]))
        m = compute_metrics(equity)
        # With mostly positive returns and small losses, Sortino > Sharpe
        assert m.sortino_ratio > m.sharpe_ratio

    def test_sortino_single_negative_return(self):
        """Sortino should be non-zero when exactly one negative excess return exists."""
        # equity: 100 -> 105 -> 103 -> 108
        # daily returns: +5%, -1.9%, +4.85%
        # Only one negative return, so downside has exactly 1 element
        equity = np.array([100.0, 105.0, 103.0, 108.0])
        m = compute_metrics(equity)
        assert m.sortino_ratio != 0.0, "Sortino should be non-zero with one negative return"
        assert m.sortino_ratio > 0, "Sortino should be positive for net-positive returns"

    def test_annualized_volatility(self):
        """Annualized vol should be daily vol * sqrt(252)."""
        np.random.seed(123)
        n = 253
        daily_returns = np.random.normal(0.0005, 0.01, n - 1)
        equity = 100.0 * np.cumprod(np.concatenate([[1.0], 1.0 + daily_returns]))
        m = compute_metrics(equity)

        # Recompute expected
        actual_daily = np.diff(equity) / equity[:-1]
        expected_vol = float(np.std(actual_daily, ddof=1) * np.sqrt(252.0))
        np.testing.assert_allclose(m.annualized_volatility, expected_vol, atol=1e-10)

    def test_cvar_5(self):
        """CVaR 5% should be the mean of the worst 5% of daily returns."""
        equity = np.array([100.0, 95.0, 90.0, 88.0, 92.0, 95.0,
                           98.0, 100.0, 102.0, 105.0, 108.0,
                           110.0, 107.0, 109.0, 112.0, 115.0,
                           118.0, 120.0, 122.0, 125.0, 128.0])
        m = compute_metrics(equity)
        # CVaR should be negative (worst returns)
        assert m.cvar_5 < 0

    def test_time_underwater(self):
        equity = np.array([100.0, 110.0, 105.0, 108.0, 112.0])
        m = compute_metrics(equity)
        # Days 2 and 3 are underwater (below peak of 110)
        assert m.time_underwater_days == 2

    def test_calmar_ratio(self):
        equity = np.array([100.0, 110.0, 80.0, 120.0])
        m = compute_metrics(equity)
        expected_dd = (80.0 - 110.0) / 110.0  # negative
        expected_calmar = m.annualized_return / abs(expected_dd)
        np.testing.assert_allclose(m.calmar_ratio, expected_calmar, atol=1e-10)

    def test_insufficient_data(self):
        """Single data point should return empty metrics."""
        m = compute_metrics(np.array([100.0]))
        assert m.total_return == 0.0
        assert m.annualized_volatility == 0.0
        assert m.sortino_ratio == 0.0

    def test_risk_free_rate(self):
        """Non-zero risk-free rate should reduce Sharpe."""
        n = 253
        equity = 100.0 * np.cumprod(
            np.concatenate([[1.0], np.full(n - 1, 1.001)])
        )
        m_no_rf = compute_metrics(equity, risk_free_rate=0.0)
        m_with_rf = compute_metrics(equity, risk_free_rate=0.05)
        assert m_with_rf.sharpe_ratio < m_no_rf.sharpe_ratio

    def test_flat_equity(self):
        """Flat equity -> zero returns, zero vol, zero Sharpe."""
        equity = np.full(100, 100.0)
        m = compute_metrics(equity)
        assert m.total_return == 0.0
        assert m.annualized_volatility == 0.0
        assert m.sharpe_ratio == 0.0
        assert m.max_drawdown == 0.0


# ── aggregate_returns ───────────────────────────────────────────────


class TestAggregateReturns:
    def test_monthly_aggregation_normal(self):
        """Monthly aggregation compounds normal returns correctly."""
        dates = np.array([20240102, 20240103, 20240201, 20240202], dtype=np.int64)
        returns = np.array([np.nan, 0.01, 0.02, -0.01])
        per_leg = {"A": np.array([np.nan, 0.015, 0.025, -0.005])}

        result = aggregate_returns(dates, returns, per_leg, "normal", "monthly")

        assert len(result) == 2
        assert result[0]["period"] == "2024-01"
        assert result[1]["period"] == "2024-02"

        # Jan: compound of [0.01] (NaN skipped)
        np.testing.assert_allclose(result[0]["portfolio"], 0.01, atol=1e-10)
        # Feb: compound of [0.02, -0.01]
        expected_feb = (1.02) * (0.99) - 1.0
        np.testing.assert_allclose(result[1]["portfolio"], expected_feb, atol=1e-10)

    def test_monthly_aggregation_log(self):
        """Monthly aggregation sums log returns."""
        dates = np.array([20240102, 20240103, 20240201, 20240202], dtype=np.int64)
        returns = np.array([np.nan, 0.01, 0.02, -0.01])
        per_leg = {"A": returns.copy()}

        result = aggregate_returns(dates, returns, per_leg, "log", "monthly")

        assert len(result) == 2
        # Jan: sum of [0.01]
        np.testing.assert_allclose(result[0]["portfolio"], 0.01, atol=1e-10)
        # Feb: sum of [0.02, -0.01] = 0.01
        np.testing.assert_allclose(result[1]["portfolio"], 0.01, atol=1e-10)

    def test_yearly_aggregation(self):
        dates = np.array([20230102, 20230103, 20240102, 20240103], dtype=np.int64)
        returns = np.array([np.nan, 0.05, 0.03, 0.02])
        per_leg: dict[str, np.ndarray] = {}

        result = aggregate_returns(dates, returns, per_leg, "normal", "yearly")

        assert len(result) == 2
        assert result[0]["period"] == "2023"
        assert result[1]["period"] == "2024"

    def test_invalid_granularity(self):
        with pytest.raises(ValueError, match="granularity"):
            aggregate_returns(
                np.array([20240101], dtype=np.int64),
                np.array([0.01]),
                {},
                "normal",
                "quarterly",
            )

    def test_all_nan_period_skipped(self):
        """Periods with only NaN returns should be omitted."""
        dates = np.array([20240102, 20240201], dtype=np.int64)
        returns = np.array([np.nan, 0.01])
        per_leg: dict[str, np.ndarray] = {}

        result = aggregate_returns(dates, returns, per_leg, "normal", "monthly")

        # Jan has only NaN -> skipped, Feb has 0.01
        assert len(result) == 1
        assert result[0]["period"] == "2024-02"


# ── Integration: full pipeline ──────────────────────────────────────


class TestIntegration:
    def test_full_pipeline_daily_rebalance(self):
        """End-to-end: prices -> portfolio -> metrics."""
        np.random.seed(99)
        n = 253
        dates = np.array(
            [20240101 + i for i in range(n)], dtype=np.int64
        )
        # Note: these dates aren't all valid calendar dates, but the engine
        # doesn't validate calendar correctness -- it only detects boundaries.
        prices_a = 100.0 * np.cumprod(
            np.concatenate([[1.0], 1.0 + np.random.normal(0.0003, 0.01, n - 1)])
        )
        prices_b = 100.0 * np.cumprod(
            np.concatenate([[1.0], 1.0 + np.random.normal(0.0001, 0.005, n - 1)])
        )

        port_ret, leg_ret, port_eq, leg_eq, _, _ = compute_weighted_portfolio(
            aligned_closes={"stocks": prices_a, "bonds": prices_b},
            weights={"stocks": 0.6, "bonds": 0.4},
            rebalance_freq="daily",
            return_type="normal",
            dates=dates,
        )

        assert len(port_ret) == n
        assert len(port_eq) == n
        assert port_eq[0] == 100.0

        # Compute metrics
        m = compute_metrics(port_eq)
        assert isinstance(m, MetricsSuite)
        assert m.num_trades == 0
        # Volatility should be positive for random data
        assert m.annualized_volatility > 0

    def test_full_pipeline_monthly_rebalance(self):
        """Monthly rebalance pipeline with real-ish dates."""
        # 2 months of data, ~21 days each
        dates_list = []
        for m in [1, 2]:
            for d in range(1, 22):
                dates_list.append(20240000 + m * 100 + d)
        dates = np.array(dates_list, dtype=np.int64)
        n = len(dates)

        np.random.seed(77)
        prices_a = 100.0 * np.cumprod(
            np.concatenate([[1.0], 1.0 + np.random.normal(0.0005, 0.015, n - 1)])
        )
        prices_b = 100.0 * np.cumprod(
            np.concatenate([[1.0], 1.0 + np.random.normal(0.0002, 0.008, n - 1)])
        )

        port_ret, leg_ret, port_eq, leg_eq, _, _ = compute_weighted_portfolio(
            aligned_closes={"A": prices_a, "B": prices_b},
            weights={"A": 0.7, "B": 0.3},
            rebalance_freq="monthly",
            return_type="normal",
            dates=dates,
        )

        m = compute_metrics(port_eq)
        assert isinstance(m, MetricsSuite)

        # Aggregate monthly
        agg = aggregate_returns(dates, port_ret, leg_ret, "normal", "monthly")
        assert len(agg) >= 1  # At least one valid month


# ── Engine __init__ exports ─────────────────────────────────────────


class TestEngineExports:
    def test_imports_from_engine(self):
        """Verify public API is accessible from tcg.engine."""
        from tcg.engine import (
            aggregate_returns,
            compute_daily_returns,
            compute_equity_curve,
            compute_metrics,
            compute_weighted_portfolio,
        )
        # Just verify they're callable
        assert callable(compute_daily_returns)
        assert callable(compute_equity_curve)
        assert callable(compute_weighted_portfolio)
        assert callable(compute_metrics)
        assert callable(aggregate_returns)
