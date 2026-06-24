"""Unit tests for basket strict per-class mapping + duplicate-leg validators.

Iter-3 rewrite: ``_asset_class_from_collection`` was removed (the basket
envelope now declares ``asset_class`` directly); ``_check_basket_homogeneity``
now enforces the strict per-asset-class → ``instrument.type`` mapping;
``_check_basket_no_duplicates`` deduplicates on the canonical hash of the
full leg ``instrument`` spec (not just ``instrument_id``).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from tcg.core.api.persistence import (
    BasketLegIn,
    _ASSET_CLASS_TO_INSTRUMENT_TYPE,
    _check_basket_homogeneity,
    _check_basket_no_duplicates,
)


def _spot_leg(
    instrument_id: str, collection: str = "ETF", weight: float = 0.5
) -> BasketLegIn:
    return BasketLegIn(
        instrument={
            "type": "spot",
            "collection": collection,
            "instrument_id": instrument_id,
        },
        weight=weight,
    )


def _continuous_leg(
    collection: str,
    weight: float = 0.5,
    *,
    adjustment: str = "none",
    cycle: str | None = None,
    rollOffset: int = 0,
) -> BasketLegIn:
    return BasketLegIn(
        instrument={
            "type": "continuous",
            "collection": collection,
            "adjustment": adjustment,
            "cycle": cycle,
            "rollOffset": rollOffset,
            "strategy": "front_month",
        },
        weight=weight,
    )


def _option_stream_leg(collection: str = "OPT_VIX", weight: float = 0.5) -> BasketLegIn:
    return BasketLegIn(
        instrument={
            "type": "option_stream",
            "collection": collection,
            "option_type": "C",
            "cycle": None,
            "maturity": {"kind": "next_third_friday"},
            "selection": {"kind": "by_moneyness", "target": 1.0},
            "stream": "mid",
        },
        weight=weight,
    )


# ---------------------------------------------------------------------------
# _ASSET_CLASS_TO_INSTRUMENT_TYPE mapping is authoritative
# ---------------------------------------------------------------------------


def test_asset_class_to_instrument_type_mapping() -> None:
    """The four supported asset classes map to exactly one instrument
    type each — the validator reads this map verbatim."""
    assert _ASSET_CLASS_TO_INSTRUMENT_TYPE == {
        "equity": "spot",
        "index": "spot",
        "future": "continuous",
        "option": "option_stream",
    }


# ---------------------------------------------------------------------------
# _check_basket_homogeneity — strict per-class mapping
# ---------------------------------------------------------------------------


def test_equity_with_spot_legs_passes() -> None:
    legs = [_spot_leg("SPY"), _spot_leg("QQQ")]
    _check_basket_homogeneity("equity", legs)  # should not raise


def test_index_with_spot_legs_passes() -> None:
    legs = [_spot_leg("SPX", collection="INDEX")]
    _check_basket_homogeneity("index", legs)


def test_future_with_continuous_legs_passes() -> None:
    legs = [_continuous_leg("FUT_VIX"), _continuous_leg("FUT_ES")]
    _check_basket_homogeneity("future", legs)


def test_option_with_option_stream_legs_passes() -> None:
    legs = [_option_stream_leg()]
    _check_basket_homogeneity("option", legs)


def test_equity_with_continuous_leg_raises_400_with_leg_index() -> None:
    """Iter-3 strict mismatch: equity basket cannot carry a continuous leg."""
    legs = [_spot_leg("SPY"), _continuous_leg("FUT_VIX")]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_homogeneity("equity", legs)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "leg 1" in detail
    assert "spot" in detail and "continuous" in detail
    assert "equity" in detail


def test_future_with_spot_leg_raises_400_with_leg_index() -> None:
    legs = [_continuous_leg("FUT_VIX"), _spot_leg("SPY")]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_homogeneity("future", legs)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "leg 1" in detail
    assert "continuous" in detail
    assert "future" in detail


def test_option_with_continuous_leg_raises_400_with_leg_index() -> None:
    legs = [_option_stream_leg(), _continuous_leg("FUT_VIX")]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_homogeneity("option", legs)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "leg 1" in detail


def test_empty_legs_passes() -> None:
    """Empty basket is valid — supports saving partial work."""
    _check_basket_homogeneity("equity", [])


def test_single_leg_passes() -> None:
    legs = [_spot_leg("SPY", weight=1.0)]
    _check_basket_homogeneity("equity", legs)


# ---------------------------------------------------------------------------
# _check_basket_no_duplicates — dedup by full instrument spec + weight
# ---------------------------------------------------------------------------


def test_no_duplicates_passes() -> None:
    legs = [_spot_leg("SPY"), _spot_leg("QQQ")]
    _check_basket_no_duplicates(legs)


def test_duplicate_spot_legs_with_same_weight_rejected() -> None:
    """Two structurally-identical spot legs with the same weight = duplicate."""
    legs = [_spot_leg("SPY", weight=0.5), _spot_leg("SPY", weight=0.5)]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_no_duplicates(legs)
    assert exc_info.value.status_code == 400


def test_same_instrument_different_weights_not_duplicate() -> None:
    """Two legs with the same instrument but DIFFERENT weights are NOT
    duplicates — the user may be expressing a directional layering."""
    legs = [_spot_leg("SPY", weight=0.3), _spot_leg("SPY", weight=0.5)]
    _check_basket_no_duplicates(legs)  # should not raise


def test_continuous_legs_distinguished_by_adjustment() -> None:
    """Two continuous legs on the same collection with different
    adjustment must NOT be flagged as duplicates."""
    legs = [
        _continuous_leg("FUT_VIX", adjustment="none", weight=0.5),
        _continuous_leg("FUT_VIX", adjustment="ratio", weight=0.5),
    ]
    _check_basket_no_duplicates(legs)


def test_continuous_legs_same_full_spec_and_weight_rejected() -> None:
    """Two continuous legs with identical adjustment/cycle/rollOffset
    AND the same weight ARE duplicates."""
    legs = [
        _continuous_leg("FUT_VIX", adjustment="ratio", cycle="HMUZ", weight=0.5),
        _continuous_leg("FUT_VIX", adjustment="ratio", cycle="HMUZ", weight=0.5),
    ]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_no_duplicates(legs)
    assert exc_info.value.status_code == 400


def test_empty_no_duplicates_passes() -> None:
    _check_basket_no_duplicates([])


# ---------------------------------------------------------------------------
# BasketLegIn — weight non-zero validator
# ---------------------------------------------------------------------------


def test_basket_leg_zero_weight_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BasketLegIn(
            instrument={
                "type": "spot",
                "collection": "ETF",
                "instrument_id": "SPY",
            },
            weight=0.0,
        )


def test_basket_leg_negative_weight_allowed() -> None:
    leg = BasketLegIn(
        instrument={
            "type": "spot",
            "collection": "ETF",
            "instrument_id": "SPY",
        },
        weight=-0.5,
    )
    assert leg.weight == -0.5
    assert leg.instrument.type == "spot"


def test_basket_leg_extra_field_on_leg_rejected() -> None:
    """``extra='forbid'`` on the leg envelope."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BasketLegIn(
            instrument={
                "type": "spot",
                "collection": "ETF",
                "instrument_id": "SPY",
            },
            weight=1.0,
            junk="x",
        )


