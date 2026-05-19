"""Q3 smoke test — Pydantic v2 nested discriminator for BasketRef.

This is the FIRST piece of code on the BE worker's path (per Wave-P
decision). Before extending ``signals.py`` or the engine, confirm
that:

1. The outer discriminator on ``type`` correctly selects the basket
   branch.
2. The inner discriminator on ``kind`` correctly disambiguates
   ``BasketRefSaved`` vs ``BasketRefInline``.
3. ``extra="forbid"`` is enforced on both shapes (and on
   ``BasketLegInLite``).
4. The inline shape's ``legs`` honours ``min_length=1`` and the
   weight-nonzero validator.

If Pydantic v2 rejects the construction, this test would fail at
import time (Pydantic validates the Annotated discriminator wiring at
class-build time, not lazily). A failure here is a STOP condition —
escalate via PROBLEMS.md.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from typing import Annotated, Union

from tcg.core.api._models import (
    BasketLegInLite,
    BasketRef,
    BasketRefInline,
    BasketRefSaved,
    SeriesRef,
)


# A minimal harness for round-tripping a SeriesRef payload — mirrors how
# the field is used inside ``_InputIn.instrument`` in signals.py.
_SeriesRefAdapter = TypeAdapter(SeriesRef)
_BasketRefAdapter = TypeAdapter(BasketRef)


# ---------------------------------------------------------------------------
# Q3.1 — outer + inner discriminator both fire correctly
# ---------------------------------------------------------------------------


def test_saved_basket_payload_resolves_to_basket_ref_saved() -> None:
    payload = {"type": "basket", "kind": "saved", "basket_id": "MY_BASKET"}
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefSaved)
    assert parsed.basket_id == "MY_BASKET"
    assert parsed.kind == "saved"
    assert parsed.type == "basket"


def test_inline_basket_payload_resolves_to_basket_ref_inline() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [
            {"instrument_id": "SPY", "weight": 0.6},
            {"instrument_id": "QQQ", "weight": 0.4},
        ],
    }
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefInline)
    assert parsed.asset_class == "equity"
    assert parsed.kind == "inline"
    assert parsed.type == "basket"
    assert len(parsed.legs) == 2
    assert parsed.legs[0].instrument_id == "SPY"
    assert parsed.legs[0].weight == 0.6


def test_inline_basket_via_basket_ref_adapter_directly() -> None:
    # Bypass the outer discriminator — exercise just the inner one.
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "future",
        "legs": [{"instrument_id": "FUT_ES_2026H", "weight": 1.0}],
    }
    parsed = _BasketRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefInline)


def test_saved_basket_via_basket_ref_adapter_directly() -> None:
    payload = {"type": "basket", "kind": "saved", "basket_id": "B1"}
    parsed = _BasketRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefSaved)


def test_missing_kind_on_basket_payload_is_rejected() -> None:
    payload = {"type": "basket", "basket_id": "B1"}
    with pytest.raises(ValidationError) as exc_info:
        _SeriesRefAdapter.validate_python(payload)
    # The error message should mention the missing discriminator key.
    assert "kind" in str(exc_info.value)


def test_unknown_kind_on_basket_payload_is_rejected() -> None:
    payload = {"type": "basket", "kind": "frobnicated", "basket_id": "B1"}
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


# ---------------------------------------------------------------------------
# Q3.2 — extra="forbid" enforced on all three basket models
# ---------------------------------------------------------------------------


def test_saved_basket_extra_field_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "saved",
        "basket_id": "B1",
        "rogue_field": "should-fail",
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_extra_field_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [{"instrument_id": "SPY", "weight": 1.0}],
        "rogue_field": "should-fail",
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_leg_extra_field_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        # The FE wire shape has no ``collection`` on inline legs.
        # If a sloppy client adds one, reject it loudly.
        "legs": [{"instrument_id": "SPY", "weight": 1.0, "collection": "ETF"}],
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
        "legs": [{"instrument_id": "SPY", "weight": 0.0}],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


def test_inline_basket_leg_negative_weight_accepted() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "equity",
        "legs": [{"instrument_id": "SPY", "weight": -1.0}],
    }
    parsed = _SeriesRefAdapter.validate_python(payload)
    assert isinstance(parsed, BasketRefInline)
    assert parsed.legs[0].weight == -1.0


def test_inline_basket_unknown_asset_class_rejected() -> None:
    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "commodity",  # not in the locked literal
        "legs": [{"instrument_id": "X", "weight": 1.0}],
    }
    with pytest.raises(ValidationError):
        _SeriesRefAdapter.validate_python(payload)


# ---------------------------------------------------------------------------
# Q3.4 — outer discriminator still routes the non-basket branches.
# Regression: adding the nested-union member must not break the simple
# branches it shares the outer union with.
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
