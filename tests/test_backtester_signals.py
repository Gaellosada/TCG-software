"""Test vectorized indicator functions from the vendored library."""

import numpy as np
import pytest
from tcg.backtester.lib.indicators import sma, ema, rsi


class TestSMA:
    def test_basic_sma(self):
        close = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = sma(close, 3)
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        np.testing.assert_almost_equal(result[2], 2.0)
        np.testing.assert_almost_equal(result[3], 3.0)
        np.testing.assert_almost_equal(result[4], 4.0)

    def test_window_1(self):
        close = np.array([10.0, 20.0, 30.0])
        result = sma(close, 1)
        np.testing.assert_array_almost_equal(result, close)

    def test_invalid_window(self):
        with pytest.raises(ValueError):
            sma(np.array([1.0, 2.0]), 0)


class TestEMA:
    def test_basic_ema(self):
        close = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = ema(close, 3)
        assert result.shape == close.shape
        # First value is seeded from first finite close
        assert not np.isnan(result[0])

    def test_invalid_span(self):
        with pytest.raises(ValueError):
            ema(np.array([1.0]), 0)


class TestRSI:
    def test_constant_price(self):
        """Constant price should give RSI = 50 (no gains/losses after warm-up)."""
        close = np.full(30, 100.0)
        result = rsi(close, 14)
        assert result.shape == close.shape
        # First 14 values are NaN (warm-up)
        assert all(np.isnan(result[:14]))
        # After warm-up: no gains, no losses -> avg_gain=0, avg_loss=0 -> RSI=50
        valid = result[~np.isnan(result)]
        if len(valid) > 0:
            np.testing.assert_almost_equal(valid[0], 50.0)

    def test_trending_up(self):
        """Strictly rising prices should give RSI near 100."""
        close = np.arange(1.0, 31.0)
        result = rsi(close, 14)
        # After warm-up, RSI should be very high (all gains, no losses)
        valid = result[~np.isnan(result)]
        if len(valid) > 0:
            assert valid[-1] > 90.0
