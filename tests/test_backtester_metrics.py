"""Test metrics computation from the vendored library."""

import numpy as np
import pytest
from tcg.backtester.lib.metrics import compute_metrics


def _make_dates(n: int) -> np.ndarray:
    """Generate n sequential YYYYMMDD dates starting from 20200102."""
    # Simple: just increment day (won't be valid calendar but metrics
    # only need strictly-increasing int64 dates)
    base = 20200102
    dates = []
    d = base
    for _ in range(n):
        dates.append(d)
        # Increment day, rolling month/year as needed
        day = d % 100
        month = (d // 100) % 100
        year = d // 10000
        day += 1
        if day > 28:  # simple — keep it safe
            day = 1
            month += 1
            if month > 12:
                month = 1
                year += 1
        d = year * 10000 + month * 100 + day
    return np.array(dates, dtype=np.int64)


class TestComputeMetrics:
    def test_flat_equity(self):
        """Flat equity should give zero returns and zero Sharpe."""
        n = 252
        equity = np.full(n, 100.0, dtype=np.float64)
        dates = _make_dates(n)
        m = compute_metrics(equity, dates, trades=[])
        assert m.total_return == pytest.approx(0.0)
        assert m.sharpe_ratio == pytest.approx(0.0)
        assert m.max_drawdown == pytest.approx(0.0)

    def test_positive_return(self):
        """Steadily rising equity should give positive metrics."""
        n = 252
        equity = np.array([100.0 + i * 0.1 for i in range(n)], dtype=np.float64)
        dates = _make_dates(n)
        m = compute_metrics(equity, dates, trades=[])
        assert m.total_return > 0
        assert m.annualized_return > 0
        assert m.max_drawdown == pytest.approx(0.0)  # No drawdown in monotonic rise

    def test_drawdown(self):
        """Equity that drops should report negative max_drawdown."""
        equity = np.array([100.0, 110.0, 90.0, 95.0, 100.0], dtype=np.float64)
        dates = _make_dates(5)
        m = compute_metrics(equity, dates, trades=[])
        # Max drawdown from 110 to 90 = (90-110)/110 ~ -0.1818
        assert m.max_drawdown < 0

    def test_metrics_suite_to_dict(self):
        """MetricsSuite.to_dict() should return all expected keys."""
        n = 30
        equity = np.full(n, 100.0, dtype=np.float64)
        dates = _make_dates(n)
        m = compute_metrics(equity, dates, trades=[])
        d = m.to_dict()
        expected_keys = {
            "total_return",
            "annualized_return",
            "sharpe_ratio",
            "sortino_ratio",
            "max_drawdown",
            "calmar_ratio",
            "cvar_5",
            "time_underwater_days",
            "annualized_volatility",
            "num_trades",
            "win_rate",
        }
        assert set(d.keys()) == expected_keys
