"""Frozen dataclasses for the persistence (write) layer.

These four documents (``IndicatorDoc``, ``SignalDoc``, ``PortfolioDoc``,
``BasketDoc``) are persisted in the PostgreSQL ``tcg_app_data`` schema —
one table per kind (``indicators`` / ``signals`` / ``portfolios`` /
``baskets``) — and each carries a ``type`` discriminator string. Storage
is treated as a flat, open-schema store: this module owns *structure*,
not *interpretation* of the inner payloads.

Opacity contract
----------------
All "interesting" content is carried as opaque dict / tuple payloads.
The persistence layer makes no claim about the inner shape of those
payloads — that contract belongs to whichever engine module *consumes*
them downstream. Adding new payload fields therefore does NOT require
changes here, by design.

For ``SignalDoc`` the editable content carried verbatim is:
  * ``inputs``     — tuple of input descriptors (instrument bindings)
  * ``rules``      — dict of rule sections (entries / exits / resets)
  * ``settings``   — dict of signal-level settings (e.g. dont_repeat)
  * ``description``— free-form documentation string

For ``PortfolioDoc`` the editable content carried verbatim is:
  * ``legs``       — tuple of leg descriptors (instrument or signal legs)
  * ``rebalance``  — rebalance frequency string (e.g. "none", "monthly")

Frozen-dataclass immutability is preserved by using tuples for the
list-typed fields (tuples are hashable / immutable). The serializer
converts tuples ↔ JSON arrays at the JSONB boundary.

Category semantics
------------------
``Category`` applies to *signals, portfolios and baskets*. Indicators
have no user category and use a separate ``deleted: bool`` flag for
soft-delete. The uniform soft-delete sentinel ``'DELETED'`` is set
server-side on the ``category`` projection column (see ``to_pg_row``);
it is NOT a user-facing ``Category`` member.

Serialization
-------------
``to_json_doc`` / ``from_json_doc`` map a dataclass ↔ a plain JSON-able
dict (the JSONB ``payload``). The dataclass ``id`` field stays ``id``
(the PostgreSQL primary-key column is ``id``). Tuples are converted to /
from lists. ``to_pg_row`` / ``from_pg_row`` wrap those to produce / read
the full table row: the ``payload`` JSONB is the single source of truth
and the top-level ``id`` / ``type`` / ``category`` / ``locked`` columns
are indexable projections used only by SQL filters.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal


class Category(StrEnum):
    """Workflow stage for signals and portfolios.

    No state machine is enforced — any value transition is allowed.
    ``ARCHIVE`` is the soft-delete target for signals/portfolios.
    """

    RESEARCH = "RESEARCH"
    DEV = "DEV"
    PROD = "PROD"
    ARCHIVE = "ARCHIVE"


class DocType(StrEnum):
    """Discriminator string for persistence documents.

    The three persistence document kinds share one MongoDB collection
    and are distinguished by the ``type`` field. Using a ``StrEnum``
    makes the discriminator a single source of truth — every runtime
    comparison goes through this class and renaming a type is one
    grep away rather than the previous 17-site sweep.
    """

    INDICATOR = "indicator"
    SIGNAL = "signal"
    PORTFOLIO = "portfolio"
    BASKET = "basket"


@dataclass(frozen=True, slots=True)
class IndicatorDoc:
    """Persisted indicator definition.

    ``definition`` is an opaque dict — the persistence layer does not
    interpret its contents. ``deleted`` is the soft-delete flag (no
    category for indicators). ``locked`` is the write-lock flag: a
    locked doc cannot be updated, recategorized, or archived/deleted —
    only the dedicated lock endpoint may flip it back.
    """

    id: str
    type: Literal["indicator"]
    name: str
    definition: dict
    created_at: datetime
    updated_at: datetime
    deleted: bool = False
    locked: bool = False


@dataclass(frozen=True, slots=True)
class SignalDoc:
    """Persisted signal — the full editable state of one signal.

    Editable payload fields (``inputs`` / ``rules`` / ``settings`` /
    ``description``) are opaque to this module. The frontend owns
    their inner shape. Persisted as tuples / dicts so the frozen
    dataclass stays immutable. ``locked`` is the write-lock flag:
    a locked signal cannot be updated, recategorized, or archived —
    only the dedicated lock endpoint may flip it back.
    """

    id: str
    type: Literal["signal"]
    name: str
    category: Category
    created_at: datetime
    updated_at: datetime
    inputs: tuple[dict, ...] = field(default_factory=tuple)
    rules: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    description: str = ""
    locked: bool = False


@dataclass(frozen=True, slots=True)
class PortfolioDoc:
    """Persisted portfolio — the full editable state of one portfolio.

    Editable payload fields (``legs`` / ``rebalance``) are opaque to
    this module. The frontend owns their inner shape. ``locked`` is the
    write-lock flag: a locked portfolio cannot be updated, recategorized,
    or archived — only the dedicated lock endpoint may flip it back.
    """

    id: str
    type: Literal["portfolio"]
    name: str
    category: Category
    created_at: datetime
    updated_at: datetime
    legs: tuple[dict, ...] = field(default_factory=tuple)
    rebalance: str = "none"
    locked: bool = False


@dataclass(frozen=True, slots=True)
class BasketDoc:
    """Persisted basket — a named, weighted, single-asset-class group of instruments.

    ``legs`` is an opaque tuple of leg descriptors.  The persistence layer
    does not interpret the inner shape — that belongs to the API layer which
    validates strict per-class mapping on write (each leg's
    ``instrument.type`` must match the envelope-declared ``asset_class``).

    The leg shape stored inside the tuple is the polymorphic form::

        {"instrument": {"type": "<spot|continuous|option_stream>", ...},
         "weight": float}

    where ``weight`` is a signed float fraction (negative = short) and
    the ``instrument`` sub-dict carries the full spec for the leg's
    underlying series (collection, adjustment, cycle, etc., depending
    on the discriminator).

    ``asset_class`` is the envelope-level declaration that constrains
    the per-leg ``instrument.type`` — ``"equity"``/``"index"`` →
    ``"spot"``, ``"future"`` → ``"continuous"``, ``"option"`` →
    ``"option_stream"``.  Defaulted to ``"equity"`` for backward
    compatibility with the iter-1/2 saved-basket shape (no production
    data — the basket collection is empty per ORDERS).
    """

    id: str
    type: Literal["basket"]
    name: str
    category: Category
    created_at: datetime
    updated_at: datetime
    legs: tuple[dict, ...] = field(default_factory=tuple)
    asset_class: str = "equity"


@dataclass(frozen=True, slots=True)
class TicketDoc:
    """A user-noted issue ticket — deliberately OUTSIDE the uniform store.

    A ticket is a single free-text note a user jots when they hit an
    issue. It is intentionally NOT part of the uniform 7-column
    (``id``/``type``/``category``/``locked``/``payload``/``created_at``/
    ``updated_at``) machinery: there is no ``type`` discriminator, no
    soft-delete ``category``, no ``locked`` flag, no JSONB payload, and
    no ``updated_at``. The backing table ``tcg_app_data.tickets`` has
    exactly three columns (``id text PK``, ``text text NOT NULL``,
    ``created_at timestamptz NOT NULL``).

    Consequently this type is excluded from :data:`PersistenceDoc`,
    ``_TYPE_TO_CLASS`` and the ``to_pg_row`` / ``from_pg_row`` helpers —
    tickets travel a self-contained code path (own dataclass, own
    repository methods, own SQL). Editing a ticket is an in-place
    ``UPDATE`` of ``text``; deletion is a HARD ``DELETE`` (it does NOT
    follow the uniform ``category='DELETED'`` soft-delete convention).
    """

    id: str
    text: str
    created_at: datetime


PersistenceDoc = IndicatorDoc | SignalDoc | PortfolioDoc | BasketDoc

# Internal map: discriminator string → dataclass. Used by ``from_json_doc``.
_TYPE_TO_CLASS: dict[str, type] = {
    DocType.INDICATOR.value: IndicatorDoc,
    DocType.SIGNAL.value: SignalDoc,
    DocType.PORTFOLIO.value: PortfolioDoc,
    DocType.BASKET.value: BasketDoc,
}

# Fields that store a sequence on the dataclass as a tuple but must be
# emitted as a JSON list at the JSONB boundary. Keep this list explicit
# so accidental tuple-typed fields don't get auto-converted.
_TUPLE_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    DocType.INDICATOR.value: frozenset(),
    DocType.SIGNAL.value: frozenset({"inputs"}),
    DocType.PORTFOLIO.value: frozenset({"legs"}),
    DocType.BASKET.value: frozenset({"legs"}),
}

# Soft-delete sentinel stored in the ``category`` projection column for a
# deleted document of ANY kind. NOT a user-facing ``Category`` member —
# it is set server-side by ``WriteRepository.archive`` and excluded from
# every list query. ``from_pg_row`` tolerates it on read.
DELETED_CATEGORY = "DELETED"


def to_json_doc(doc: PersistenceDoc) -> dict[str, Any]:
    """Serialize a persistence dataclass to a plain JSON-able dict.

    This is the JSONB ``payload`` form. The dataclass ``id`` field stays
    ``id`` (the PostgreSQL primary-key column is ``id``). ``Category`` is
    unwrapped to its string value so the payload stores a plain string
    rather than a Python enum. Tuple-typed fields are converted to
    JSON-compatible lists.
    """
    tuple_fields = _TUPLE_FIELDS_BY_TYPE.get(doc.type, frozenset())
    out: dict[str, Any] = {}
    for f in fields(doc):
        value = getattr(doc, f.name)
        if isinstance(value, Category):
            out[f.name] = value.value
        elif f.name in tuple_fields and isinstance(value, tuple):
            out[f.name] = list(value)
        else:
            out[f.name] = value
    return out


def from_json_doc(d: dict[str, Any]) -> PersistenceDoc:
    """Reconstruct a persistence dataclass from a JSON-able dict.

    Uses the ``type`` field as the discriminator. ``category`` strings are
    re-wrapped into the ``Category`` enum. List-typed payload fields that
    map to tuple-typed dataclass fields are coerced to tuples. Raises
    ``ValueError`` if ``type`` is missing/unknown or a required field is
    absent.
    """
    doc_type = d.get("type")
    if doc_type not in _TYPE_TO_CLASS:
        raise ValueError(
            f"persistence: unknown or missing 'type' discriminator: {doc_type!r}"
        )
    cls = _TYPE_TO_CLASS[doc_type]
    tuple_fields = _TUPLE_FIELDS_BY_TYPE.get(doc_type, frozenset())
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name == "id":
            if "id" not in d:
                raise ValueError(
                    f"persistence: document missing 'id' for type={doc_type!r}"
                )
            kwargs["id"] = d["id"]
        elif f.name == "category":
            kwargs["category"] = Category(d["category"])
        else:
            # Use the default for optional fields when absent from the
            # stored document — supports forward-compatibility with
            # older docs that predate a new field.
            if f.name in d:
                value = d[f.name]
                # Coerce stored lists back to tuples for tuple-typed
                # fields so the round-trip preserves equality of frozen
                # dataclasses.
                if f.name in tuple_fields and isinstance(value, list):
                    value = tuple(value)
                kwargs[f.name] = value
            elif f.default is not MISSING:
                kwargs[f.name] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                kwargs[f.name] = f.default_factory()  # type: ignore[misc]
            else:
                raise ValueError(
                    f"persistence: document missing required field "
                    f"{f.name!r} for type={doc_type!r}"
                )
    return cls(**kwargs)  # type: ignore[no-any-return]


def _category_projection(doc: PersistenceDoc) -> str | None:
    """Derive the value for the ``category`` projection column.

    - Indicators have no user category: ``'DELETED'`` when ``deleted`` is
      set, else ``None`` (active).
    - Signals / portfolios / baskets project their ``Category`` value.

    The ``'DELETED'`` sentinel on non-indicator kinds is written by
    ``WriteRepository.archive`` directly (not through this helper), so a
    normal ``create`` / ``update`` only ever projects a real category
    here.
    """
    if isinstance(doc, IndicatorDoc):
        return DELETED_CATEGORY if doc.deleted else None
    category = getattr(doc, "category", None)
    if isinstance(category, Category):
        return category.value
    return category


def _locked_projection(doc: PersistenceDoc) -> bool | None:
    """Derive the value for the ``locked`` projection column.

    Baskets have no ``locked`` column (not lockable) → ``None``. The
    three lockable kinds project their stored ``locked`` flag.
    """
    return getattr(doc, "locked", None)


# Timestamp fields kept in the dedicated ``timestamptz`` columns rather
# than inside the JSONB payload — JSON has no native datetime, and the
# columns are the authoritative source for CAS + ordering. ``to_pg_row``
# strips them from the stored payload; ``from_pg_row`` re-injects the
# column values before reconstructing the dataclass.
_TIMESTAMP_FIELDS = ("created_at", "updated_at")


def to_pg_row(
    doc: PersistenceDoc,
) -> tuple[str, str, str | None, bool | None, dict[str, Any], datetime, datetime]:
    """Project a persistence dataclass into a ``tcg_app_data`` table row.

    Returns ``(id, type, category, locked, payload, created_at, updated_at)``.
    ``payload`` is the full document MINUS the two timestamp fields (those
    live in the dedicated ``timestamptz`` columns, since JSON has no
    datetime type); it remains the source of truth for all other content.
    The scalar columns are indexable projections used by SQL filters:
    ``category`` is ``None`` for an active indicator and ``locked`` is
    ``None`` for a basket (no such column).
    """
    payload = to_json_doc(doc)
    for ts in _TIMESTAMP_FIELDS:
        payload.pop(ts, None)
    return (
        doc.id,
        doc.type,
        _category_projection(doc),
        _locked_projection(doc),
        payload,
        doc.created_at,
        doc.updated_at,
    )


def from_pg_row(row: dict[str, Any]) -> PersistenceDoc:
    """Reconstruct a persistence dataclass from a ``tcg_app_data`` row.

    The ``payload`` JSONB is authoritative for content; the ``created_at``
    / ``updated_at`` columns supply the timestamps (stripped from the
    payload on write). The other scalar projection columns (``category`` /
    ``locked`` / ``id`` / ``type``) are deliberately NOT used to populate
    content — they exist only for SQL filtering. psycopg returns JSONB as
    a parsed ``dict`` and ``timestamptz`` as a tz-aware ``datetime``.
    """
    payload = row["payload"]
    if not isinstance(payload, dict):
        raise ValueError(
            f"persistence: row id={row.get('id')!r} has a non-dict payload"
        )
    # Re-inject the authoritative timestamp columns so the full document
    # is reconstructed. Copy so we don't mutate the caller's row dict.
    full = dict(payload)
    full["created_at"] = row["created_at"]
    full["updated_at"] = row["updated_at"]
    return from_json_doc(full)


__all__ = [
    "Category",
    "DocType",
    "DELETED_CATEGORY",
    "IndicatorDoc",
    "SignalDoc",
    "PortfolioDoc",
    "BasketDoc",
    "TicketDoc",
    "PersistenceDoc",
    "to_json_doc",
    "from_json_doc",
    "to_pg_row",
    "from_pg_row",
]
