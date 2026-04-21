"""Unit tests for the Historical Volatility indicator.

Covers: NaN warm-up, hand-computed known values, short input,
and default window on a longer series.
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.indicator_exec import run_indicator

HISTORICAL_VOL_CODE = """def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    for i in range(window - 1, n):
        chunk = s[i - window + 1 : i + 1]
        rets = chunk[1:] / chunk[:-1] - 1.0
        out[i] = np.std(rets, ddof=1) * (252.0 ** 0.5) * 100.0
    return out"""


def _series(values):
    return {"close": np.asarray(values, dtype=np.float64)}


def _expected_hvol(close, window):
    """Reference implementation for verification."""
    n = len(close)
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    for i in range(window - 1, n):
        chunk = close[i - window + 1 : i + 1]
        rets = chunk[1:] / chunk[:-1] - 1.0
        out[i] = np.std(rets, ddof=1) * (252.0 ** 0.5) * 100.0
    return out


class TestNaNWarmup:
    """First (window - 1) entries must be NaN."""

    def test_nan_warmup_window3(self):
        close = [100.0, 102.0, 101.0, 103.0, 105.0]
        result = run_indicator(HISTORICAL_VOL_CODE, {"window": 3}, _series(close))
        assert np.isnan(result[0]), "index 0 should be NaN"
        assert np.isnan(result[1]), "index 1 should be NaN"
        assert not np.isnan(result[2]), "index 2 should NOT be NaN"

    @pytest.mark.parametrize("window", [3, 5, 10])
    def test_nan_warmup_parametrized(self, window):
        # window >= 3 ensures at least 2 returns per chunk, so std(ddof=1) is
        # well-defined. window=2 gives a single return per chunk and
        # std(ddof=1) of one element is NaN — a separate edge case, not a
        # warm-up issue.
        close = list(range(100, 130))  # 30 values
        result = run_indicator(
            HISTORICAL_VOL_CODE, {"window": window}, _series(close)
        )
        assert np.isnan(result[: window - 1]).all(), (
            f"first {window - 1} entries should all be NaN"
        )
        assert not np.isnan(result[window - 1 :]).any(), (
            f"entries from index {window - 1} onward should not be NaN"
        )


class TestKnownValues:
    """Hand-computed values for window=3 on [100, 102, 101, 103, 105]."""

    def test_known_values(self):
        close = np.array([100.0, 102.0, 101.0, 103.0, 105.0])
        result = run_indicator(HISTORICAL_VOL_CODE, {"window": 3}, _series(close))

        # i=2: chunk=[100,102,101]
        #   returns = [102/100 - 1, 101/102 - 1] = [0.02, -0.00980392156862745]
        #   std(ddof=1) * sqrt(252)
        rets_2 = np.array([102.0 / 100.0 - 1.0, 101.0 / 102.0 - 1.0])
        expected_2 = np.std(rets_2, ddof=1) * (252.0 ** 0.5) * 100.0

        # i=3: chunk=[102,101,103]
        #   returns = [101/102 - 1, 103/101 - 1]
        rets_3 = np.array([101.0 / 102.0 - 1.0, 103.0 / 101.0 - 1.0])
        expected_3 = np.std(rets_3, ddof=1) * (252.0 ** 0.5) * 100.0

        # i=4: chunk=[101,103,105]
        #   returns = [103/101 - 1, 105/103 - 1]
        rets_4 = np.array([103.0 / 101.0 - 1.0, 105.0 / 103.0 - 1.0])
        expected_4 = np.std(rets_4, ddof=1) * (252.0 ** 0.5) * 100.0

        np.testing.assert_allclose(result[2], expected_2, rtol=1e-10)
        np.testing.assert_allclose(result[3], expected_3, rtol=1e-10)
        np.testing.assert_allclose(result[4], expected_4, rtol=1e-10)

        # Cross-check against pre-computed numeric values (percentage).
        np.testing.assert_allclose(result[2], 33.45481898762581, rtol=1e-10)
        np.testing.assert_allclose(result[3], 33.2325423111838, rtol=1e-10)
        np.testing.assert_allclose(result[4], 0.4316051969748184, rtol=1e-10)


class TestShortInput:
    """Input shorter than window returns all NaN."""

    def test_short_input_all_nan(self):
        close = [100.0, 102.0]  # length 2, window default 20
        result = run_indicator(HISTORICAL_VOL_CODE, {"window": 20}, _series(close))
        assert result.shape == (2,)
        assert np.isnan(result).all()

    def test_exact_window_minus_one_all_nan(self):
        close = [100.0, 101.0, 102.0]  # length 3, window 4
        result = run_indicator(HISTORICAL_VOL_CODE, {"window": 4}, _series(close))
        assert result.shape == (3,)
        assert np.isnan(result).all()

    def test_single_element(self):
        close = [100.0]
        result = run_indicator(HISTORICAL_VOL_CODE, {"window": 3}, _series(close))
        assert result.shape == (1,)
        assert np.isnan(result[0])


class TestDefaultWindow:
    """Verify the indicator works with the default window=20 on longer input."""

    def test_default_window_on_long_series(self):
        # Generate a monotonic-ish series with 100 values
        rng = np.random.default_rng(42)
        close = 100.0 + np.cumsum(rng.standard_normal(100) * 0.5)
        result = run_indicator(HISTORICAL_VOL_CODE, {"window": 20}, _series(close))

        assert result.shape == (100,)
        assert result.dtype == np.float64
        # First 19 entries are NaN
        assert np.isnan(result[:19]).all()
        # From index 19 onward, all valid
        assert not np.isnan(result[19:]).any()
        # All valid values should be non-negative (std is non-negative)
        assert (result[19:] >= 0.0).all()

    def test_default_window_matches_reference(self):
        rng = np.random.default_rng(123)
        close = 100.0 + np.cumsum(rng.standard_normal(200) * 0.3)
        result = run_indicator(HISTORICAL_VOL_CODE, {"window": 20}, _series(close))
        expected = _expected_hvol(close, 20)
        np.testing.assert_allclose(result, expected, rtol=1e-10, equal_nan=True)
