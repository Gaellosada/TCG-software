"""Frozen dataclasses for the persistence (write) layer.

These three documents (``IndicatorDoc``, ``SignalDoc``, ``PortfolioDoc``)
all live in the single MongoDB collection ``tcg-instrument.2026-app-data``
and are distinguished by a ``type`` discriminator string. The collection
is treated as a flat, open-schema store: this module owns *structure*,
not *interpretation* of the inner payloads.

Opacity contract
----------------
``IndicatorDoc.definition`` is an opaque ``dict``. ``SignalDoc.blocks``
is an opaque ``list[dict]``. ``PortfolioDoc.instruments`` and
``PortfolioDoc.rebalance`` are opaque ``dict`` / ``list[dict]`` payloads.
The persistence layer makes no claim about the inner shape of those
payloads — that contract belongs to whichever engine module *consumes*
them downstream. Adding new payload fields therefore does NOT require
changes here, by design.

Category semantics
------------------
``Category`` applies to *signals and portfolios only*. Indicators have
no category and use a separate ``deleted: bool`` flag for soft-delete.
This split is intentional: indicators are reusable building blocks
without a workflow stage, while signals and portfolios move through
``RESEARCH → DEV → PROD`` (or ``ARCHIVE``) as the user iterates.

Serialization
-------------
The Mongo ``_id`` field maps to the dataclass ``id`` field on read; the
serializer emits ``_id`` on write. All other fields round-trip
verbatim (datetimes stay as ``datetime`` objects — Mongo handles the
BSON encoding).
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
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
    """Persisted signal: an ordered list of weighted condition blocks.

    ``blocks`` is an opaque ``list[dict]``. ``category`` drives the
    workflow stage and doubles as the soft-archive marker
    (``Category.ARCHIVE``).
    """

    id: str
    type: Literal["signal"]
    name: str
    blocks: list[dict]
    category: Category
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PortfolioDoc:
    """Persisted portfolio definition.

    ``instruments`` and ``rebalance`` are opaque payloads. ``category``
    drives the workflow stage and the soft-archive marker.
    """

    id: str
    type: Literal["portfolio"]
    name: str
    instruments: list[dict]
    rebalance: dict
    category: Category
    created_at: datetime
    updated_at: datetime


PersistenceDoc = IndicatorDoc | SignalDoc | PortfolioDoc

# Internal map: discriminator string → dataclass. Used by ``from_mongo_dict``.
_TYPE_TO_CLASS: dict[str, type] = {
    "indicator": IndicatorDoc,
    "signal": SignalDoc,
    "portfolio": PortfolioDoc,
}


def to_mongo_dict(doc: PersistenceDoc) -> dict[str, Any]:
    """Serialize a persistence dataclass to a Mongo-ready dict.

    The dataclass ``id`` field is renamed to ``_id`` (Mongo's primary
    key). ``Category`` is unwrapped to its string value so Mongo stores
    a plain string rather than a Python enum.
    """
    out: dict[str, Any] = {}
    for f in fields(doc):
        value = getattr(doc, f.name)
        if f.name == "id":
            out["_id"] = value
        elif isinstance(value, Category):
            out[f.name] = value.value
        else:
            out[f.name] = value
    return out


def from_mongo_dict(d: dict[str, Any]) -> PersistenceDoc:
    """Reconstruct a persistence dataclass from a Mongo document.

    Uses the ``type`` field as the discriminator. ``_id`` maps back to
    ``id``. ``category`` strings are re-wrapped into the ``Category``
    enum. Raises ``ValueError`` if ``type`` is missing or unknown.
    """
    doc_type = d.get("type")
    if doc_type not in _TYPE_TO_CLASS:
        raise ValueError(
            f"persistence: unknown or missing 'type' discriminator: {doc_type!r}"
        )
    cls = _TYPE_TO_CLASS[doc_type]
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
            # Use the default for optional fields (e.g. IndicatorDoc.deleted)
            # when absent from the stored document — supports forward-
            # compatibility with older docs that predate a new field.
            if f.name in d:
                kwargs[f.name] = d[f.name]
            elif f.default is not MISSING:
                kwargs[f.name] = f.default
            else:
                raise ValueError(
                    f"persistence: document missing required field "
                    f"{f.name!r} for type={doc_type!r}"
                )
    return cls(**kwargs)  # type: ignore[no-any-return]


__all__ = [
    "Category",
    "IndicatorDoc",
    "SignalDoc",
    "PortfolioDoc",
    "PersistenceDoc",
    "to_mongo_dict",
    "from_mongo_dict",
]
