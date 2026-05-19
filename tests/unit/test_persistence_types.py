"""Unit tests for ``BasketDoc`` round-trip through Mongo serde."""

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


def _make_basket(**kwargs) -> BasketDoc:
    defaults = dict(
        id="b1",
        type=DocType.BASKET.value,
        name="Test Basket",
        category=Category.RESEARCH,
        created_at=NOW,
        updated_at=NOW,
        legs=(
            {"instrument_id": "SPY", "collection": "ETF", "weight": 0.6},
            {"instrument_id": "QQQ", "collection": "ETF", "weight": 0.4},
        ),
    )
    defaults.update(kwargs)
    return BasketDoc(**defaults)


def test_basket_doctype_value() -> None:
    assert DocType.BASKET == "basket"
    assert DocType.BASKET.value == "basket"


def test_basket_doc_round_trip_via_mongo_dict() -> None:
    doc = _make_basket()
    d = to_mongo_dict(doc)
    assert d["_id"] == "b1"
    assert d["type"] == "basket"
    assert d["category"] == "RESEARCH"
    assert isinstance(d["legs"], list)
    assert d["legs"][0] == {
        "instrument_id": "SPY",
        "collection": "ETF",
        "weight": 0.6,
    }
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
    """Short legs (negative weight) survive Mongo round-trip."""
    doc = _make_basket(
        legs=(
            {"instrument_id": "SPY", "collection": "ETF", "weight": 1.0},
            {"instrument_id": "QQQ", "collection": "ETF", "weight": -0.5},
        )
    )
    d = to_mongo_dict(doc)
    reconstructed = from_mongo_dict(d)
    assert reconstructed == doc
