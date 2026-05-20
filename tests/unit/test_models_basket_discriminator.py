"""Q3 smoke + iter-3 polymorphic-leg discriminator tests for BasketRef.

Iter-1 confirmed the outer SeriesRef union runs through a callable
``Discriminator(_series_ref_discriminator)`` because FastAPI's OpenAPI
3.0 emitter can't handle nested discriminators *as members of another
discriminated union*.

Iter-3 adds a nested standard ``Field(discriminator="type")`` on
:class:`BasketLeg.instrument` over ``Spot | Continuous | OptionStream``.
That nested discriminator is one level deep (its members are not
themselves discriminated unions) so it does NOT trigger the OpenAPI bug
— the standard Annotated/Field discriminator works.  This file
smoke-tests the leg-level dispatch on every supported leg.instrument.type
and verifies the outer SeriesRef discriminator still routes the
non-basket branches.

Tests intentionally exercise the strict per-class mapping at the model
level (``model_validator`` on :class:`BasketRefInline`) — a mismatched
(asset_class, leg.instrument.type) raises ValidationError at
Pydantic-validation time, BEFORE the request hits any route handler.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tcg.core.api._models import (
    BasketLeg,
    BasketRef,
    BasketRefInline,
    BasketRefSaved,
    SeriesRef,
)


_SeriesRefAdapter = TypeAdapter(SeriesRef)
_BasketRefAdapter = TypeAdapter(BasketRef)
_BasketLegAdapter = TypeAdapter(BasketLeg)


# ---------------------------------------------------------------------------
# Q3.1 — outer + inner discriminator both fire
# ---------------------------------------------------------------------------


def test_saved_basket_payload_resolves_to_basket_ref_saved() -> None:
    payload = {"type": "basket", "kind": "saved", "basket_id": "MY_BASKET"}
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefSaved)
    assert parsed.basket_id == "MY_BASKET"


def test_inline_basket_with_spot_legs_resolves() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "SPY",
                },
                "weight": 0.6,
            },
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "QQQ",
                },
                "weight": 0.4,
            },
        ],
    }
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefInline)
    assert parsed.asset_class == "equity"
    assert len(parsed.legs) == 2
    assert parsed.legs[0].instrument.type == "spot"
    assert parsed.legs[0].instrument.instrument_id == "SPY"  # type: ignore[union-attr]


def test_inline_basket_with_continuous_legs_resolves() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "future",
        "legs": [
            {
                "instrument": {
                    "type": "continuous",
                    "collection": "FUT_ES",
                    "adjustment": "ratio",
                    "cycle": "HMUZ",
                    "rollOffset": 0,
                    "strategy": "front_month",
                },
                "weight": 1.0,
            }
        ],
    }
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefInline)
    assert parsed.legs[0].instrument.type == "continuous"
    assert parsed.legs[0].instrument.adjustment == "ratio"  # type: ignore[union-attr]


def test_inline_basket_with_option_stream_legs_resolves() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "option",
        "legs": [
            {
                "instrument": {
                    "type": "option_stream",
                    "collection": "OPT_SP_500",
                    "option_type": "C",
                    "cycle": None,
                    "maturity": {"kind": "next_third_friday"},
                    "selection": {"kind": "by_moneyness", "target": 1.0},
                    "stream": "mid",
                },
                "weight": 1.0,
            }
        ],
    }
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefInline)
    assert parsed.legs[0].instrument.type == "option_stream"


def test_missing_kind_on_basket_payload_is_rejected() -> None:
    payload = {"type": "basket", "basket_id": "B1"}
    with pytest.raises(ValidationError) as exc_info:
        _SeriesRefAdapter.validate_python(payload)
    error = exc_info.value.errors()[0]
    assert error["type"] == "union_tag_not_found"


def test_unknown_kind_on_basket_payload_is_rejected() -> None:
    payload = {"type": "basket", "kind": "frobnicated", "basket_id": "B1"}
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_missing_leg_instrument_type_is_rejected() -> None:
    """The leg-level ``Field(discriminator="type")`` must reject a leg
    whose ``instrument`` payload omits ``type``."""
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {"collection": "ETF", "instrument_id": "SPY"},
                "weight": 1.0,
            }
        ],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_unknown_leg_instrument_type_is_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {"instrument": {"type": "future_legacy"}, "weight": 1.0}
        ],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


# ---------------------------------------------------------------------------
# Q3.2 — extra="forbid" on all basket models
# ---------------------------------------------------------------------------


def test_saved_basket_extra_field_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "saved",
        "basket_id": "B1",
        "rogue_field": "x",
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_extra_field_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "SPY",
                },
                "weight": 1.0,
            }
        ],
        "rogue_field": "x",
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_leg_extra_field_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "SPY",
                },
                "weight": 1.0,
                "junk": "x",  # extra at the leg envelope
            }
        ],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_leg_instrument_unknown_type_rejected() -> None:
    """Sanity check that the standard ``Field(discriminator="type")``
    on the leg-level union rejects unknown discriminator tags.  This
    is the iter-3 standard-discriminator regression — different shape
    from the outer callable-Discriminator coverage above."""
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {"type": "spot_legacy", "x": "y"},
                "weight": 1.0,
            }
        ],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


# ---------------------------------------------------------------------------
# Q3.3 — leg-level constraints (min_length, weight!=0)
# ---------------------------------------------------------------------------


def test_inline_basket_zero_legs_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_leg_zero_weight_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "SPY",
                },
                "weight": 0.0,
            }
        ],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_leg_negative_weight_accepted() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "SPY",
                },
                "weight": -1.0,
            }
        ],
    }
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefInline)
    assert parsed.legs[0].weight == -1.0


def test_inline_basket_unknown_asset_class_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "commodity",
        "legs": [
            {
                "instrument": {
                    "type": "spot",
                    "collection": "X",
                    "instrument_id": "Y",
                },
                "weight": 1.0,
            }
        ],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


# ---------------------------------------------------------------------------
# Iter-3 strict per-class mapping at the model level (model_validator)
# ---------------------------------------------------------------------------


def test_equity_with_continuous_leg_rejected_by_model_validator() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {
                "instrument": {
                    "type": "continuous",
                    "collection": "FUT_ES",
                    "cycle": "HMUZ",
                },
                "weight": 1.0,
            }
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        _SeriesRefAdapter.validate_python(payload)
    msg = str(exc_info.value)
    assert "leg 0" in msg
    assert "spot" in msg and "continuous" in msg


def test_future_with_spot_leg_rejected_by_model_validator() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "future",
        "legs": [
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "SPY",
                },
                "weight": 1.0,
            }
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        _SeriesRefAdapter.validate_python(payload)
    assert "leg 0" in str(exc_info.value)


def test_option_with_continuous_leg_rejected_by_model_validator() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "option",
        "legs": [
            {
                "instrument": {
                    "type": "continuous",
                    "collection": "FUT_VIX",
                },
                "weight": 1.0,
            }
        ],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_mismatch_detail_names_leg_index() -> None:
    """Mismatch on the second leg names ``leg 1`` (zero-indexed)."""
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "future",
        "legs": [
            {
                "instrument": {
                    "type": "continuous",
                    "collection": "FUT_ES",
                },
                "weight": 0.5,
            },
            {
                "instrument": {
                    "type": "spot",
                    "collection": "ETF",
                    "instrument_id": "SPY",
                },
                "weight": 0.5,
            },
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        _SeriesRefAdapter.validate_python(payload)
    assert "leg 1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Q3.4 — outer discriminator still routes non-basket branches
# ---------------------------------------------------------------------------


def test_spot_ref_still_routes_via_outer_discriminator() -> None:
    from tcg.core.api._models import SpotInstrumentRef

    payload = {"type": "spot", "collection": "ETF", "instrument_id": "SPY"}
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, SpotInstrumentRef)


def test_continuous_ref_still_routes_via_outer_discriminator() -> None:
    from tcg.core.api._models import ContinuousInstrumentRef

    payload = {"type": "continuous", "collection": "FUT_ES", "cycle": "H"}
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, ContinuousInstrumentRef)
