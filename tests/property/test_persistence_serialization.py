"""Hypothesis property tests: dataclass ↔ mongo-dict round-trip.

For arbitrary indicator / signal / portfolio docs we want:

    from_mongo_dict(to_mongo_dict(doc)) == doc

This catches any subtle drift in the serializer (e.g. forgetting to
rename id↔_id, mishandling Category, dropping a field).
"""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, strategies as st

from tcg.types.persistence import (
    Category,
    IndicatorDoc,
    PortfolioDoc,
    SignalDoc,
    from_mongo_dict,
    to_mongo_dict,
)


# ---------------------------------------------------------------------------
# Inner-payload strategies
# ---------------------------------------------------------------------------


def _jsonable_scalar() -> st.SearchStrategy:
    """Mongo-storable scalar values: int / float / bool / str / None.

    We deliberately keep the strategy simple — the persistence module
    treats payloads as opaque dicts, so we only need to verify that
    dict/list nesting and primitive values survive a round-trip.
    """
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**31), max_value=2**31 - 1),
        st.floats(allow_nan=False, allow_infinity=False, width=32),
        st.text(max_size=20),
    )


def _payload_dict() -> st.SearchStrategy:
    """A small opaque dict — keys are short ASCII strings, values are
    scalars or nested dicts/lists of scalars.
    """
    return st.dictionaries(
        keys=st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                min_codepoint=0x20,
                max_codepoint=0x7E,
            ),
            min_size=1,
            max_size=12,
        ),
        values=st.one_of(
            _jsonable_scalar(),
            st.lists(_jsonable_scalar(), max_size=4),
            st.dictionaries(
                keys=st.text(min_size=1, max_size=8),
                values=_jsonable_scalar(),
                max_size=4,
            ),
        ),
        max_size=4,
    )


def _payload_list() -> st.SearchStrategy:
    """A small list of opaque payload dicts."""
    return st.lists(_payload_dict(), max_size=4)


# ``datetime`` with explicit UTC tzinfo — matches the repository's
# server-stamped values. Mongo strips sub-millisecond precision in
# practice (BSON datetime is millisecond-resolution), but here we go
# through plain Python dicts, so microsecond precision survives.
def _utc_datetimes() -> st.SearchStrategy:
    return st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 12, 31, 23, 59, 59),
        timezones=st.just(timezone.utc),
    )


# Short identifier strategy — non-empty ASCII, no whitespace tricks.
_ID_STRATEGY = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=32,
)
_NAME_STRATEGY = st.text(min_size=1, max_size=40)


# ---------------------------------------------------------------------------
# Dataclass strategies
# ---------------------------------------------------------------------------


@st.composite
def _indicator_docs(draw) -> IndicatorDoc:
    return IndicatorDoc(
        id=draw(_ID_STRATEGY),
        type="indicator",
        name=draw(_NAME_STRATEGY),
        definition=draw(_payload_dict()),
        created_at=draw(_utc_datetimes()),
        updated_at=draw(_utc_datetimes()),
        deleted=draw(st.booleans()),
    )


@st.composite
def _signal_docs(draw) -> SignalDoc:
    return SignalDoc(
        id=draw(_ID_STRATEGY),
        type="signal",
        name=draw(_NAME_STRATEGY),
        blocks=draw(_payload_list()),
        category=draw(st.sampled_from(list(Category))),
        created_at=draw(_utc_datetimes()),
        updated_at=draw(_utc_datetimes()),
    )


@st.composite
def _portfolio_docs(draw) -> PortfolioDoc:
    return PortfolioDoc(
        id=draw(_ID_STRATEGY),
        type="portfolio",
        name=draw(_NAME_STRATEGY),
        instruments=draw(_payload_list()),
        rebalance=draw(_payload_dict()),
        category=draw(st.sampled_from(list(Category))),
        created_at=draw(_utc_datetimes()),
        updated_at=draw(_utc_datetimes()),
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(doc=_indicator_docs())
def test_indicator_roundtrip(doc: IndicatorDoc) -> None:
    assert from_mongo_dict(to_mongo_dict(doc)) == doc


@given(doc=_signal_docs())
def test_signal_roundtrip(doc: SignalDoc) -> None:
    assert from_mongo_dict(to_mongo_dict(doc)) == doc


@given(doc=_portfolio_docs())
def test_portfolio_roundtrip(doc: PortfolioDoc) -> None:
    assert from_mongo_dict(to_mongo_dict(doc)) == doc


def test_to_mongo_dict_emits_underscore_id_key() -> None:
    """Sanity assertion — the serializer must rename id → _id; no
    Mongo doc should contain a literal ``id`` field."""
    doc = IndicatorDoc(
        id="foo",
        type="indicator",
        name="bar",
        definition={},
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    out = to_mongo_dict(doc)
    assert "_id" in out
    assert "id" not in out
    assert out["_id"] == "foo"


def test_from_mongo_dict_rejects_unknown_type() -> None:
    """The discriminator gate must reject foreign documents loudly."""
    import pytest

    with pytest.raises(ValueError, match="unknown or missing 'type'"):
        from_mongo_dict({"_id": "x", "type": "unicorn"})


def test_from_mongo_dict_rejects_missing_id() -> None:
    import pytest

    with pytest.raises(ValueError, match="missing '_id'"):
        from_mongo_dict(
            {
                "type": "indicator",
                "name": "x",
                "definition": {},
                "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
                "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            }
        )
