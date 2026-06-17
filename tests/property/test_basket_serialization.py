"""Hypothesis property tests: ``BasketDoc`` polymorphic-leg Mongo serde."""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.types.persistence import (
    BasketDoc,
    Category,
    DocType,
    from_json_doc,
    to_json_doc,
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


_SPOT_LEG = st.fixed_dictionaries(
    {
        "instrument": st.fixed_dictionaries(
            {
                "type": st.just("spot"),
                "collection": st.sampled_from(["ETF", "FUND", "FOREX", "INDEX"]),
                "instrument_id": _SAFE_STR,
            }
        ),
        "weight": st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    }
)


_CONTINUOUS_LEG = st.fixed_dictionaries(
    {
        "instrument": st.fixed_dictionaries(
            {
                "type": st.just("continuous"),
                "collection": st.sampled_from(["FUT_VIX", "FUT_ES", "FUT_CL"]),
                "adjustment": st.sampled_from(["none", "ratio", "difference"]),
                "cycle": st.one_of(st.none(), st.sampled_from(["HMUZ", "M"])),
                "rollOffset": st.integers(min_value=-5, max_value=5),
                "strategy": st.just("front_month"),
            }
        ),
        "weight": st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    }
)


@given(
    doc_id=_SAFE_STR,
    name=st.text(min_size=1, max_size=128),
    category=st.sampled_from(_CATEGORIES),
    legs=st.lists(_SPOT_LEG, max_size=20),
)
@settings(max_examples=150, deadline=None)
def test_basket_doc_spot_legs_round_trip(doc_id, name, category, legs) -> None:
    """Spot-leg baskets (equity / index) serde."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    doc = BasketDoc(
        id=doc_id,
        type=DocType.BASKET.value,
        name=name,
        category=category,
        asset_class="equity",
        created_at=now,
        updated_at=now,
        legs=tuple(legs),
    )
    d = to_json_doc(doc)
    reconstructed = from_json_doc(d)
    assert reconstructed == doc


@given(
    doc_id=_SAFE_STR,
    legs=st.lists(_CONTINUOUS_LEG, max_size=10),
)
@settings(max_examples=100, deadline=None)
def test_basket_doc_continuous_legs_round_trip(doc_id, legs) -> None:
    """Continuous (rolled-future) legs serde — adjustment + cycle +
    rollOffset all preserved."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    doc = BasketDoc(
        id=doc_id,
        type=DocType.BASKET.value,
        name="prop-cont",
        category=Category.RESEARCH,
        asset_class="future",
        created_at=now,
        updated_at=now,
        legs=tuple(legs),
    )
    d = to_json_doc(doc)
    reconstructed = from_json_doc(d)
    assert reconstructed == doc


@given(
    legs=st.lists(
        st.fixed_dictionaries(
            {
                "instrument": st.fixed_dictionaries(
                    {
                        "type": st.just("spot"),
                        "collection": st.sampled_from(["ETF", "FUT_VIX"]),
                        "instrument_id": _SAFE_STR,
                    }
                ),
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
        asset_class="equity",
        created_at=now,
        updated_at=now,
        legs=tuple(legs),
    )
    d = to_json_doc(doc)
    reconstructed = from_json_doc(d)
    assert reconstructed == doc
