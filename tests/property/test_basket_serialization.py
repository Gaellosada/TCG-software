"""Hypothesis property tests: ``BasketDoc`` Mongo serde round-trips."""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.types.persistence import (
    BasketDoc,
    Category,
    DocType,
    from_mongo_dict,
    to_mongo_dict,
)


_CATEGORIES = list(Category)

# Safe identifiers — letters / digits / underscore / dash.
_SAFE_STR = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"), whitelist_characters="_-"
    ),
    min_size=1,
    max_size=64,
)

_LEG = st.fixed_dictionaries(
    {
        "instrument_id": _SAFE_STR,
        "collection": st.sampled_from(["ETF", "FUT_VIX", "INDEX", "FUND"]),
        # Avoid weight=0.0 to mirror the wire-level validator.
        "weight": st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    }
)


@given(
    doc_id=_SAFE_STR,
    name=st.text(min_size=1, max_size=128),
    category=st.sampled_from(_CATEGORIES),
    legs=st.lists(_LEG, max_size=20),
)
@settings(max_examples=200, deadline=None)
def test_basket_doc_mongo_round_trip(doc_id, name, category, legs) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    doc = BasketDoc(
        id=doc_id,
        type=DocType.BASKET.value,
        name=name,
        category=category,
        created_at=now,
        updated_at=now,
        legs=tuple(legs),
    )
    d = to_mongo_dict(doc)
    reconstructed = from_mongo_dict(d)
    assert reconstructed == doc


@given(
    legs=st.lists(
        st.fixed_dictionaries(
            {
                "instrument_id": _SAFE_STR,
                "collection": st.sampled_from(["ETF", "FUT_VIX"]),
                "weight": st.floats(
                    min_value=-1.0, max_value=1.0, allow_nan=False
                ).filter(lambda w: abs(w) > 1e-9),
            }
        ),
        max_size=10,
    )
)
@settings(max_examples=100, deadline=None)
def test_basket_doc_signed_weights_round_trip(legs) -> None:
    """Negative weights (short legs) survive serde unchanged."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    doc = BasketDoc(
        id="b-prop",
        type=DocType.BASKET.value,
        name="prop",
        category=Category.RESEARCH,
        created_at=now,
        updated_at=now,
        legs=tuple(legs),
    )
    d = to_mongo_dict(doc)
    reconstructed = from_mongo_dict(d)
    assert reconstructed == doc
