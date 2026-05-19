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
