"""Unit tests for tcg/core/api/_serializers.py::nan_safe_floats."""

from __future__ import annotations

import numpy as np
import pytest

from tcg.core.api._serializers import nan_safe_floats


class TestNanSafeFloatsHappyPath:
    def test_finite_floats_pass_through(self):
        arr = np.array([1.0, 2.5, -3.14, 0.0])
        result = nan_safe_floats(arr)
        assert result == [1.0, 2.5, -3.14, 0.0]

    def test_single_element(self):
        arr = np.array([42.0])
        result = nan_safe_floats(arr)
        assert result == [42.0]

    def test_empty_array(self):
        arr = np.array([], dtype=np.float64)
        result = nan_safe_floats(arr)
        assert result == []

    def test_none_input_returns_empty_list(self):
        result = nan_safe_floats(None)
        assert result == []


class TestNanHandling:
    def test_nan_becomes_none(self):
        arr = np.array([float("nan")])
        result = nan_safe_floats(arr)
        assert result == [None]

    def test_nan_in_array_becomes_none(self):
        arr = np.array([1.0, float("nan"), 3.0])
        result = nan_safe_floats(arr)
        assert result[0] == 1.0
        assert result[1] is None
        assert result[2] == 3.0

    def test_all_nan_becomes_all_none(self):
        arr = np.array([float("nan"), float("nan"), float("nan")])
        result = nan_safe_floats(arr)
        assert result == [None, None, None]

    def test_numpy_nan_constant(self):
        arr = np.array([np.nan, 1.0])
        result = nan_safe_floats(arr)
        assert result[0] is None
        assert result[1] == 1.0


class TestInfHandling:
    def test_positive_inf_passes_through_as_float(self):
        """inf is not NaN — NaN check (v != v) does not catch it."""
        arr = np.array([float("inf")])
        result = nan_safe_floats(arr)
        # inf is not NaN so it passes through as float
        assert result[0] == float("inf")

    def test_negative_inf_passes_through_as_float(self):
        arr = np.array([float("-inf")])
        result = nan_safe_floats(arr)
        assert result[0] == float("-inf")


class TestMixedArrays:
    def test_mixed_nan_finite_inf(self):
        arr = np.array([1.0, float("nan"), float("inf"), -2.5, float("nan")])
        result = nan_safe_floats(arr)
        assert result[0] == 1.0
        assert result[1] is None
        assert result[2] == float("inf")
        assert result[3] == -2.5
        assert result[4] is None

    def test_large_array_preserves_order(self):
        raw = list(range(100))
        raw[50] = float("nan")
        arr = np.array(raw, dtype=np.float64)
        result = nan_safe_floats(arr)
        assert len(result) == 100
        assert result[50] is None
        assert result[0] == 0.0
        assert result[99] == 99.0

    def test_result_is_list(self):
        arr = np.array([1.0, 2.0])
        result = nan_safe_floats(arr)
        assert isinstance(result, list)

    def test_values_are_python_floats_or_none(self):
        arr = np.array([1.0, float("nan"), 3.0])
        result = nan_safe_floats(arr)
        for val in result:
            assert val is None or isinstance(val, float)
