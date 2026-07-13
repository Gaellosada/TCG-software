"""Generic on-disk (SQLite) result cache — durable across restarts.

A single embedded SQLite file (stdlib ``sqlite3``, WAL mode, no new dependency)
memoises expensive JSON-serialisable compute results. It backs the portfolio
compute path: because the key is a content hash of the compute body, a
standalone compute and a composed-portfolio leg that reference the same
``(spec, range)`` produce the SAME key and share ONE entry (the Bug-2 unified
reuse fix). See ``tcg.core.api.portfolio``.

Design notes
------------
* **Durable & always-on.** On-disk, survives restarts. Single-user/desktop
  target (WSL-native), so an embedded file — NOT the shared dwh warehouse — is
  the right store; the ``tcg_app_data`` schema is reserved for entity
  persistence, not result blobs.
* **Stdlib-only** (``sqlite3``/``hashlib``/``json``) → no import-linter impact.
* **Off the event loop.** SQLite is synchronous; :meth:`get`/:meth:`put` run the
  sync work in a worker thread via ``asyncio.to_thread`` so the async server is
  never blocked. A fresh connection per operation keeps it thread-safe (no
  cross-thread connection sharing) — cheap for a local file.
* **Fresh arrays on every read.** Values round-trip through JSON, so a ``get``
  always returns a brand-new mutable object. There is NO shared/frozen in-memory
  entry — the aliasing concerns of the old in-process cache simply do not exist.
* **Eviction:** LRU by ``last_access`` with a bounded entry cap, plus an optional
  TTL to bound staleness (content-addressed, so a changed body is already a new
  key — TTL only guards against upstream data revisions).
* **Concurrency:** WAL allows concurrent readers with a writer; ``INSERT OR
  REPLACE`` makes a write idempotent. Two concurrent misses on the same key both
  compute and both write the SAME content — a benign double compute, never a
  wrong answer or a corrupt row. No in-flight dedup (deliberate; documented).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

# Default bound on the number of distinct results retained on disk.
DEFAULT_MAX_ENTRIES = 200


def canonical_hash(payload: Any) -> str:
    """Return a deterministic SHA-256 hex digest of ``payload``.

    ``payload`` must be JSON-able (e.g. ``PortfolioRequest.model_dump(
    mode="json")``). ``sort_keys`` makes the digest independent of dict insertion
    order at every nesting level, so two semantically identical bodies hash
    equal; ``separators`` strips incidental whitespace. ``default=str`` is
    defensive insurance — ``mode="json"`` should already have coerced every value
    to a JSON native.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DiskResultCache:
    """A bounded, durable SQLite result cache with LRU-by-last-access eviction.

    Values must be JSON-serialisable dicts. ``None`` is the miss sentinel, so a
    stored value must never be ``None`` (compute results are always dicts).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._path = str(path)
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── connection / schema ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0)
        # WAL: durable, allows a reader concurrent with the writer. busy_timeout:
        # wait (not fail) on a transient write lock. Both are per-connection but
        # WAL is a persistent property of the file once set.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS results ("
                "key TEXT PRIMARY KEY, "
                "value TEXT NOT NULL, "
                "created_at REAL NOT NULL, "
                "last_access REAL NOT NULL)"
            )

    # ── sync core (run in a worker thread) ──

    def _get_sync(self, key: str) -> Any | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value, created_at FROM results WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value_json, created_at = row
            if self._ttl is not None and (now - created_at) > self._ttl:
                # Expired: drop it and report a miss so the caller recomputes.
                conn.execute("DELETE FROM results WHERE key = ?", (key,))
                return None
            conn.execute("UPDATE results SET last_access = ? WHERE key = ?", (now, key))
            return json.loads(value_json)

    def _put_sync(self, key: str, value: Any) -> None:
        now = time.time()
        blob = json.dumps(value, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO results(key, value, created_at, last_access) "
                "VALUES (?, ?, ?, ?)",
                (key, blob, now, now),
            )
            # LRU eviction: if over the cap, drop the least-recently-accessed
            # entries. Content-addressed, so an evicted key simply recomputes.
            (count,) = conn.execute("SELECT COUNT(*) FROM results").fetchone()
            if count > self._max_entries:
                conn.execute(
                    "DELETE FROM results WHERE key IN ("
                    "SELECT key FROM results ORDER BY last_access ASC LIMIT ?)",
                    (count - self._max_entries,),
                )

    # ── async API ──

    async def get(self, key: str) -> Any | None:
        """Return the cached value for ``key`` (marking it MRU) or ``None``."""
        return await asyncio.to_thread(self._get_sync, key)

    async def put(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key`` (evicting the LRU entry if over cap)."""
        await asyncio.to_thread(self._put_sync, key, value)

    async def get_or_compute(
        self, key: str, compute: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return the cached value for ``key`` or ``await compute()`` and store it.

        See the module docstring for the (benign) concurrent-miss behaviour.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await compute()
        await self.put(key, value)
        return value

    # ── sync admin helpers (tests / a clear endpoint) ──

    def count(self) -> int:
        with self._connect() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM results").fetchone()
            return int(n)

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM results")


__all__ = ["DiskResultCache", "canonical_hash", "DEFAULT_MAX_ENTRIES"]
