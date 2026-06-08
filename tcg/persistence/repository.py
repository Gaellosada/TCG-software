"""WriteRepository — the only object the application uses to mutate
the application write collection (``tcg-app-data.2026-app-data`` by default).

Safety design
-------------
The collection handle is bound exactly once in ``__init__``:

    self._coll = client[db_name][collection_name]

``db_name`` and ``collection_name`` are resolved from env vars by the
factory in ``_persistence_wiring.py`` and injected at construction time.
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

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal

import pymongo.errors
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.types.persistence import (
    Category,
    DocType,
    IndicatorDoc,
    PersistenceDoc,
    from_mongo_dict,
    to_mongo_dict,
)

_log = logging.getLogger(__name__)


def _deserialize_skipping_malformed(rows: list[dict[str, Any]]) -> list[PersistenceDoc]:
    """Deserialize raw Mongo docs, skipping + logging any malformed one.

    ``from_mongo_dict`` raises ``ValueError`` / ``KeyError`` on a stored
    document with a missing/unknown ``type``, a missing required field
    (e.g. ``category``), or an unparseable ``category`` value. A single
    such legacy / partial-write doc must NOT take down the entire list
    endpoint — without this guard the list-comprehension propagated the
    error as an unhandled 500 and the user saw zero signals / portfolios /
    baskets for the whole category instead of "the good ones minus one".

    The API layer's own skip-loop only wraps ``_checked`` / ``_to_out``
    and never reaches a deserialization failure (the repo returned before
    the API saw the docs), so the guard MUST live here. We skip the bad
    doc, log it (with its ``_id`` when present) at WARNING with a
    traceback, and return the docs that deserialized cleanly.
    """
    out: list[PersistenceDoc] = []
    for r in rows:
        try:
            out.append(from_mongo_dict(r))
        except (ValueError, KeyError, TypeError) as exc:
            _log.warning(
                "persistence: skipping malformed stored doc _id=%r type=%r: %s",
                r.get("_id", "?") if isinstance(r, dict) else "?",
                r.get("type", "?") if isinstance(r, dict) else "?",
                exc,
                exc_info=True,
            )
    return out


class DocumentTooLargeError(Exception):
    """Raised when a persistence write hits MongoDB's 16 MB doc limit.

    Wraps :class:`pymongo.errors.DocumentTooLarge` so the API layer can
    map it to 413 without leaking PyMongo types across the boundary.
    """


class ConcurrentUpdateError(Exception):
    """Raised by :meth:`WriteRepository.update` when the optimistic
    check-and-set guard sees ``updated_at`` has moved since the
    pre-image was read. The API layer maps this to 409.
    """


def _utcnow() -> datetime:
    """Single source of truth for server-set timestamps.

    Always UTC, always timezone-aware. BSON datetimes are
    millisecond-resolution, so we truncate sub-millisecond microseconds
    here — that way the in-memory dataclass returned by ``create`` /
    ``update`` compares equal to the same doc fetched back from Mongo.
    """
    now = datetime.now(timezone.utc)
    truncated_us = (now.microsecond // 1000) * 1000
    return now.replace(microsecond=truncated_us)


class WriteRepository:
    """Application-side write surface for the persistence layer.

    Instantiated once per process with a ``client`` built from
    ``build_write_client()``, plus the resolved ``db_name`` and
    ``collection_name`` from env vars. The collection handle is bound
    on construction and never re-derived.

    Immutability of ``_coll``
    -------------------------
    ``_coll`` is bound once in ``__init__`` via ``object.__setattr__``.
    Ordinary attribute writes (``repo._coll = other``, ``repo.alias = x``)
    are blocked by the combination of ``__slots__`` and an unconditional
    ``__setattr__`` guard, so accidental rebinds from typos or refactors
    fail loud rather than silently re-targeting the namespace.

    This is *defense in depth*, not a cryptographic seal: an attacker
    holding Python-level code execution can still reach the slot via
    ``object.__setattr__(repo, '_coll', ...)`` (no class can prevent
    that — base-class ``__setattr__`` is always reachable). The real
    namespace boundary lives on the Mongo server: the ``app-writer``
    role grants ``readWrite`` only on the configured collection, so a
    repo handle pointed at any other database / collection fails the
    next operation with ``OperationFailure`` regardless of what the
    Python object thinks it's holding.

    Tests:
    - ``test_coll_attribute_is_immutable_after_construction``:
      ordinary writes raise (defense in depth, layer 1).
    - ``test_cross_namespace_write_blocked_by_mongo_role``
      (integration, live Mongo): ``object.__setattr__`` rebind followed
      by a write fails with ``OperationFailure`` (layer 2).
    """

    __slots__ = ("_coll",)

    def __init__(
        self,
        client: AsyncIOMotorClient,
        *,
        db_name: str,
        collection_name: str,
    ) -> None:
        # Bind ONCE. We deliberately keep only ``self._coll`` so that
        # nothing in the rest of this class can navigate to another
        # database or collection through ``self``. ``object.__setattr__``
        # is used to bypass our own ``__setattr__`` guard for the single
        # legitimate write during construction.
        object.__setattr__(self, "_coll", client[db_name][collection_name])

    def __setattr__(self, name: str, value: object) -> None:
        """Reject any post-construction attribute mutation via the
        ordinary attribute-assignment syntax.

        Combined with ``__slots__``, this means ``repo._coll = other``
        — or any other attribute assignment — raises rather than
        silently re-binding the namespace handle. This catches
        accidental rebinds from typos or refactors but is NOT a
        cryptographic seal: ``object.__setattr__(repo, '_coll', x)``
        still reaches the slot (no class can intercept that). The
        ultimate namespace guarantee comes from the Mongo server-side
        ``app-writer`` role, not from this method.
        """
        raise AttributeError(
            "WriteRepository is immutable after construction; "
            f"cannot set attribute {name!r}"
        )

    async def create(self, doc: PersistenceDoc) -> PersistenceDoc:
        """Insert ``doc`` and return the stored copy.

        ``created_at`` and ``updated_at`` are overwritten server-side
        with the current UTC instant — callers cannot back-date docs.
        Raises :class:`DocumentTooLargeError` when the resulting BSON
        document would exceed MongoDB's 16 MB cap.
        """
        now = _utcnow()
        stamped = replace(doc, created_at=now, updated_at=now)
        try:
            await self._coll.insert_one(to_mongo_dict(stamped))
        except pymongo.errors.DocumentTooLarge as exc:
            raise DocumentTooLargeError(
                f"persistence: {doc.type} doc id={doc.id!r} exceeds "
                f"MongoDB's 16 MB BSON document limit"
            ) from exc
        return stamped

    async def get_by_id(
        self,
        doc_type: Literal["indicator", "signal", "portfolio", "basket"],
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
        if doc_type != DocType.INDICATOR.value:
            raise ValueError(
                f"list_by_type only supports 'indicator', got {doc_type!r}. "
                "Use list_by_type_and_category for signals and portfolios."
            )
        cursor = self._coll.find(
            {"type": DocType.INDICATOR.value, "deleted": {"$ne": True}}
        )
        rows = await cursor.to_list(length=None)
        # Per-doc guard: a single malformed stored doc must not 500 the
        # whole list (see _deserialize_skipping_malformed).
        return _deserialize_skipping_malformed(rows)  # type: ignore[return-value]

    async def list_by_type_and_category(
        self,
        doc_type: Literal["signal", "portfolio", "basket"],
        category: Category,
    ) -> list[PersistenceDoc]:
        """Return all docs of the given type filtered by category.

        ``ARCHIVE`` is a legal category to query — that's how the UI
        surfaces archived items.
        """
        cursor = self._coll.find({"type": doc_type, "category": category.value})
        rows = await cursor.to_list(length=None)
        # Per-doc guard: a single malformed stored doc must not 500 the
        # whole list (see _deserialize_skipping_malformed).
        return _deserialize_skipping_malformed(rows)

    async def update(
        self,
        doc: PersistenceDoc,
        *,
        expected_updated_at: datetime | None = None,
    ) -> PersistenceDoc:
        """Replace the doc identified by ``(_id, type)`` with ``doc``.

        ``updated_at`` is bumped to the current UTC instant.
        ``created_at`` is preserved verbatim from ``doc``.

        Concurrency: when ``expected_updated_at`` is supplied the
        filter is extended to ``{_id, type, updated_at: expected}`` —
        a check-and-set. If no document matches, the method first
        checks whether the doc exists at all:

        - exists with a *different* ``updated_at`` → another writer
          modified it since we read it → raise
          :class:`ConcurrentUpdateError`.
        - does not exist → raise ``KeyError`` (same as before).

        Without ``expected_updated_at`` the previous semantics hold:
        match by ``(_id, type)`` only, ``KeyError`` on miss. Callers
        that don't supply the token accept last-writer-wins.

        Also raises :class:`DocumentTooLargeError` when the replacement
        BSON would exceed MongoDB's 16 MB cap.
        """
        bumped = replace(doc, updated_at=_utcnow())
        payload = to_mongo_dict(bumped)
        filter_: dict = {"_id": doc.id, "type": doc.type}
        if expected_updated_at is not None:
            filter_["updated_at"] = expected_updated_at
        try:
            result = await self._coll.replace_one(filter_, payload)
        except pymongo.errors.DocumentTooLarge as exc:
            raise DocumentTooLargeError(
                f"persistence: {doc.type} doc id={doc.id!r} exceeds "
                f"MongoDB's 16 MB BSON document limit"
            ) from exc
        if result.matched_count == 0:
            # Disambiguate: was the doc gone (404) or did its
            # ``updated_at`` move under us (409)?
            if expected_updated_at is not None:
                still_there = await self._coll.find_one(
                    {"_id": doc.id, "type": doc.type}, projection={"_id": 1}
                )
                if still_there is not None:
                    raise ConcurrentUpdateError(
                        f"persistence: {doc.type} id={doc.id!r} was modified "
                        f"concurrently — refusing to overwrite"
                    )
            raise KeyError(f"persistence: no {doc.type} with id={doc.id!r} to update")
        return bumped

    async def archive(
        self,
        doc_type: Literal["indicator", "signal", "portfolio", "basket"],
        doc_id: str,
    ) -> None:
        """Soft-delete the doc.

        - ``signal`` / ``portfolio`` / ``basket``: set ``category = ARCHIVE``.
        - ``indicator``: set ``deleted = True``.

        Raises ``KeyError`` if the doc does not exist. Idempotent
        otherwise — archiving an already-archived doc just refreshes
        ``updated_at``.
        """
        now = _utcnow()
        if doc_type == DocType.INDICATOR.value:
            update_payload = {"$set": {"deleted": True, "updated_at": now}}
        elif doc_type in (
            DocType.SIGNAL.value,
            DocType.PORTFOLIO.value,
            DocType.BASKET.value,
        ):
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
            raise KeyError(f"persistence: no {doc_type} with id={doc_id!r} to archive")


__all__ = [
    "WriteRepository",
    "ConcurrentUpdateError",
    "DocumentTooLargeError",
]
