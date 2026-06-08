"""API-surface reflection tests for ``WriteRepository``.

These tests are the safety net for the "no escape hatch" guarantee:
they fail loudly if anyone adds a public method that takes a
collection name, or exposes a public attribute that looks like a
collection / database handle, or widens ``__init__``. They run in
pure-Python (no Mongo, no event loop), so they catch regressions on
every test run.
"""

from __future__ import annotations

import inspect

from tcg.persistence import WriteRepository


_EXPECTED_PUBLIC_METHODS = frozenset(
    {
        "create",
        "get_by_id",
        "list_by_type",
        "list_by_type_and_category",
        "update",
        "archive",
    }
)

# Parameter names that would smuggle a collection / database handle
# into a public method. The presence of any of these is a regression.
_FORBIDDEN_PARAM_NAMES = frozenset(
    {"collection", "coll", "coll_name", "db", "database", "namespace"}
)

# Attribute names that would expose a collection handle on the public
# surface. We allow only the documented ``_coll`` private attribute.
_FORBIDDEN_PUBLIC_ATTR_HINTS = (
    "coll",
    "collection",
    "db",
    "database",
    "namespace",
    "client",
)


def _public_methods() -> dict[str, object]:
    """Return ``{name: member}`` for every public method on the class."""
    out: dict[str, object] = {}
    for name, member in inspect.getmembers(WriteRepository):
        if name.startswith("_"):
            continue
        # Methods on a class show up as functions (PEP 3155) — we don't
        # want to also gate static helper *attributes* (none today, but
        # be explicit).
        if inspect.isfunction(member) or inspect.ismethod(member):
            out[name] = member
    return out


def test_public_method_set_is_exactly_the_documented_surface() -> None:
    publics = set(_public_methods().keys())
    assert publics == _EXPECTED_PUBLIC_METHODS, (
        f"WriteRepository public method set drifted: "
        f"unexpected={publics - _EXPECTED_PUBLIC_METHODS}, "
        f"missing={_EXPECTED_PUBLIC_METHODS - publics}"
    )


def test_no_public_method_accepts_a_collection_name() -> None:
    for name, method in _public_methods().items():
        sig = inspect.signature(method)
        for param in sig.parameters.values():
            assert param.name not in _FORBIDDEN_PARAM_NAMES, (
                f"WriteRepository.{name} accepts a forbidden parameter "
                f"{param.name!r} — collection/db names must NOT be "
                f"reachable through the public API."
            )


def test_init_signature_is_locked_to_client_db_collection() -> None:
    """``__init__`` must accept exactly ``(self, client, *, db_name, collection_name)``.

    ``db_name`` and ``collection_name`` are keyword-only — they configure
    the collection binding at construction time and are not reachable
    through the public API surface (``__init__`` is exempt from the
    "no collection name in public methods" rule; the constraint applies
    to ``create / get_by_id / list_* / update / archive``).
    """
    sig = inspect.signature(WriteRepository.__init__)
    params = sig.parameters
    param_names = list(params.keys())
    assert param_names == ["self", "client", "db_name", "collection_name"], (
        f"WriteRepository.__init__ signature drifted: "
        f"expected ['self', 'client', 'db_name', 'collection_name'], got {param_names}"
    )
    # db_name and collection_name must be keyword-only
    import inspect as _inspect

    for kw in ("db_name", "collection_name"):
        assert params[kw].kind == _inspect.Parameter.KEYWORD_ONLY, (
            f"WriteRepository.__init__ parameter {kw!r} must be keyword-only"
        )


def test_no_public_attribute_exposes_a_collection_handle() -> None:
    """Class-level attributes whose name looks like a handle would
    re-introduce the escape hatch. The only allowed handle attribute
    is the private ``_coll``."""
    for name in dir(WriteRepository):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        for hint in _FORBIDDEN_PUBLIC_ATTR_HINTS:
            assert hint not in lowered, (
                f"WriteRepository exposes public attribute {name!r} "
                f"matching forbidden hint {hint!r}. Make it private "
                f"(prefix with _) or rename."
            )


def test_class_does_not_define_dunder_getattr() -> None:
    """A custom ``__getattr__`` could forward arbitrary attribute
    lookups to the underlying client, defeating the whole point of
    binding ``_coll`` once. Forbid it explicitly."""
    own = vars(WriteRepository)
    assert "__getattr__" not in own, (
        "WriteRepository defines __getattr__ — this would be an escape "
        "hatch back to the underlying Motor client. Remove it."
    )


def test_coll_attribute_is_immutable_after_construction() -> None:
    """``_coll`` must NOT be rebindable post-construction.

    Regression for M5: a previous version allowed
    ``repo._coll = attacker_handle`` which would route subsequent
    writes to an attacker-controlled namespace. The class now uses
    ``__slots__`` and a ``__setattr__`` guard to make any attempt
    raise ``AttributeError``.
    """
    import pytest

    class _FakeColl:
        pass

    class _FakeDB:
        def __getitem__(self, name: str) -> object:
            return _FakeColl()

    class _FakeClient:
        def __getitem__(self, name: str) -> object:
            return _FakeDB()

    repo = WriteRepository(
        _FakeClient(),  # type: ignore[arg-type]
        db_name="any",
        collection_name="any",
    )

    # Existing _coll attribute cannot be rebound.
    with pytest.raises(AttributeError):
        repo._coll = _FakeColl()  # type: ignore[misc]

    # Adding a fresh attribute is also rejected (slots + setattr guard).
    with pytest.raises(AttributeError):
        repo.alias = "x"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# CRIT#1 — per-document deserialization in the repository list methods must
