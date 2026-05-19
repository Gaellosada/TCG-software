"""WriteRepository — the only object the application uses to mutate
``tcg-instrument.2026-app-data``.

Safety design
-------------
The collection handle is bound exactly once in ``__init__``:

    self._coll = client["tcg-instrument"]["2026-app-data"]

No public method accepts a collection name. No public method calls
``client[...]`` or ``db[...]``. There is no ``__getattr__`` escape
hatch. This guarantees — at the Python class boundary — that every
``insert/find/update/delete`` issued by application code targets the
one authorised namespace. Combined with the server-side role on
``app-writer``, two independent layers must fail before a write
reaches any other collection.

Method contract
---------------
- ``create``: stamps ``created_at`` and ``updated_at`` server-side
  (overwriting whatever the caller passed), returns the stored doc.
- ``get_by_id``: filters by ``(_id, type)`` so a stray id collision
  across types cannot return the wrong dataclass.
- ``list_by_type`` / ``list_by_type_and_category``: read-side helpers
  scoped to this repository's collection. Indicators have no category;
  signals and portfolios do.
- ``update``: full-document replace by ``(_id, type)``; bumps
  ``updated_at``. Raises ``KeyError`` if the doc does not exist (we do
  not silently upsert — that would mask a stale-id bug in the caller).
- ``archive``: soft-delete. For signals/portfolios, sets ``category``
  to ``ARCHIVE``. For indicators, sets ``deleted=True``. Both bump
  ``updated_at``. Raises ``KeyError`` if missing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

from motor.motor_asyncio import AsyncIOMotorClient

from tcg.types.persistence import (
    Category,
    IndicatorDoc,
    PersistenceDoc,
    SignalDoc,
    PortfolioDoc,
    from_mongo_dict,
    to_mongo_dict,
)


_DB_NAME = "tcg-instrument"
_COLLECTION_NAME = "2026-app-data"


def _utcnow() -> datetime:
    """Single source of truth for server-set timestamps.

    Always UTC, always timezone-aware. Mongo preserves the tzinfo on
    round-trip (Motor encodes as BSON ``datetime`` with UTC offset).
    """
    return datetime.now(timezone.utc)


class WriteRepository:
    """Application-side write surface for the persistence layer.

    Instantiated once per process with a ``client`` built from
    ``build_write_client()``. The collection handle is bound on
    construction and never re-derived.
    """

    def __init__(self, client: AsyncIOMotorClient) -> None:
        # Bind ONCE. We deliberately keep only ``self._coll`` so that
        # nothing in the rest of this class can navigate to another
        # database or collection through ``self``.
        self._coll = client[_DB_NAME][_COLLECTION_NAME]

    async def create(self, doc: PersistenceDoc) -> PersistenceDoc:
        """Insert ``doc`` and return the stored copy.

        ``created_at`` and ``updated_at`` are overwritten server-side
        with the current UTC instant — callers cannot back-date docs.
        """
        now = _utcnow()
        stamped = replace(doc, created_at=now, updated_at=now)
        await self._coll.insert_one(to_mongo_dict(stamped))
        return stamped

    async def get_by_id(
        self,
        doc_type: Literal["indicator", "signal", "portfolio"],
        doc_id: str,
    ) -> PersistenceDoc | None:
        """Return the doc with id ``doc_id`` and type ``doc_type``.

        ``None`` when no matching document exists. Filtering by both
        keys guards against a hypothetical id collision across types.
        """
        raw = await self._coll.find_one({"_id": doc_id, "type": doc_type})
        if raw is None:
            return None
        return from_mongo_dict(raw)

    async def list_by_type(
        self,
        doc_type: Literal["indicator"],
    ) -> list[IndicatorDoc]:
        """Return all *active* (non-deleted) docs of the given type.

        Currently only used for indicators (the only type with a
        ``deleted`` flag rather than a category). Returns an empty list
        if the collection has none.
        """
        # The literal narrowing in the signature already restricts
        # callers, but assert at runtime to keep the soft-delete query
        # honest if the literal ever expands.
        if doc_type != "indicator":
            raise ValueError(
                f"list_by_type only supports 'indicator', got {doc_type!r}. "
                "Use list_by_type_and_category for signals and portfolios."
            )
        cursor = self._coll.find({"type": "indicator", "deleted": {"$ne": True}})
        rows = await cursor.to_list(length=None)
        return [from_mongo_dict(r) for r in rows]  # type: ignore[misc]

    async def list_by_type_and_category(
        self,
        doc_type: Literal["signal", "portfolio"],
        category: Category,
    ) -> list[PersistenceDoc]:
        """Return all docs of the given type filtered by category.

        ``ARCHIVE`` is a legal category to query — that's how the UI
        surfaces archived items.
        """
        cursor = self._coll.find(
            {"type": doc_type, "category": category.value}
        )
        rows = await cursor.to_list(length=None)
        return [from_mongo_dict(r) for r in rows]

    async def update(self, doc: PersistenceDoc) -> PersistenceDoc:
        """Replace the doc identified by ``(_id, type)`` with ``doc``.

        ``updated_at`` is bumped to the current UTC instant.
        ``created_at`` is preserved verbatim from ``doc``. Raises
        ``KeyError`` if no document matched — callers must surface a
        404 rather than silently upsert.
        """
        bumped = replace(doc, updated_at=_utcnow())
        payload = to_mongo_dict(bumped)
        result = await self._coll.replace_one(
            {"_id": doc.id, "type": doc.type}, payload
        )
        if result.matched_count == 0:
            raise KeyError(
                f"persistence: no {doc.type} with id={doc.id!r} to update"
            )
        return bumped

    async def archive(
        self,
        doc_type: Literal["indicator", "signal", "portfolio"],
        doc_id: str,
    ) -> None:
        """Soft-delete the doc.

        - ``signal`` / ``portfolio``: set ``category = ARCHIVE``.
        - ``indicator``: set ``deleted = True``.

        Raises ``KeyError`` if the doc does not exist. Idempotent
        otherwise — archiving an already-archived doc just refreshes
        ``updated_at``.
        """
        now = _utcnow()
        if doc_type == "indicator":
            update_payload = {"$set": {"deleted": True, "updated_at": now}}
        elif doc_type in ("signal", "portfolio"):
            update_payload = {
                "$set": {
                    "category": Category.ARCHIVE.value,
                    "updated_at": now,
                }
            }
        else:
            raise ValueError(f"unknown doc_type: {doc_type!r}")
        result = await self._coll.update_one(
            {"_id": doc_id, "type": doc_type}, update_payload
        )
        if result.matched_count == 0:
            raise KeyError(
                f"persistence: no {doc_type} with id={doc_id!r} to archive"
            )


# Concrete subclasses so callers can spell the return types explicitly
# at the API edge (handlers want ``IndicatorDoc``, not the broad union).
# Not strictly necessary — the methods already preserve the dataclass
# identity — but documented here so static analyzers see the link.
_DOC_CLASSES: tuple[type, ...] = (IndicatorDoc, SignalDoc, PortfolioDoc)


__all__ = ["WriteRepository"]
