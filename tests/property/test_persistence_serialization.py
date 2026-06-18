"""Hypothesis property tests: dataclass ↔ mongo-dict round-trip.

For arbitrary indicator / signal / portfolio docs we want:

    from_json_doc(to_json_doc(doc)) == doc

This catches any subtle drift in the serializer (e.g. forgetting to
rename id↔_id, mishandling Category, dropping a field).

Strategy width (M3)
-------------------
The strategies cover broader inputs than the original ASCII-only,
max-depth-2, 4-element-list version:

* keys/values include the full BMP Unicode range,
* dicts can be empty (``min_size=0``) and contain up to 10 entries,
* nesting reaches up to 5 levels deep,
* explicit ``@example`` cases pin edge inputs (empty string, ``None``
  values, full-width Unicode, deeply nested dicts).

These are tighter than malicious input — the goal is to exercise the
serializer over realistic and adversarial shapes without exploding
the test runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import example, given, settings, strategies as st

from tcg.types.persistence import (
    Category,
    IndicatorDoc,
    PortfolioDoc,
    SignalDoc,
    from_json_doc,
    to_json_doc,
)


# ---------------------------------------------------------------------------
# Inner-payload strategies
# ---------------------------------------------------------------------------


def _jsonable_scalar() -> st.SearchStrategy:
    """Mongo-storable scalar values: int / float / bool / str / None.

    Wider than the previous ASCII-only strategy: text spans the full
    Basic Multilingual Plane so emoji-free Unicode (CJK, accents,
    full-width forms) is exercised. Floats are full-width (no
    ``width=32`` clipping).
    """
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53 - 1),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=100, alphabet=st.characters(max_codepoint=0xFFFF)),
    )


def _key_strategy() -> st.SearchStrategy:
    """Keys: non-empty BMP strings, excluding control + surrogate.

    Mongo rejects dollar-prefixed keys and keys containing ``.``; this
    strategy intentionally does NOT carve those out because the
    persistence layer treats payloads as opaque (the surface is the
    Pydantic wire models, which are tested separately). Keeping them
    here just verifies the serializer doesn't choke on them.
    """
    return st.text(
        alphabet=st.characters(max_codepoint=0xFFFF, blacklist_categories=("Cc", "Cs")),
        min_size=1,
        max_size=16,
    )


def _payload_value(max_leaves: int = 20) -> st.SearchStrategy:
    """A nested value (dict / list / scalar) up to ``max_leaves`` deep.

    Used as the values inside ``_payload_dict``. Built from
    ``st.recursive`` so a single strategy covers everything from a
    plain scalar to a 5-level-deep nested dict.
    """
    return st.recursive(
        _jsonable_scalar(),
        lambda children: st.one_of(
            st.lists(children, max_size=10),
            st.dictionaries(keys=_key_strategy(), values=children, max_size=10),
        ),
        max_leaves=max_leaves,
    )


def _payload_dict(max_leaves: int = 20, max_depth: int = 5) -> st.SearchStrategy:
    """An opaque dict whose values can nest several levels deep.

    Keys span the BMP, values use :func:`_payload_value`. ``max_depth``
    is documentation only — depth is bounded by ``max_leaves`` via
    ``st.recursive``'s budget. We expose the param for caller
    legibility.
    """
    del max_depth  # implicit via max_leaves
    return st.dictionaries(
        keys=_key_strategy(),
        values=_payload_value(max_leaves=max_leaves),
        max_size=10,
    )


def _payload_list() -> st.SearchStrategy:
    """A list of opaque payload dicts. Wider than the original (max 4)."""
    return st.lists(_payload_dict(max_leaves=10, max_depth=4), max_size=10)


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


# Identifier strategy — wider than before but still excluding control
# chars / surrogates so the serializer's round-trip isn't muddied by
# encoding quirks that are out of scope for this test.
_ID_STRATEGY = st.text(
    alphabet=st.characters(
        max_codepoint=0xFFFF, blacklist_categories=("Cc", "Cs", "Zl", "Zp")
    ),
    min_size=1,
    max_size=64,
)
# Names can be any non-empty BMP string up to 100 chars.
_NAME_STRATEGY = st.text(
    alphabet=st.characters(max_codepoint=0xFFFF, blacklist_categories=("Cc", "Cs")),
    min_size=0,
    max_size=100,
)
# Descriptions can be empty.
_DESCRIPTION_STRATEGY = st.text(
    alphabet=st.characters(max_codepoint=0xFFFF, blacklist_categories=("Cc", "Cs")),
    min_size=0,
    max_size=200,
)


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
    # ``inputs`` is tuple-typed on the dataclass — Hypothesis still
    # produces lists; coerce so we exercise the canonical in-memory
    # shape (mismatched container types would surface as round-trip
    # failures).
    return SignalDoc(
        id=draw(_ID_STRATEGY),
        type="signal",
        name=draw(_NAME_STRATEGY),
        category=draw(st.sampled_from(list(Category))),
        created_at=draw(_utc_datetimes()),
        updated_at=draw(_utc_datetimes()),
        inputs=tuple(draw(_payload_list())),
        rules=draw(_payload_dict()),
        settings=draw(_payload_dict()),
        description=draw(_DESCRIPTION_STRATEGY),
    )


@st.composite
def _portfolio_docs(draw) -> PortfolioDoc:
    return PortfolioDoc(
        id=draw(_ID_STRATEGY),
        type="portfolio",
        name=draw(_NAME_STRATEGY),
        category=draw(st.sampled_from(list(Category))),
        created_at=draw(_utc_datetimes()),
        updated_at=draw(_utc_datetimes()),
        legs=tuple(draw(_payload_list())),
        rebalance=draw(
            st.sampled_from(
                ["none", "daily", "weekly", "monthly", "quarterly", "annually"]
            )
        ),
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


# Edge-case datetime used by ``@example`` cases — deterministic so the
# round-trip is reproducible regardless of Hypothesis seed.
_EDGE_DT = datetime(2025, 6, 15, 12, 30, 45, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Round-trip properties
# ---------------------------------------------------------------------------


@given(doc=_indicator_docs())
@example(
    # Empty definition — minimal shape.
    doc=IndicatorDoc(
        id="x",
        type="indicator",
        name="",
        definition={},
        created_at=_EDGE_DT,
        updated_at=_EDGE_DT,
    )
)
@example(
    # Deeply nested definition (5 levels) with mixed Unicode and None.
    doc=IndicatorDoc(
        id="深い",
        type="indicator",
        name=" ",  # single space
        definition={
            "lvl1": {
                "lvl2": {"lvl3": {"lvl4": {"lvl5": None, "u": "ｆｕｌｌ-ｗｉｄｔｈ"}}}
            }
        },
        created_at=_EDGE_DT,
        updated_at=_EDGE_DT,
    )
)
@settings(max_examples=50, deadline=None)
def test_indicator_roundtrip(doc: IndicatorDoc) -> None:
    assert from_json_doc(to_json_doc(doc)) == doc


@given(doc=_signal_docs())
@example(
    # All-empty editable content + empty description.
    doc=SignalDoc(
        id="s",
        type="signal",
        name="n",
        category=Category.DEV,
        created_at=_EDGE_DT,
        updated_at=_EDGE_DT,
        inputs=(),
        rules={},
        settings={},
        description="",
    )
)
@example(
    # Unicode-heavy description + nested rules.
    doc=SignalDoc(
        id="s",
        type="signal",
        name="测试-signal",
        category=Category.PROD,
        created_at=_EDGE_DT,
        updated_at=_EDGE_DT,
        inputs=({"k": "Ω-α-β"},),
        rules={"entries": [{"cond": {"op": ">", "v": None}}]},
        settings={"dont_repeat": True, "deep": {"a": {"b": {"c": "x"}}}},
        description="日本語 + Ωmega",
    )
)
@settings(max_examples=50, deadline=None)
def test_signal_roundtrip(doc: SignalDoc) -> None:
    assert from_json_doc(to_json_doc(doc)) == doc


@given(doc=_portfolio_docs())
@example(
    # Empty legs + rebalance=none.
    doc=PortfolioDoc(
        id="p",
        type="portfolio",
        name="empty",
        category=Category.RESEARCH,
        created_at=_EDGE_DT,
        updated_at=_EDGE_DT,
        legs=(),
        rebalance="none",
    )
)
@example(
    # Multiple legs with Unicode labels.
    doc=PortfolioDoc(
        id="p",
        type="portfolio",
        name="emoji-free unicode",
        category=Category.PROD,
        created_at=_EDGE_DT,
        updated_at=_EDGE_DT,
        legs=(
            {"label": "ＳＰＹ", "weight": 60, "type": "instrument"},
            {"label": "АГГ", "weight": 40, "type": "instrument"},  # Cyrillic
        ),
        rebalance="quarterly",
    )
)
@settings(max_examples=50, deadline=None)
def test_portfolio_roundtrip(doc: PortfolioDoc) -> None:
    assert from_json_doc(to_json_doc(doc)) == doc


def test_to_json_doc_emits_plain_id_key() -> None:
    """Sanity assertion — the JSONB payload keeps the ``id`` field (the
    PostgreSQL primary-key column is ``id``); no legacy ``_id`` key."""
    doc = IndicatorDoc(
        id="foo",
        type="indicator",
        name="bar",
        definition={},
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    out = to_json_doc(doc)
    assert "id" in out
    assert "_id" not in out
    assert out["id"] == "foo"


def test_from_json_doc_rejects_unknown_type() -> None:
    """The discriminator gate must reject foreign documents loudly."""
    import pytest

    with pytest.raises(ValueError, match="unknown or missing 'type'"):
        from_json_doc({"_id": "x", "type": "unicorn"})


def test_from_json_doc_rejects_missing_id() -> None:
    import pytest

    with pytest.raises(ValueError, match="missing 'id'"):
        from_json_doc(
            {
                "type": "indicator",
                "name": "x",
                "definition": {},
                "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
                "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            }
        )
