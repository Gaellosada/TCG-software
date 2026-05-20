"""Unit tests for ``BasketDoc`` round-trip through Mongo serde — iter-3 polymorphic leg shape."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tcg.types.persistence import (
    BasketDoc,
    Category,
    DocType,
    from_mongo_dict,
    to_mongo_dict,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _spot_leg(instrument_id: str, weight: float = 0.5) -> dict:
    return {
        "instrument": {
            "type": "spot",
            "collection": "ETF",
            "instrument_id": instrument_id,
        },
        "weight": weight,
    }


def _continuous_leg(
    collection: str,
    weight: float = 0.5,
    *,
    adjustment: str = "ratio",
    cycle: str | None = "HMUZ",
) -> dict:
    return {
        "instrument": {
            "type": "continuous",
            "collection": collection,
            "adjustment": adjustment,
            "cycle": cycle,
            "rollOffset": 0,
            "strategy": "front_month",
        },
        "weight": weight,
    }


def _make_basket(**kwargs) -> BasketDoc:
    defaults = dict(
        id="b1",
        type=DocType.BASKET.value,
        name="Test Basket",
        category=Category.RESEARCH,
        asset_class="equity",
        created_at=NOW,
        updated_at=NOW,
        legs=(
            _spot_leg("SPY", weight=0.6),
            _spot_leg("QQQ", weight=0.4),
        ),
    )
    defaults.update(kwargs)
    return BasketDoc(**defaults)


def test_basket_doctype_value() -> None:
    assert DocType.BASKET == "basket"
    assert DocType.BASKET.value == "basket"


def test_basket_doc_polymorphic_spot_legs_round_trip() -> None:
    doc = _make_basket()
    d = to_mongo_dict(doc)
    assert d["_id"] == "b1"
    assert d["type"] == "basket"
    assert d["asset_class"] == "equity"
    assert isinstance(d["legs"], list)
    # Polymorphic shape: ``instrument`` sub-dict + flat ``weight``.
    assert d["legs"][0]["instrument"]["type"] == "spot"
    assert d["legs"][0]["instrument"]["instrument_id"] == "SPY"
    assert d["legs"][0]["weight"] == 0.6
    reconstructed = from_mongo_dict(d)
    assert reconstructed == doc


def test_basket_doc_continuous_legs_round_trip() -> None:
    doc = _make_basket(
        asset_class="future",
        legs=(
            _continuous_leg("FUT_VIX", weight=0.5, adjustment="ratio"),
            _continuous_leg("FUT_ES", weight=0.5, adjustment="none"),
        ),
    )
    d = to_mongo_dict(doc)
    reconstructed = from_mongo_dict(d)
    assert reconstructed == doc


def test_basket_doc_empty_legs_round_trip() -> None:
    doc = _make_basket(legs=())
    d = to_mongo_dict(doc)
    assert d["legs"] == []
    reconstructed = from_mongo_dict(d)
    assert isinstance(reconstructed, BasketDoc)
    assert reconstructed.legs == ()


def test_basket_doc_category_serialised_as_string() -> None:
    doc = _make_basket(category=Category.PROD)
    d = to_mongo_dict(doc)
    assert d["category"] == "PROD"
    assert isinstance(d["category"], str)


def test_basket_doc_asset_class_present_on_mongo_dict() -> None:
    doc = _make_basket(asset_class="future")
    d = to_mongo_dict(doc)
    assert d["asset_class"] == "future"


def test_basket_doc_asset_class_defaults_when_absent_in_stored_doc() -> None:
    """Forward-compat: a doc missing ``asset_class`` (predates iter 3)
    reconstructs with the default ``"equity"`` rather than raising."""
    d = {
        "_id": "legacy",
        "type": "basket",
        "name": "Legacy",
        "category": "RESEARCH",
        "created_at": NOW,
        "updated_at": NOW,
        "legs": [],
    }
    reconstructed = from_mongo_dict(d)
    assert isinstance(reconstructed, BasketDoc)
    assert reconstructed.asset_class == "equity"


def test_from_mongo_dict_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown or missing"):
        from_mongo_dict({"type": "unknown_type", "_id": "x"})


def test_from_mongo_dict_missing_id_raises() -> None:
    with pytest.raises(ValueError, match="missing '_id'"):
        from_mongo_dict(
            {
                "type": "basket",
                "name": "X",
                "category": "RESEARCH",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )


def test_basket_doc_negative_weight_round_trip() -> None:
    doc = _make_basket(
        legs=(
            _spot_leg("SPY", weight=1.0),
            _spot_leg("QQQ", weight=-0.5),
        )
    )
    d = to_mongo_dict(doc)
    reconstructed = from_mongo_dict(d)
    assert reconstructed == doc
