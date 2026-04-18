"""Unit tests for the restricted indicator execution sandbox.

Covers: happy path, NaN padding, disallowed imports, missing `compute`,
dunder access rejection, wrong return length, wrong return type,
typed-signature validation, and exact param/kwargs matching.
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.indicator_exec import (
    IndicatorRuntimeError,
    IndicatorValidationError,
    run_indicator,
)


SMA_CODE = (
    "def compute(series, window: int = 3):\n"
    "    s = next(iter(series.values()))\n"
    "    out = np.full_like(s, np.nan, dtype=float)\n"
    "    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')\n"
    "    return out\n"
)


def _series(values: list[float]) -> dict[str, np.ndarray]:
    return {"SPX": np.asarray(values, dtype=np.float64)}


# ── Happy path ─────────────────────────────────────────────────────────


class TestHappyPath:
    def test_sma_produces_expected_values(self):
        series = _series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = run_indicator(SMA_CODE, {"window": 3}, series)

        # Leading (w-1) = 2 positions are NaN
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        # Tail values are the 3-period rolling mean
        np.testing.assert_allclose(
            result[2:], [2.0, 3.0, 4.0], rtol=0, atol=1e-12
        )

    def test_output_length_matches_input(self):
        series = _series([10.0] * 20)
        result = run_indicator(SMA_CODE, {"window": 5}, series)
        assert result.shape == (20,)

    def test_result_dtype_is_float64(self):
        series = _series([1.0, 2.0, 3.0, 4.0])
        result = run_indicator(SMA_CODE, {"window": 2}, series)
        assert result.dtype == np.float64

    def test_list_return_converted_to_array(self):
        code = (
            "def compute(series):\n"
            "    s = next(iter(series.values()))\n"
            "    return [float(x) * 2 for x in s]\n"
        )
        series = _series([1.0, 2.0, 3.0])
        result = run_indicator(code, {}, series)
        np.testing.assert_allclose(result, [2.0, 4.0, 6.0])
        assert result.dtype == np.float64

    def test_integer_dtype_return_coerced(self):
        code = (
            "def compute(series):\n"
            "    s = next(iter(series.values()))\n"
            "    return np.arange(len(s), dtype=np.int64)\n"
        )
        series = _series([1.0, 2.0, 3.0])
        result = run_indicator(code, {}, series)
        assert result.dtype == np.float64
        np.testing.assert_allclose(result, [0.0, 1.0, 2.0])

    def test_typed_signature_with_int_float_bool(self):
        code = (
            "def compute(series, window: int = 2, scale: float = 1.5, "
            "flag: bool = False):\n"
            "    s = next(iter(series.values()))\n"
            "    out = s * scale\n"
            "    if flag:\n"
            "        out = out + float(window)\n"
            "    return out\n"
        )
        series = _series([1.0, 2.0, 3.0])
        r = run_indicator(
            code, {"window": 4, "scale": 2.0, "flag": True}, series
        )
        np.testing.assert_allclose(r, [6.0, 8.0, 10.0])

    def test_series_annotation_dict_allowed(self):
        code = (
            "def compute(series: dict, window: int = 2):\n"
            "    s = next(iter(series.values()))\n"
            "    return s * float(window)\n"
        )
        series = _series([1.0, 2.0, 3.0])
        r = run_indicator(code, {"window": 3}, series)
        np.testing.assert_allclose(r, [3.0, 6.0, 9.0])

    def test_bool_param_flows_through(self):
        code = (
            "def compute(series, use_log: bool = False):\n"
            "    s = next(iter(series.values()))\n"
            "    if use_log:\n"
            "        return np.log(s)\n"
            "    return s\n"
        )
        series = _series([1.0, np.e, np.e * np.e])
        r = run_indicator(code, {"use_log": True}, series)
        np.testing.assert_allclose(r, [0.0, 1.0, 2.0], atol=1e-12)


# ── NaN padding (lookback window) ──────────────────────────────────────


class TestNaNPadding:
    @pytest.mark.parametrize("window", [2, 3, 5, 10])
    def test_leading_positions_are_nan(self, window: int):
        series = _series(list(range(1, 21)))
        result = run_indicator(SMA_CODE, {"window": window}, series)
        assert np.isnan(result[: window - 1]).all()
        assert not np.isnan(result[window - 1 :]).any()


# ── Signature validation ───────────────────────────────────────────────


class TestSignatureValidation:
    def test_kwargs_rejected(self):
        code = (
            "def compute(series, **params):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="kwargs"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_args_rejected(self):
        code = (
            "def compute(series, *args):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match=r"\*args"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_unannotated_param_rejected(self):
        code = (
            "def compute(series, window=20):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="annotated"):
            run_indicator(code, {"window": 20}, _series([1.0, 2.0]))

    def test_non_whitelisted_annotation_rejected(self):
        code = (
            "def compute(series, window: str = 'x'):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="int, float, or bool"):
            run_indicator(code, {"window": 1}, _series([1.0, 2.0]))

    def test_missing_default_rejected(self):
        code = (
            "def compute(series, window: int):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="default"):
            run_indicator(code, {"window": 20}, _series([1.0, 2.0]))

    def test_wrong_type_default_rejected(self):
        code = (
            "def compute(series, window: int = 1.5):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="int default"):
            run_indicator(code, {"window": 1}, _series([1.0, 2.0]))

    def test_default_value_not_a_literal_rejected(self):
        # `window: int = abs(-1)` — default is a Call, not an ast.Constant.
        code = (
            "def compute(series, window: int = abs(-1)):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="literal"):
            run_indicator(code, {"window": 1}, _series([1.0, 2.0]))

    def test_kwonly_args_rejected(self):
        code = (
            "def compute(series, *, window: int = 3):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="keyword-only"):
            run_indicator(code, {"window": 3}, _series([1.0, 2.0]))

    def test_first_arg_not_named_series_rejected(self):
        code = (
            "def compute(data, window: int = 3):\n"
            "    return next(iter(data.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="series"):
            run_indicator(code, {"window": 3}, _series([1.0, 2.0]))

    def test_series_bad_annotation_rejected(self):
        code = (
            "def compute(series: list, window: int = 3):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="dict"):
            run_indicator(code, {"window": 3}, _series([1.0, 2.0]))

    def test_dunder_parameter_name_rejected(self):
        # Belt-and-braces: a param named ``__class__`` must be rejected at the
        # signature level even if the body never references it directly.
        # This is separate from the AST body-walker which blocks *name references*.
        code = (
            "def compute(series, __class__: int = 0):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="_"):
            run_indicator(code, {"__class__": 0}, _series([1.0, 2.0]))


# ── Param / kwargs matching ────────────────────────────────────────────


class TestParamMatching:
    def test_extra_param_rejected(self):
        code = (
            "def compute(series, window: int = 3):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="unexpected"):
            run_indicator(
                code, {"window": 3, "other": 1}, _series([1.0, 2.0])
            )

    def test_missing_param_rejected(self):
        code = (
            "def compute(series, window: int = 3, scale: float = 1.0):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="missing"):
            run_indicator(code, {"window": 3}, _series([1.0, 2.0]))

    def test_bool_value_for_int_param_rejected(self):
        code = (
            "def compute(series, window: int = 3):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="bool"):
            run_indicator(code, {"window": True}, _series([1.0, 2.0]))

    def test_non_integer_float_for_int_param_rejected(self):
        code = (
            "def compute(series, window: int = 3):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="non-integer"):
            run_indicator(code, {"window": 1.5}, _series([1.0, 2.0]))

    def test_integer_valued_float_accepted_for_int_param(self):
        code = (
            "def compute(series, window: int = 3):\n"
            "    s = next(iter(series.values()))\n"
            "    return s * float(window)\n"
        )
        r = run_indicator(code, {"window": 2.0}, _series([1.0, 2.0]))
        np.testing.assert_allclose(r, [2.0, 4.0])

    def test_non_bool_for_bool_param_rejected(self):
        code = (
            "def compute(series, flag: bool = False):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="bool"):
            run_indicator(code, {"flag": 1}, _series([1.0, 2.0]))


# ── Disallowed code (security) ─────────────────────────────────────────


class TestDisallowedCode:
    def test_import_statement_rejected(self):
        code = (
            "import os\n"
            "def compute(series):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="import"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_from_import_rejected(self):
        code = (
            "from math import sqrt\n"
            "def compute(series):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="import"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_dunder_attribute_rejected(self):
        code = (
            "def compute(series):\n"
            "    s = next(iter(series.values()))\n"
            "    cls = s.__class__\n"
            "    return s\n"
        )
        with pytest.raises(IndicatorValidationError, match="_"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_dunder_name_rejected(self):
        # Access to __builtins__ directly must be blocked.
        code = (
            "def compute(series):\n"
            "    x = __builtins__\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="_"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_eval_call_rejected(self):
        code = (
            "def compute(series):\n"
            "    eval('1+1')\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="eval"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_exec_call_rejected(self):
        code = (
            "def compute(series):\n"
            "    exec('x = 1')\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="exec"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_open_call_rejected(self):
        code = (
            "def compute(series):\n"
            "    open('/etc/passwd')\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(IndicatorValidationError, match="open"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_missing_compute_rejected(self):
        code = (
            "def other(series):\n"
            "    return next(iter(series.values()))\n"
        )
        with pytest.raises(
            IndicatorValidationError, match="compute"
        ):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_syntax_error_reported(self):
        with pytest.raises(IndicatorValidationError, match="syntax"):
            run_indicator("def compute(", {}, _series([1.0, 2.0]))


# ── Return type / shape validation ─────────────────────────────────────


class TestReturnValidation:
    def test_wrong_length_rejected(self):
        code = (
            "def compute(series):\n"
            "    s = next(iter(series.values()))\n"
            "    return s[:-1]  # drop one element\n"
        )
        with pytest.raises(IndicatorRuntimeError, match="length"):
            run_indicator(code, {}, _series([1.0, 2.0, 3.0, 4.0]))

    def test_string_return_rejected(self):
        code = (
            "def compute(series):\n"
            "    return 'hello'\n"
        )
        with pytest.raises(IndicatorRuntimeError, match="str"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_scalar_return_rejected(self):
        code = (
            "def compute(series):\n"
            "    return 42.0\n"
        )
        with pytest.raises(IndicatorRuntimeError):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_2d_array_rejected(self):
        code = (
            "def compute(series):\n"
            "    s = next(iter(series.values()))\n"
            "    return np.stack([s, s])\n"
        )
        with pytest.raises(IndicatorRuntimeError, match="1-D"):
            run_indicator(code, {}, _series([1.0, 2.0]))


# ── Runtime errors from user code ──────────────────────────────────────


class TestUserRuntimeError:
    def test_user_exception_wrapped(self):
        # Use a deliberate runtime failure that does not need a name
        # lookup (ValueError etc. aren't in the builtins whitelist).
        code = (
            "def compute(series):\n"
            "    x = [0][5]  # IndexError\n"
            "    return x\n"
        )
        with pytest.raises(IndicatorRuntimeError, match="IndexError"):
            run_indicator(code, {}, _series([1.0, 2.0]))

    def test_mismatched_series_lengths_rejected(self):
        with pytest.raises(IndicatorValidationError, match="same length"):
            run_indicator(
                SMA_CODE,
                {"window": 2},
                {
                    "A": np.array([1.0, 2.0, 3.0]),
                    "B": np.array([1.0, 2.0]),
                },
            )

    def test_empty_series_dict_rejected(self):
        with pytest.raises(IndicatorValidationError, match="at least one"):
            run_indicator(SMA_CODE, {"window": 2}, {})
