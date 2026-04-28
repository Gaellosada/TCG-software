"""Unit tests for ``tcg.engine.options.chain._widen``.

Module 6 is the only place where ``ComputeResult.source="stored"`` is
emitted (spec §3.6 / Appendix C.3).  The widening helpers must:

- Wrap a non-None stored value as ``source="stored"``, with ``model``,
  ``inputs_used``, ``missing_inputs``, ``error_code``, ``error_detail``
  all ``None``.
- Wrap a None stored value as ``source="missing"``, with
  ``error_code="not_stored"`` and ``missing_inputs=(<greek>,)``.
- Pass through Module 2's already-emitted ``ComputeResult`` unchanged
  when ``compute_missing=True`` and a computed result is supplied.
"""

from __future__ import annotations

import pytest

from tcg.engine.options.chain._widen import (
    merge_stored_with_computed,
    widen_stored,
)
from tcg.types.options import ComputeResult


class TestWidenStored:
    """The core stored-source widening helper."""

    def test_present_value_becomes_source_stored(self) -> None:
        result = widen_stored(0.50, greek_name="iv")
        assert result.value == 0.50
        assert result.source == "stored"
        assert result.model is None
        assert result.inputs_used is None
        assert result.missing_inputs is None
        assert result.error_code is None
        assert result.error_detail is None

    def test_none_value_becomes_source_missing_not_stored(self) -> None:
        result = widen_stored(None, greek_name="delta")
        assert result.value is None
        assert result.source == "missing"
        assert result.error_code == "not_stored"
        assert result.missing_inputs == ("delta",)
        # Sanity: not pretending to be a computed envelope.
        assert result.model is None
        assert result.inputs_used is None

    def test_zero_is_treated_as_a_valid_stored_value(self) -> None:
        # Zero IV / Greek is unusual but legal; do not coerce to missing.
        result = widen_stored(0.0, greek_name="theta")
        assert result.value == 0.0
        assert result.source == "stored"
        assert result.error_code is None

    @pytest.mark.parametrize("greek_name", ["iv", "delta", "gamma", "theta", "vega"])
    def test_missing_inputs_carries_the_greek_name(self, greek_name: str) -> None:
        result = widen_stored(None, greek_name=greek_name)
        assert result.missing_inputs == (greek_name,)


class TestMergeStoredWithComputed:
    """Stored takes precedence; computed fills in only when stored is missing."""

    def test_stored_present_short_circuits_compute(self) -> None:
        # Stored is present → computed result is ignored entirely (we do not
        # even pass it; passing one would still be honoured by precedence).
        computed = ComputeResult(
            value=0.99,
            source="computed",
            model="Black-76",
            inputs_used={"iv": 0.99},
        )
        result = merge_stored_with_computed(
            stored_value=0.42,
            greek_name="iv",
            computed=computed,
        )
        assert result.source == "stored"
        assert result.value == 0.42

    def test_stored_missing_with_computed_passes_through(self) -> None:
        computed = ComputeResult(
            value=0.31,
            source="computed",
            model="Black-76",
            inputs_used={"iv": 0.31, "ttm": 0.05, "r": 0.0},
        )
        result = merge_stored_with_computed(
            stored_value=None,
            greek_name="delta",
            computed=computed,
        )
        # Pass-through; do NOT alter the source label.
        assert result is computed
        assert result.source == "computed"
        assert result.value == 0.31

    def test_stored_missing_with_missing_computed_passes_through(self) -> None:
        computed = ComputeResult(
            value=None,
            source="missing",
            error_code="missing_underlying_price",
            missing_inputs=("underlying_price",),
        )
        result = merge_stored_with_computed(
            stored_value=None,
            greek_name="vega",
            computed=computed,
        )
        assert result is computed
        assert result.source == "missing"
        assert result.error_code == "missing_underlying_price"

    def test_stored_missing_no_computed_returns_widen_stored_none(self) -> None:
        result = merge_stored_with_computed(
            stored_value=None,
            greek_name="gamma",
            computed=None,
        )
        assert result.source == "missing"
        assert result.error_code == "not_stored"
        assert result.missing_inputs == ("gamma",)
