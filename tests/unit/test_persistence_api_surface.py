"""API-surface reflection tests for ``WriteRepository``.

These tests are the safety net for the "no escape hatch" guarantee:
they fail loudly if anyone adds a public method that takes a
collection name, or exposes a public attribute that looks like a
collection / database handle, or widens ``__init__``. They run in
pure-Python (no database, no event loop), so they catch regressions on
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
        # ``set_locked`` flips the per-doc write-lock flag and is the
        # ONLY mutation that bypasses the lock guard (so a locked doc can
        # be unlocked). It takes ``(doc_type, doc_id, locked)`` — no
        # collection/db handle — so the escape-hatch guarantees below
        # still hold. Registered here as an intentional surface addition.
        "set_locked",
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


def test_init_signature_is_locked_to_pool() -> None:
    """``__init__`` must accept exactly ``(self, pool)``.

    The repository is constructed from a single read-WRITE
    :class:`AppDbConnectionPool`; no table / schema / database name is
    reachable through the constructor (``__init__`` is exempt from the
    "no collection name in public methods" rule; that constraint applies
    to ``create / get_by_id / list_* / update / archive / set_locked``).
    The pool handle is bound to the private ``_pool`` slot.
    """
    sig = inspect.signature(WriteRepository.__init__)
    param_names = list(sig.parameters.keys())
    assert param_names == ["self", "pool"], (
        f"WriteRepository.__init__ signature drifted: "
        f"expected ['self', 'pool'], got {param_names}"
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
    lookups to the underlying pool, defeating the whole point of binding
    ``_pool`` once. Forbid it explicitly."""
    own = vars(WriteRepository)
    assert "__getattr__" not in own, (
        "WriteRepository defines __getattr__ — this would be an escape "
        "hatch back to the underlying connection pool. Remove it."
    )


def test_pool_attribute_is_immutable_after_construction() -> None:
    """``_pool`` must NOT be rebindable post-construction.

    Regression for M5: a previous version allowed
    ``repo._pool = attacker_handle`` which would route subsequent writes
    to an attacker-controlled store. The class uses ``__slots__`` + a
    ``__setattr__`` guard so any attempt raises ``AttributeError``.
    """
    import pytest

    class _FakePool:
        schema = "tcg_app_data"

    repo = WriteRepository(_FakePool())  # type: ignore[arg-type]

    # Existing _pool attribute cannot be rebound.
    with pytest.raises(AttributeError):
        repo._pool = _FakePool()  # type: ignore[misc]

    # Adding a fresh attribute is also rejected (slots + setattr guard).
    with pytest.raises(AttributeError):
        repo.alias = "x"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# CRIT#1 — per-document deserialization in the repository list methods must
# NOT let ONE malformed stored doc 500 the entire list. The repo must skip +
# log the bad doc and return the good ones.
#
# These tests drive the real ``WriteRepository.list_by_type`` /
# ``list_by_type_and_category`` against a fake PG pool whose cursor
# ``fetchall()`` yields a mix of valid and malformed PG rows (each row is
# ``{id, type, category, payload, created_at, updated_at}`` with ``payload``
# = the full document dict). Pure-Python (no DB, only asyncio.run).
# ---------------------------------------------------------------------------


