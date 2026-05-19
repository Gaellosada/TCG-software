"""Frozen dataclasses for the persistence (write) layer.

These three documents (``IndicatorDoc``, ``SignalDoc``, ``PortfolioDoc``)
all live in the single MongoDB collection ``tcg-app-data.2026-app-data``
and are distinguished by a ``type`` discriminator string. The collection
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
converts tuples ↔ JSON arrays at the Mongo boundary.

Category semantics
------------------
``Category`` applies to *signals and portfolios only*. Indicators have
no category and use a separate ``deleted: bool`` flag for soft-delete.

Serialization
-------------
The Mongo ``_id`` field maps to the dataclass ``id`` field on read; the
serializer emits ``_id`` on write. Tuples are converted to / from
lists at the Mongo boundary. All other fields round-trip verbatim
(datetimes stay as ``datetime`` objects — Mongo handles the BSON
encoding).
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


@dataclass(frozen=True, slots=True)
class IndicatorDoc:
    """Persisted indicator definition.

    ``definition`` is an opaque dict — the persistence layer does not
    interpret its contents. ``deleted`` is the soft-delete flag (no
    category for indicators).
    """

    id: str
    type: Literal["indicator"]
    name: str
    definition: dict
    created_at: datetime
    updated_at: datetime
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class SignalDoc:
    """Persisted signal — the full editable state of one signal.

    Editable payload fields (``inputs`` / ``rules`` / ``settings`` /
    ``description``) are opaque to this module. The frontend owns
    their inner shape. Persisted as tuples / dicts so the frozen
    dataclass stays immutable.
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


@dataclass(frozen=True, slots=True)
class PortfolioDoc:
    """Persisted portfolio — the full editable state of one portfolio.

    Editable payload fields (``legs`` / ``rebalance``) are opaque to
    this module. The frontend owns their inner shape.
    """

    id: str
    type: Literal["portfolio"]
    name: str
    category: Category
    created_at: datetime
    updated_at: datetime
    legs: tuple[dict, ...] = field(default_factory=tuple)
    rebalance: str = "none"


PersistenceDoc = IndicatorDoc | SignalDoc | PortfolioDoc

# Internal map: discriminator string → dataclass. Used by ``from_mongo_dict``.
_TYPE_TO_CLASS: dict[str, type] = {
    DocType.INDICATOR.value: IndicatorDoc,
    DocType.SIGNAL.value: SignalDoc,
    DocType.PORTFOLIO.value: PortfolioDoc,
}

# Fields that store a sequence on the dataclass as a tuple but must be
# emitted as a JSON list at the Mongo boundary. Keep this list explicit
# so accidental tuple-typed fields don't get auto-converted.
_TUPLE_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    DocType.INDICATOR.value: frozenset(),
    DocType.SIGNAL.value: frozenset({"inputs"}),
    DocType.PORTFOLIO.value: frozenset({"legs"}),
}


def to_mongo_dict(doc: PersistenceDoc) -> dict[str, Any]:
    """Serialize a persistence dataclass to a Mongo-ready dict.

    The dataclass ``id`` field is renamed to ``_id`` (Mongo's primary
    key). ``Category`` is unwrapped to its string value so Mongo stores
    a plain string rather than a Python enum. Tuple-typed fields are
    converted to JSON-compatible lists.
    """
    tuple_fields = _TUPLE_FIELDS_BY_TYPE.get(doc.type, frozenset())
    out: dict[str, Any] = {}
    for f in fields(doc):
        value = getattr(doc, f.name)
        if f.name == "id":
            out["_id"] = value
        elif isinstance(value, Category):
            out[f.name] = value.value
        elif f.name in tuple_fields and isinstance(value, tuple):
            out[f.name] = list(value)
        else:
            out[f.name] = value
    return out


def from_mongo_dict(d: dict[str, Any]) -> PersistenceDoc:
    """Reconstruct a persistence dataclass from a Mongo document.

    Uses the ``type`` field as the discriminator. ``_id`` maps back to
    ``id``. ``category`` strings are re-wrapped into the ``Category``
    enum. List-typed payload fields that map to tuple-typed dataclass
    fields are coerced to tuples. Raises ``ValueError`` if ``type`` is
    missing or unknown.
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
            if "_id" not in d:
                raise ValueError(
                    f"persistence: document missing '_id' for type={doc_type!r}"
                )
            kwargs["id"] = d["_id"]
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


__all__ = [
    "Category",
    "DocType",
    "IndicatorDoc",
    "SignalDoc",
    "PortfolioDoc",
    "PersistenceDoc",
    "to_mongo_dict",
    "from_mongo_dict",
]