# ---------------------------------------------------------------------------
# Inline-basket option_stream leg carries adjustment / roll_offset
# (the MAJOR review finding — wire model parity with continuous legs)
# ---------------------------------------------------------------------------


def test_inline_option_stream_leg_threads_roll_offset_and_ignores_adjustment() -> None:
    """The inline-basket wire model (``_models.BasketLeg``) threads
    ``roll_offset`` on an option_stream leg, mirroring the continuous-leg field.
    Option streams carry no back-adjustment, so a stray ``adjustment`` key is
    ignored (extra fields are dropped on ``OptionStreamRef``)."""
    from tcg.core.api._models import BasketLeg

    leg = BasketLeg(
        instrument={
            "type": "option_stream",
            "collection": "OPT_VIX",
            "option_type": "C",
            "cycle": None,
            "maturity": {"kind": "next_third_friday", "offset_months": 0},
            "selection": {"kind": "by_moneyness", "target": 1.0, "tolerance": 0.01},
            "stream": "mid",
            "adjustment": "ratio",
            "roll_offset": 5,
        },
        weight=0.5,
    )
    assert not hasattr(leg.instrument, "adjustment")
    assert leg.instrument.roll_offset == 5


def test_inline_option_stream_leg_defaults_when_absent() -> None:
    """Absent roll_offset defaults to 0 (additive — old inline baskets
    unchanged); option_stream legs never carry an ``adjustment``."""
    from tcg.core.api._models import BasketLeg

    leg = BasketLeg(
        instrument={
            "type": "option_stream",
            "collection": "OPT_VIX",
            "option_type": "C",
            "cycle": None,
            "maturity": {"kind": "next_third_friday", "offset_months": 0},
            "selection": {"kind": "by_moneyness", "target": 1.0, "tolerance": 0.01},
            "stream": "mid",
        },
        weight=0.5,
    )
    assert not hasattr(leg.instrument, "adjustment")
    assert leg.instrument.roll_offset == 0


def test_inline_option_stream_leg_roll_offset_out_of_range_rejected() -> None:
    """``OptionStreamRef`` bounds roll_offset to 0..30 on the inline path."""
    from pydantic import ValidationError

    from tcg.core.api._models import BasketLeg

    with pytest.raises(ValidationError):
        BasketLeg(
            instrument={
                "type": "option_stream",
                "collection": "OPT_VIX",
                "option_type": "C",
                "cycle": None,
                "maturity": {"kind": "next_third_friday", "offset_months": 0},
                "selection": {"kind": "by_moneyness", "target": 1.0, "tolerance": 0.01},
                "stream": "mid",
                "roll_offset": 31,
            },
            weight=0.5,
        )
