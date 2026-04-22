"""Unit tests for tcg/core/api/_models.py — SeriesRef discriminated union."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tcg.core.api._models import (
    ContinuousInstrumentRef,
    SeriesRef,
    SpotInstrumentRef,
)


# TypeAdapter lets us validate the Annotated union directly.
_adapter = TypeAdapter(SeriesRef)


class TestSpotInstrumentRef:
    def test_happy_path(self):
        data = {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"}
        ref = _adapter.validate_python(data)
        assert isinstance(ref, SpotInstrumentRef)
        assert ref.type == "spot"
        assert ref.collection == "INDEX"
        assert ref.instrument_id == "SPX"

    def test_missing_instrument_id_raises(self):
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "spot", "collection": "INDEX"})

    def test_missing_collection_raises(self):
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "spot", "instrument_id": "SPX"})


class TestContinuousInstrumentRef:
    def test_happy_path_minimal(self):
        data = {"type": "continuous", "collection": "FUT_VX"}
        ref = _adapter.validate_python(data)
        assert isinstance(ref, ContinuousInstrumentRef)
        assert ref.type == "continuous"
        assert ref.collection == "FUT_VX"
        # Defaults
        assert ref.adjustment == "none"
        assert ref.cycle is None
        assert ref.rollOffset == 0
        assert ref.strategy == "front_month"

    def test_happy_path_full(self):
        data = {
            "type": "continuous",
            "collection": "FUT_VX",
            "adjustment": "proportional",
            "cycle": "HMUZ",
            "rollOffset": 2,
            "strategy": "front_month",
        }
        ref = _adapter.validate_python(data)
        assert isinstance(ref, ContinuousInstrumentRef)
        assert ref.adjustment == "proportional"
        assert ref.cycle == "HMUZ"
        assert ref.rollOffset == 2

    def test_difference_adjustment(self):
        data = {"type": "continuous", "collection": "FUT_ES", "adjustment": "difference"}
        ref = _adapter.validate_python(data)
        assert ref.adjustment == "difference"

    def test_invalid_adjustment_rejected(self):
        with pytest.raises(ValidationError):
            _adapter.validate_python({
                "type": "continuous",
                "collection": "FUT_VX",
                "adjustment": "unknown_method",
            })

    def test_invalid_strategy_rejected(self):
        with pytest.raises(ValidationError):
            _adapter.validate_python({
                "type": "continuous",
                "collection": "FUT_VX",
                "strategy": "back_month",
            })


class TestSeriesRefDiscriminator:
    def test_unknown_type_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            _adapter.validate_python({"type": "futures", "collection": "FUT_VX"})
        # Error message should mention the bad discriminator value
        error_str = str(exc_info.value)
        assert "futures" in error_str or "discriminator" in error_str.lower() or "type" in error_str

    def test_missing_type_raises(self):
        with pytest.raises(ValidationError):
            _adapter.validate_python({"collection": "INDEX", "instrument_id": "SPX"})

    def test_spot_discriminator_selects_spot_model(self):
        ref = _adapter.validate_python({"type": "spot", "collection": "INDEX", "instrument_id": "NDX"})
        assert type(ref).__name__ == "SpotInstrumentRef"

    def test_continuous_discriminator_selects_continuous_model(self):
        ref = _adapter.validate_python({"type": "continuous", "collection": "FUT_CL"})
        assert type(ref).__name__ == "ContinuousInstrumentRef"

    def test_json_roundtrip_spot(self):
        data = {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"}
        ref = _adapter.validate_python(data)
        dumped = ref.model_dump()
        ref2 = _adapter.validate_python(dumped)
        assert ref == ref2

    def test_json_roundtrip_continuous(self):
        data = {
            "type": "continuous",
            "collection": "FUT_VX",
            "adjustment": "proportional",
            "cycle": "HMUZ",
            "rollOffset": 3,
        }
        ref = _adapter.validate_python(data)
        dumped = ref.model_dump()
        ref2 = _adapter.validate_python(dumped)
        assert ref == ref2
