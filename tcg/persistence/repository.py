"""WriteRepository — the only object the application uses to mutate the
app-data store (PostgreSQL schema ``tcg_app_data``).

Storage model
-------------
One table per document kind — ``indicators`` / ``signals`` /
``portfolios`` / ``baskets`` (PLURAL table names; the stored ``type``
discriminator is SINGULAR: ``indicator`` / ``signal`` / ``portfolio`` /
``basket``). Every table has columns::

    id text PK | type text | category text | locked boolean | payload jsonb
    | created_at timestamptz | updated_at timestamptz

except ``baskets`` which has NO ``locked`` column (baskets are not
lockable). ``payload`` (JSONB) carries the full document and is the
single source of truth; the scalar columns are indexable projections
used only by the WHERE clauses here.

Soft-delete (uniform model)
---------------------------
``archive`` is a soft delete: it sets the ``category`` projection column
to the sentinel ``'DELETED'`` for EVERY kind (and, for indicators, also
flips ``payload.deleted = true`` so the reconstructed dataclass and the
``IndicatorOut.deleted`` flag stay consistent). Every list method
EXCLUDES ``category = 'DELETED'``. ``'DELETED'`` is server-set only and
is NOT a user-facing ``Category`` value; the payload always retains a
valid ``Category`` so a deleted doc still round-trips through
``from_pg_row`` without an enum error.

Safety design
-------------
The pool handle is bound exactly once in ``__init__`` (``self._pool``)
via ``object.__setattr__``; ``__slots__`` + an unconditional
``__setattr__`` guard make any post-construction rebind fail loud. No
public method takes a table / schema name. The ultimate namespace
guarantee is the server-side ``tcg_app_rw`` grant (SELECT/INSERT/UPDATE/
DELETE on the four ``tcg_app_data`` tables only — no access to
``tcg_instruments``).

Method contract
---------------
- ``create``: stamps ``created_at`` + ``updated_at`` server-side, INSERTs.
  Raises :class:`DuplicateIdError` on a primary-key collision (409).
- ``get_by_id``: filters by ``(id, type)``.
- ``list_by_type`` / ``list_by_type_and_category``: read-side helpers,
  both excluding soft-deleted docs.
- ``update``: full-document replace by ``(id, type)``; bumps
  ``updated_at``. Optimistic CAS via ``expected_updated_at``. Lock guard
  (stored ``locked``) runs first / atomically.
- ``archive``: soft delete (category → ``'DELETED'``). Lock-guarded,
  TOCTOU-atomic.
- ``set_locked``: the ONLY mutation that bypasses the lock guard.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from tcg.types.persistence import (
    DELETED_CATEGORY,
    Category,
    DocType,
    IndicatorDoc,
    PersistenceDoc,
    TicketDoc,
    from_pg_row,
    to_pg_row,
)
from tcg.persistence._pg import AppDbConnectionPool

_log = logging.getLogger(__name__)


# Singular ``type`` discriminator → PLURAL table name. Single explicit
# map (mirrors DocType) so the plural-table / singular-type split never
# gets conflated.
_TABLE_BY_TYPE: dict[str, str] = {
    DocType.INDICATOR.value: "indicators",
    DocType.SIGNAL.value: "signals",
    DocType.PORTFOLIO.value: "portfolios",
    DocType.BASKET.value: "baskets",
}

# Kinds that carry a ``locked`` column (everything except baskets).
_LOCKABLE_TYPES = frozenset(
    {DocType.INDICATOR.value, DocType.SIGNAL.value, DocType.PORTFOLIO.value}
)


def _deserialize_skipping_malformed(
    rows: list[dict[str, Any]],
) -> list[PersistenceDoc]:
    """Deserialize raw PG rows, skipping + logging any malformed one.

    ``from_pg_row`` raises ``ValueError`` / ``KeyError`` on a row with a
    missing/unknown ``type`` in its payload, a missing required field, or
    an unparseable ``category``. A single such partial-write / legacy doc
    must NOT take down the whole list endpoint — without this guard the
    list-comprehension propagated the error as an unhandled 500 and the
    user saw zero docs for the entire category. We skip + log the bad doc
    (with its ``id`` when present) and return the docs that deserialized
    cleanly.
    """
    out: list[PersistenceDoc] = []
    for r in rows:
        try:
            out.append(from_pg_row(r))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            _log.warning(
                "persistence: skipping malformed stored doc id=%r type=%r: %s",
                r.get("id", "?") if isinstance(r, dict) else "?",
                r.get("type", "?") if isinstance(r, dict) else "?",
                exc,
                exc_info=True,
            )
    return out


class DuplicateIdError(Exception):
    """Raised by :meth:`WriteRepository.create` when the primary key
    already exists (PostgreSQL ``UniqueViolation``, SQLSTATE ``23505``).

    Owned by ``tcg.persistence`` so the API layer can map it to 409
    without leaking a psycopg type across the boundary.
    """


class DocumentTooLargeError(Exception):
    """Raised when a persistence write exceeds the configured payload cap.

    Retained for contract compatibility (the API maps it to 413). The
    transport-level depth/size guards in the API layer normally catch an
    oversized payload long before a write; this is the last-ditch case.
    """


class ConcurrentUpdateError(Exception):
    """Raised by :meth:`WriteRepository.update` when the optimistic
    check-and-set guard sees ``updated_at`` has moved since the pre-image
    was read. The API layer maps this to 409.
    """


class LockedError(Exception):
    """Raised by :meth:`WriteRepository.update` / :meth:`WriteRepository.archive`
    when the *stored* document has ``locked == True``.

    The guard reads the persisted ``locked`` flag (NOT the incoming
    payload — a client cannot escape the lock by sending ``locked: false``
    in an update), and refuses to mutate. Only :meth:`set_locked` bypasses
    it. The API layer maps this to HTTP 423 (Locked).
    """


def _utcnow() -> datetime:
    """Single source of truth for server-set timestamps — always UTC,
    always tz-aware. PostgreSQL ``timestamptz`` keeps microsecond
    precision, which we preserve (no truncation) so the value returned by
    ``create`` / ``update`` equals the stored value for CAS purposes.
    """
    return datetime.now(timezone.utc)


class WriteRepository:
    """Application-side write surface for the app-data persistence layer.

    Instantiated once per process with an :class:`AppDbConnectionPool`.
    The pool handle is bound on construction and never re-derived.

    Immutability of ``_pool``
    -------------------------
    ``_pool`` is bound once in ``__init__`` via ``object.__setattr__``.
    Ordinary attribute writes (``repo._pool = other`` / ``repo.alias = x``)
    are blocked by ``__slots__`` + an unconditional ``__setattr__`` guard,
    so accidental rebinds from typos or refactors fail loud rather than
    silently re-targeting the store.

    This is defence in depth, not a cryptographic seal: code with Python
    execution can still reach the slot via ``object.__setattr__`` (no
    class can prevent that). The real namespace boundary is the
    ``tcg_app_rw`` server-side grant.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: AppDbConnectionPool) -> None:
        # Bind ONCE. We keep only ``self._pool`` so nothing in the class
        # can navigate to another store through ``self``.
        object.__setattr__(self, "_pool", pool)

    def __setattr__(self, name: str, value: object) -> None:
        """Reject any post-construction attribute mutation."""
        raise AttributeError(
            "WriteRepository is immutable after construction; "
            f"cannot set attribute {name!r}"
        )

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    async def create(self, doc: PersistenceDoc) -> PersistenceDoc:
        """Insert ``doc`` and return the stored copy.

        ``created_at`` and ``updated_at`` are overwritten server-side with
        the current UTC instant — callers cannot back-date docs. Raises
        :class:`DuplicateIdError` when the primary key already exists.
        """
        now = _utcnow()
        stamped = replace(doc, created_at=now, updated_at=now)
        table = _TABLE_BY_TYPE[doc.type]
        doc_id, doc_type, category, locked, payload, created_at, updated_at = to_pg_row(
            stamped
        )
        if doc.type in _LOCKABLE_TYPES:
            sql = (
                f"INSERT INTO {self._pool.schema}.{table} "
                "(id, type, category, locked, payload, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)"
            )
            params: tuple[Any, ...] = (
                doc_id,
                doc_type,
                category,
                bool(locked),
                Jsonb(payload),
                created_at,
                updated_at,
            )
        else:  # basket — no locked column
            sql = (
                f"INSERT INTO {self._pool.schema}.{table} "
                "(id, type, category, payload, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            )
            params = (
                doc_id,
                doc_type,
                category,
                Jsonb(payload),
                created_at,
                updated_at,
            )
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateIdError(
                f"persistence: {doc.type} with id={doc.id!r} already exists"
            ) from exc
        return stamped

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    async def get_by_id(
        self,
        doc_type: Literal["indicator", "signal", "portfolio", "basket"],
        doc_id: str,
    ) -> PersistenceDoc | None:
        """Return the doc with id ``doc_id`` and type ``doc_type``.

        ``None`` when no matching document exists. Filtering by both keys
        guards against an id collision across types (each kind has its own
        table, so ``type`` here is belt-and-braces + keeps the contract).
        """
        table = _TABLE_BY_TYPE[doc_type]
        sql = (
            f"SELECT id, type, category, payload, created_at, updated_at "
            f"FROM {self._pool.schema}.{table} WHERE id = %s AND type = %s"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (doc_id, doc_type))
                row = await cur.fetchone()
        if row is None:
            return None
        return from_pg_row(row)

    async def list_by_type(
        self,
        doc_type: Literal["indicator"],
    ) -> list[IndicatorDoc]:
        """Return all *active* (non-deleted) docs of the given type.

        Only used for indicators (the only type with no user category).
        Active = ``category`` is NOT the ``'DELETED'`` sentinel (an active
        indicator has ``category IS NULL``).
        """
        if doc_type != DocType.INDICATOR.value:
            raise ValueError(
                f"list_by_type only supports 'indicator', got {doc_type!r}. "
                "Use list_by_type_and_category for signals/portfolios/baskets."
            )
        table = _TABLE_BY_TYPE[doc_type]
        # ``IS DISTINCT FROM`` so a NULL category (active) is kept and only
        # the literal 'DELETED' sentinel is excluded.
        sql = (
            f"SELECT id, type, category, payload, created_at, updated_at "
            f"FROM {self._pool.schema}.{table} "
            "WHERE type = %s AND category IS DISTINCT FROM %s"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (doc_type, DELETED_CATEGORY))
                rows = await cur.fetchall()
        return _deserialize_skipping_malformed(rows)  # type: ignore[return-value]

    async def list_by_type_and_category(
        self,
        doc_type: Literal["signal", "portfolio", "basket"],
        category: Category,
    ) -> list[PersistenceDoc]:
        """Return all docs of the given type filtered by category.

        ``ARCHIVE`` is a legal, VISIBLE category to query. Soft-deleted
        docs (``category = 'DELETED'``) are never returned — and since
        ``'DELETED'`` is not a ``Category`` member it can't even be asked
        for here.
        """
        table = _TABLE_BY_TYPE[doc_type]
        sql = (
            f"SELECT id, type, category, payload, created_at, updated_at "
            f"FROM {self._pool.schema}.{table} "
            "WHERE type = %s AND category = %s"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (doc_type, category.value))
                rows = await cur.fetchall()
        return _deserialize_skipping_malformed(rows)

    # ------------------------------------------------------------------ #
    # Update (full replace) with CAS + lock guard
    # ------------------------------------------------------------------ #
    async def update(
        self,
        doc: PersistenceDoc,
        *,
        expected_updated_at: datetime | None = None,
    ) -> PersistenceDoc:
        """Replace the doc identified by ``(id, type)`` with ``doc``.

        ``updated_at`` is bumped; ``created_at`` is preserved from ``doc``.

        Concurrency: when ``expected_updated_at`` is supplied the WHERE is
        extended with ``updated_at = expected`` (check-and-set). Lock
        guard: the stored ``locked`` flag is folded into the WHERE so a
        locked doc matches zero rows; the incoming ``doc.locked`` is NOT
        trusted. On a zero-row update we disambiguate:

        - stored doc is locked → :class:`LockedError` (423)
        - stored doc exists with a different ``updated_at`` → :class:`ConcurrentUpdateError` (409)
        - stored doc absent → ``KeyError`` (404)

        Baskets have no lock; the lock clause is simply omitted for them.
        """
        table = _TABLE_BY_TYPE[doc.type]
        bumped = replace(doc, updated_at=_utcnow())
        doc_id, doc_type, category, locked, payload, _created, updated_at = to_pg_row(
            bumped
        )

        where = "id = %s AND type = %s"
        params: list[Any] = [doc_id, doc_type]
        if doc.type in _LOCKABLE_TYPES:
            where += " AND locked = false"
        if expected_updated_at is not None:
            where += " AND updated_at = %s"
            params.append(expected_updated_at)

        if doc.type in _LOCKABLE_TYPES:
            set_clause = "category = %s, locked = %s, payload = %s, updated_at = %s"
            set_params: list[Any] = [
                category,
                bool(locked),
                Jsonb(payload),
                updated_at,
            ]
        else:
            set_clause = "category = %s, payload = %s, updated_at = %s"
            set_params = [category, Jsonb(payload), updated_at]

        sql = (
            f"UPDATE {self._pool.schema}.{table} SET {set_clause} "
            f"WHERE {where} RETURNING updated_at"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (*set_params, *params))
                row = await cur.fetchone()
                if row is not None:
                    # Return the EXACT stored updated_at (full precision)
                    # so the next CAS token round-trips.
                    return replace(bumped, updated_at=row["updated_at"])
                # Zero rows matched — disambiguate 423 / 409 / 404.
                await self._raise_if_locked(cur, doc.type, doc.id)
                if expected_updated_at is not None:
                    exists = await self._row_exists(cur, doc.type, doc.id)
                    if exists:
                        raise ConcurrentUpdateError(
                            f"persistence: {doc.type} id={doc.id!r} was modified "
                            f"concurrently — refusing to overwrite"
                        )
                raise KeyError(
                    f"persistence: no {doc.type} with id={doc.id!r} to update"
                )

    # ------------------------------------------------------------------ #
    # Archive (soft delete)
    # ------------------------------------------------------------------ #
    async def archive(
        self,
        doc_type: Literal["indicator", "signal", "portfolio", "basket"],
        doc_id: str,
    ) -> None:
        """Soft-delete the doc: set the ``category`` column to ``'DELETED'``.

        For indicators the payload's ``deleted`` flag is also set to
        ``true`` so the reconstructed dataclass / ``IndicatorOut.deleted``
        stays consistent. For all kinds the doc then disappears from every
        list query. Raises ``KeyError`` if the doc does not exist;
        idempotent otherwise.

        Lock guard: a locked doc cannot be archived (:class:`LockedError`).
        The stored ``locked`` flag is folded into the WHERE so a
        concurrent ``set_locked(True)`` landing in the TOCTOU window makes
        the write match zero rows; we then re-read to disambiguate
        just-locked (423) from not-found (404). Baskets have no lock, so
        the lock clause is omitted.
        """
        now = _utcnow()
        table = _TABLE_BY_TYPE[doc_type]
        where = "id = %s AND type = %s"
        params: list[Any] = [doc_id, doc_type]
        if doc_type in _LOCKABLE_TYPES:
            where += " AND locked = false"

        if doc_type == DocType.INDICATOR.value:
            # Also flip payload.deleted = true (jsonb_set) so a later
            # read reconstructs a consistent IndicatorDoc.
            set_clause = (
                "category = %s, "
                "payload = jsonb_set(payload, '{deleted}', 'true'::jsonb), "
                "updated_at = %s"
            )
        else:
            set_clause = "category = %s, updated_at = %s"

        sql = f"UPDATE {self._pool.schema}.{table} SET {set_clause} WHERE {where}"
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (DELETED_CATEGORY, now, *params))
                if cur.rowcount == 0:
                    # Locked under us in the race window (423), or gone (404).
                    await self._raise_if_locked(cur, doc_type, doc_id)
                    raise KeyError(
                        f"persistence: no {doc_type} with id={doc_id!r} to archive"
                    )

    # ------------------------------------------------------------------ #
    # Lock flag (the only lock-guard bypass)
    # ------------------------------------------------------------------ #
    async def set_locked(
        self,
        doc_type: Literal["indicator", "signal", "portfolio"],
        doc_id: str,
        locked: bool,
    ) -> PersistenceDoc:
        """Set ONLY the ``locked`` flag and bump ``updated_at``.

        The only mutation that bypasses the lock guard — how a locked doc
        gets unlocked. Both the ``locked`` projection column AND the
        ``payload.locked`` field are updated so the reconstructed doc (read
        from the payload) stays consistent with the filter column. Baskets
        are rejected at runtime (and have no lock column). Returns the
        updated doc re-read from storage. Raises ``KeyError`` if no
        matching document exists.
        """
        if doc_type not in _LOCKABLE_TYPES:
            raise ValueError(
                f"set_locked supports indicator/signal/portfolio only, got {doc_type!r}"
            )
        now = _utcnow()
        table = _TABLE_BY_TYPE[doc_type]
        # ``%s::jsonb`` literal so the payload's ``locked`` mirrors the column;
        # psycopg parametrizes the 'true'/'false' text safely.
        locked_json = "true" if locked else "false"
        sql = (
            f"UPDATE {self._pool.schema}.{table} "
            "SET locked = %s, "
            "payload = jsonb_set(payload, '{locked}', %s::jsonb), "
            "updated_at = %s "
            "WHERE id = %s AND type = %s "
            "RETURNING id, type, category, payload, created_at, updated_at"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    sql, (bool(locked), locked_json, now, doc_id, doc_type)
                )
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"persistence: no {doc_type} with id={doc_id!r} to set lock")
        return from_pg_row(row)

    # ------------------------------------------------------------------ #
    # Internal helpers (operate on an open cursor — same connection)
    # ------------------------------------------------------------------ #
    async def _raise_if_locked(
        self,
        cur: psycopg.AsyncCursor,
        doc_type: str,
        doc_id: str,
    ) -> None:
        """Raise :class:`LockedError` if the *stored* doc is locked.

        Reads only the persisted ``locked`` flag — never the incoming
        payload. A missing doc is NOT a lock violation: returns silently
        and lets the caller's own existence check surface the 404.
        Baskets have no lock column, so this is a no-op for them.
        """
        if doc_type not in _LOCKABLE_TYPES:
            return
        table = _TABLE_BY_TYPE[doc_type]
        await cur.execute(
            f"SELECT locked FROM {self._pool.schema}.{table} "
            "WHERE id = %s AND type = %s",
            (doc_id, doc_type),
        )
        row = await cur.fetchone()
        if row is not None and row["locked"]:
            raise LockedError(
                f"persistence: {doc_type} id={doc_id!r} is locked — "
                f"unlock it before mutating"
            )

    async def _row_exists(
        self,
        cur: psycopg.AsyncCursor,
        doc_type: str,
        doc_id: str,
    ) -> bool:
        """Return True if a row with ``(id, type)`` exists."""
        table = _TABLE_BY_TYPE[doc_type]
        await cur.execute(
            f"SELECT 1 FROM {self._pool.schema}.{table} WHERE id = %s AND type = %s",
            (doc_id, doc_type),
        )
        return (await cur.fetchone()) is not None

    # ------------------------------------------------------------------ #
    # Tickets — SELF-CONTAINED path (NOT the uniform 7-column machinery)
    # ------------------------------------------------------------------ #
    #
    # A ticket is a single free-text note. Its table has exactly three
    # columns (``id text PK``, ``text text NOT NULL``, ``created_at
    # timestamptz NOT NULL``) — no ``type``/``category``/``locked``/JSONB/
    # ``updated_at``. So these four methods deliberately bypass
    # ``_TABLE_BY_TYPE`` / ``to_pg_row`` / ``from_pg_row`` and write their
    # own 3-column SQL. The table name is a fixed literal here (it never
    # comes from a caller, preserving the "no public method takes a table
    # name" safety property); the schema is still ``self._pool.schema``.
    #
    # Delete is a HARD ``DELETE FROM`` — an INTENTIONAL exception to the
    # project's uniform ``category='DELETED'`` soft-delete. Do NOT convert
    # it to a soft-delete.
    _TICKETS_TABLE = "tickets"

    async def create_ticket(self, text: str) -> TicketDoc:
        """Insert a new ticket and return the stored row.

        ``id`` is a server-generated ``uuid4().hex``; ``created_at`` is
        the current UTC instant (both server-set — callers supply only
        the text). The caller (API layer) is responsible for validating /
        trimming ``text`` before calling this.
        """
        ticket_id = uuid.uuid4().hex
        created_at = _utcnow()
        sql = (
            f"INSERT INTO {self._pool.schema}.{self._TICKETS_TABLE} "
            "(id, text, created_at) VALUES (%s, %s, %s)"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (ticket_id, text, created_at))
        return TicketDoc(id=ticket_id, text=text, created_at=created_at)

    async def list_tickets(self) -> list[TicketDoc]:
        """Return every ticket, newest first (``created_at`` DESC)."""
        sql = (
            f"SELECT id, text, created_at "
            f"FROM {self._pool.schema}.{self._TICKETS_TABLE} "
            "ORDER BY created_at DESC"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
        return [
            TicketDoc(id=r["id"], text=r["text"], created_at=r["created_at"])
            for r in rows
        ]

    async def update_ticket(self, ticket_id: str, text: str) -> TicketDoc:
        """In-place UPDATE of a ticket's ``text``; return the stored row.

        ``created_at`` is preserved (there is no ``updated_at`` column —
        an edit is a plain text replacement). Raises ``KeyError`` when no
        ticket with ``ticket_id`` exists (the API maps this to 404); we
        detect the miss via ``RETURNING`` yielding no row.
        """
        sql = (
            f"UPDATE {self._pool.schema}.{self._TICKETS_TABLE} "
            "SET text = %s WHERE id = %s RETURNING id, text, created_at"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (text, ticket_id))
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"persistence: no ticket with id={ticket_id!r} to update")
        return TicketDoc(id=row["id"], text=row["text"], created_at=row["created_at"])

    async def delete_ticket(self, ticket_id: str) -> None:
        """HARD-delete a ticket (real ``DELETE FROM``).

        INTENTIONAL divergence from the uniform soft-delete: the row is
        physically removed. Raises ``KeyError`` (→ 404) when no row
        matched, detected via ``cur.rowcount == 0``.
        """
        sql = f"DELETE FROM {self._pool.schema}.{self._TICKETS_TABLE} WHERE id = %s"
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (ticket_id,))
                if cur.rowcount == 0:
                    raise KeyError(
                        f"persistence: no ticket with id={ticket_id!r} to delete"
                    )


__all__ = [
    "WriteRepository",
    "DuplicateIdError",
    "ConcurrentUpdateError",
    "DocumentTooLargeError",
    "LockedError",
]