class _FakeListCursor:
    """Async cursor returning fixed rows from ``fetchall``."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def __aenter__(self) -> "_FakeListCursor":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def execute(self, sql: str, params=()) -> None:
        return None

    async def fetchall(self) -> list[dict]:
        return list(self._rows)


class _FakeListConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def __aenter__(self) -> "_FakeListConn":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def cursor(self) -> _FakeListCursor:
        return _FakeListCursor(self._rows)


class _FakeListPool:
    """Fake pool exposing only ``connection()`` + ``schema``."""

    schema = "tcg_app_data"

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def connection(self) -> _FakeListConn:
        return _FakeListConn(self._rows)


def _pg_row(payload: dict, *, now) -> dict:
    """Wrap a payload dict in a PG-shaped row (projection columns + payload)."""
    return {
        "id": payload.get("id"),
        "type": payload.get("type"),
        "category": payload.get("category"),
        "payload": payload,
        "created_at": now,
        "updated_at": now,
    }


def _repo_with_rows(rows: list[dict]) -> WriteRepository:
    """Build a real ``WriteRepository`` whose ``_pool`` is a fake pool.

    ``_pool`` is slot-locked, so we bind it through ``object.__setattr__``
    exactly the way ``__init__`` does — the public surface is untouched.
    """
    repo = WriteRepository.__new__(WriteRepository)
    object.__setattr__(repo, "_pool", _FakeListPool(rows))
    return repo


def test_list_by_type_and_category_skips_malformed_doc() -> None:
    """One stored signal payload missing ``category`` must NOT crash the
    list — the valid doc is returned, the malformed one skipped (regression
    for the CRITICAL: ``from_pg_row`` in the repo list-comp must be guarded)."""
    import asyncio

    from datetime import datetime, timezone

    from tcg.types.persistence import Category, SignalDoc

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good = _pg_row(
        {
            "id": "good-sig",
            "type": "signal",
            "name": "Good",
            "category": "DEV",
            "created_at": now,
            "updated_at": now,
            "inputs": [],
            "rules": {},
            "settings": {},
            "description": "",
        },
        now=now,
    )
    # Malformed: payload missing ``category`` → from_json_doc raises KeyError.
    bad = _pg_row(
        {
            "id": "bad-sig",
            "type": "signal",
            "name": "Missing Category",
            "created_at": now,
            "updated_at": now,
        },
        now=now,
    )
    repo = _repo_with_rows([good, bad])

    result = asyncio.run(repo.list_by_type_and_category("signal", Category.DEV))

    ids = [d.id for d in result]
    assert ids == ["good-sig"], f"expected only the valid doc, got {ids}"
    assert isinstance(result[0], SignalDoc)


def test_list_by_type_and_category_skips_unknown_type_and_bad_category() -> None:
    """Two distinct malformed payloads (unknown ``type`` → ValueError, bad
    ``category`` value → ValueError) are both skipped while the good doc
    survives."""
    import asyncio

    from datetime import datetime, timezone

    from tcg.types.persistence import Category

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good = _pg_row(
        {
            "id": "good-ptf",
            "type": "portfolio",
            "name": "Good",
            "category": "RESEARCH",
            "created_at": now,
            "updated_at": now,
            "legs": [],
            "rebalance": "none",
        },
        now=now,
    )
    bad_type = _pg_row(
        {
            "id": "bad-type",
            "type": "not-a-real-type",
            "name": "Bad Type",
            "category": "RESEARCH",
            "created_at": now,
            "updated_at": now,
        },
        now=now,
    )
    bad_category = _pg_row(
        {
            "id": "bad-cat",
            "type": "portfolio",
            "name": "Bad Category",
            "category": "BOGUS",
            "created_at": now,
            "updated_at": now,
        },
        now=now,
    )
    repo = _repo_with_rows([good, bad_type, bad_category])

    result = asyncio.run(repo.list_by_type_and_category("portfolio", Category.RESEARCH))

    assert [d.id for d in result] == ["good-ptf"]


def test_list_by_type_skips_malformed_indicator_doc() -> None:
    """The indicator list path (``list_by_type``) has the same per-doc
    guard: a malformed indicator (payload missing required ``definition``)
    is skipped, valid ones returned."""
    import asyncio

    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good = _pg_row(
        {
            "id": "good-ind",
            "type": "indicator",
            "name": "Good",
            "definition": {"period": 14},
            "created_at": now,
            "updated_at": now,
            "deleted": False,
        },
        now=now,
    )
    # Payload missing required ``definition`` → ValueError in from_json_doc.
    bad = _pg_row(
        {
            "id": "bad-ind",
            "type": "indicator",
            "name": "Missing Definition",
            "created_at": now,
            "updated_at": now,
            "deleted": False,
        },
        now=now,
    )
    repo = _repo_with_rows([good, bad])

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
