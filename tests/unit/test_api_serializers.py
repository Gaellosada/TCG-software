"""Unit tests for tcg/core/api/_serializers.py::nan_safe_floats."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tcg.core.api._serializers import nan_safe_floats, sanitize_json_floats


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


# ---------------------------------------------------------------------------
# #6 — recursive NaN/inf JSON sanitizer for nested dicts / lists / scalars.
#
# Bare ``NaN`` / ``Infinity`` violate the project's RFC-8259 finite-JSON
# invariant. ``sanitize_json_floats`` maps every non-finite float (NaN,
# +inf, -inf) to ``None`` while walking dicts / lists recursively, leaving
# finite floats, ints, strings, bools, and None untouched.
# ---------------------------------------------------------------------------


class TestSanitizeJsonFloatsScalars:
    def test_nan_scalar_to_none(self):
        assert sanitize_json_floats(float("nan")) is None

    def test_pos_inf_scalar_to_none(self):
        assert sanitize_json_floats(float("inf")) is None

    def test_neg_inf_scalar_to_none(self):
        assert sanitize_json_floats(float("-inf")) is None

    def test_finite_float_passes_through(self):
        assert sanitize_json_floats(3.14) == 3.14

    def test_int_passes_through_unchanged(self):
        # ``bool`` is a subclass of int — must NOT be coerced to float.
        assert sanitize_json_floats(5) == 5
        assert sanitize_json_floats(True) is True

    def test_str_and_none_pass_through(self):
        assert sanitize_json_floats("period") == "period"
        assert sanitize_json_floats(None) is None

    def test_numpy_nan_scalar_to_none(self):
        assert sanitize_json_floats(np.float64("nan")) is None


class TestSanitizeJsonFloatsNested:
    def test_dict_with_nan_value(self):
        out = sanitize_json_floats({"sharpe": float("nan"), "cagr": 0.1})
        assert out == {"sharpe": None, "cagr": 0.1}

    def test_list_with_inf(self):
        out = sanitize_json_floats([1.0, float("inf"), 2.0])
        assert out == [1.0, None, 2.0]

    def test_metrics_like_block(self):
        block = {
            "period": "2024-01",
            "portfolio": float("nan"),
            "A": 0.01,
            "nested": {"x": float("-inf"), "y": [float("nan"), 3.0]},
        }
        out = sanitize_json_floats(block)
        assert out == {
            "period": "2024-01",
            "portfolio": None,
            "A": 0.01,
            "nested": {"x": None, "y": [None, 3.0]},
        }

    def test_does_not_mutate_input(self):
        src = {"a": float("nan")}
        sanitize_json_floats(src)
        assert math.isnan(src["a"])  # original untouched

    def test_full_portfolio_response_shape_is_finite_or_null(self):
        """Contract guard for the portfolio compute response: the WHOLE
        payload is wrapped in ``sanitize_json_floats`` (RFC-8259 invariant),
        so every float block — equity curves, per-leg equities, raw (buy-and
        -hold) leg equities, tracking-series values, trade weights — must end
        up finite or ``null``. A representative response with NaN/inf in each
        of those blocks must contain no remaining non-finite float.
        """
        response = {
            "dates": ["2024-01-02", "2024-01-03"],
            "portfolio_equity": [100.0, float("inf")],
            "leg_equities": {"A": [50.0, float("nan")]},
            "raw_leg_equities": {"A": [50.0, float("nan")], "B": [float("-inf"), 1.0]},
            "tracking_series": {"IV": {"label": "IV", "values": [float("inf"), 0.2]}},
            "trades": [{"holding_id": "A", "signed_weight": float("nan")}],
            "metrics": {"sharpe_ratio": float("inf"), "num_trades": 3},
            "return_type": "normal",
        }
        out = sanitize_json_floats(response)

        def _no_nonfinite(v: object) -> bool:
            if isinstance(v, bool):
                return True
            if isinstance(v, float):
                return math.isfinite(v)
            if isinstance(v, dict):
                return all(_no_nonfinite(x) for x in v.values())
            if isinstance(v, (list, tuple)):
                return all(_no_nonfinite(x) for x in v)
            return True

        assert _no_nonfinite(out), out
        # Spot-check the previously-unsanitized blocks specifically.
        assert out["portfolio_equity"] == [100.0, None]
        assert out["leg_equities"]["A"] == [50.0, None]
        assert out["raw_leg_equities"]["B"] == [None, 1.0]
        assert out["tracking_series"]["IV"]["values"] == [None, 0.2]
        assert out["trades"][0]["signed_weight"] is None
        # Non-float scalars survive.
        assert out["metrics"]["num_trades"] == 3
        assert out["return_type"] == "normal"