# NOT let ONE malformed stored doc 500 the entire list. The repo must skip +
# log the bad doc and return the good ones.
#
# These tests drive the real ``WriteRepository.list_by_type`` /
# ``list_by_type_and_category`` against a fake Motor collection whose
# ``find(...).to_list(...)`` yields a mix of valid and malformed RAW mongo
# dicts. They run pure-Python (no Mongo, no real event loop beyond asyncio.run).
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Stand-in for the Motor cursor returned by ``collection.find(...)``."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def to_list(self, length=None) -> list[dict]:  # noqa: ANN001
        return list(self._rows)


class _FakeFindColl:
    """Fake collection exposing only the ``find`` used by the list methods."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.last_filter: dict | None = None

    def find(self, filter_: dict) -> _FakeCursor:
        self.last_filter = filter_
        return _FakeCursor(self._rows)


def _repo_with_rows(rows: list[dict]) -> WriteRepository:
    """Build a real ``WriteRepository`` whose ``_coll`` is a fake collection.

    ``_coll`` is slot-locked, so we bind it through ``object.__setattr__``
    exactly the way ``__init__`` does — the public surface is untouched.
    """
    repo = WriteRepository.__new__(WriteRepository)
    object.__setattr__(repo, "_coll", _FakeFindColl(rows))
    return repo


def test_list_by_type_and_category_skips_malformed_doc() -> None:
    """One stored signal doc missing ``category`` must NOT crash the list —
    the valid doc is returned, the malformed one skipped (regression for the
    CRITICAL: ``from_mongo_dict`` in the repo list-comp had no try/except)."""
    import asyncio

    from datetime import datetime, timezone

    from tcg.types.persistence import Category, SignalDoc

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good_raw = {
        "_id": "good-sig",
        "type": "signal",
        "name": "Good",
        "category": "DEV",
        "created_at": now,
        "updated_at": now,
        "inputs": [],
        "rules": {},
        "settings": {},
        "description": "",
    }
    # Malformed: missing ``category`` → from_mongo_dict raises KeyError.
    bad_raw = {
        "_id": "bad-sig",
        "type": "signal",
        "name": "Missing Category",
        "created_at": now,
        "updated_at": now,
    }
    repo = _repo_with_rows([good_raw, bad_raw])

    result = asyncio.run(repo.list_by_type_and_category("signal", Category.DEV))

    ids = [d.id for d in result]
    assert ids == ["good-sig"], f"expected only the valid doc, got {ids}"
    assert isinstance(result[0], SignalDoc)


def test_list_by_type_and_category_skips_unknown_type_and_bad_category() -> None:
    """Two distinct malformed shapes (unknown ``type`` → ValueError, bad
    ``category`` value → ValueError) are both skipped while the good doc
    survives."""
    import asyncio

    from datetime import datetime, timezone

    from tcg.types.persistence import Category

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good_raw = {
        "_id": "good-ptf",
        "type": "portfolio",
        "name": "Good",
        "category": "RESEARCH",
        "created_at": now,
        "updated_at": now,
        "legs": [],
        "rebalance": "none",
    }
    bad_type = {
        "_id": "bad-type",
        "type": "not-a-real-type",
        "name": "Bad Type",
        "category": "RESEARCH",
        "created_at": now,
        "updated_at": now,
    }
    bad_category = {
        "_id": "bad-cat",
        "type": "portfolio",
        "name": "Bad Category",
        "category": "BOGUS",
        "created_at": now,
        "updated_at": now,
    }
    repo = _repo_with_rows([good_raw, bad_type, bad_category])

    result = asyncio.run(repo.list_by_type_and_category("portfolio", Category.RESEARCH))

    assert [d.id for d in result] == ["good-ptf"]


def test_list_by_type_skips_malformed_indicator_doc() -> None:
    """The indicator list path (``list_by_type``) has the same per-doc
    guard: a malformed indicator (missing required ``definition``) is
    skipped, valid ones returned."""
    import asyncio

    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good_raw = {
        "_id": "good-ind",
        "type": "indicator",
        "name": "Good",
        "definition": {"period": 14},
        "created_at": now,
        "updated_at": now,
        "deleted": False,
    }
    # Missing required ``definition`` (no default) → ValueError in from_mongo_dict.
    bad_raw = {
        "_id": "bad-ind",
        "type": "indicator",
        "name": "Missing Definition",
        "created_at": now,
        "updated_at": now,
        "deleted": False,
    }
    repo = _repo_with_rows([good_raw, bad_raw])

    result = asyncio.run(repo.list_by_type("indicator"))

    assert [d.id for d in result] == ["good-ind"]


def test_doctype_enum_values_match_discriminators() -> None:
    """``DocType`` is the single source of truth for the type
    discriminator strings. Regression for M6 — ensures any future
    rename ripples through ``DocType`` rather than scattered string
    literals."""
    from tcg.types.persistence import DocType, SignalDoc

    assert DocType.INDICATOR.value == "indicator"
    assert DocType.SIGNAL.value == "signal"
    assert DocType.PORTFOLIO.value == "portfolio"

    # A constructed SignalDoc's type field compares equal to the
    # corresponding enum member (StrEnum semantics).
    from datetime import datetime, timezone

    from tcg.types.persistence import Category

    sig = SignalDoc(
        id="x",
        type="signal",
        name="x",
        category=Category.DEV,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert sig.type == DocType.SIGNAL
    assert sig.type == DocType.SIGNAL.value
