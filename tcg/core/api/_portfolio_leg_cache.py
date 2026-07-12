"""Bounded in-process LRU cache for composed-portfolio sub-computations (Approach 3).

A ``type="portfolio"`` leg computes its inlined child to an equity curve by
recursively invoking the portfolio compute path — an expensive operation. When
the SAME child (same spec, same resolved date range) appears again (a repeated
request, or several composed portfolios referencing the same building block),
recomputing is wasteful. This module memoises that result.

Design (design_spec §6):

* **Content-addressed key.** The key is a deterministic SHA-256 of the canonical
  JSON of the child compute body. The body already carries the parent's
  ``start``/``end`` (threaded in by ``_evaluate_portfolio_leg``), so
  ``(child_spec, resolved range)`` is fully captured — and because the ENTIRE
  body is hashed (recursively), any change to a nested child leg (including a
  signal leg buried inside the child) yields a different key. Editing a child →
  new body → new key → automatic recompute. This is the same body→result
  determinism the frontend result cache relies on; the two are independent (no
  cross-language parity needed — this key is internal, Python-side only).

* **Bounded LRU.** ``OrderedDict`` with move-to-end on access and evict-oldest on
  overflow; default capacity 50. Per-process, lost on restart — it is only a
  cache, never a source of truth (the backend stays authoritative; a miss just
  recomputes from the spec).

* **Byte-identical.** A hit returns exactly what the uncached path produced; the
  cache only skips recomputation. Cached arrays are frozen read-only so an
  accidental downstream mutation fails loudly instead of silently corrupting a
  shared entry.

* **Concurrency.** No in-flight dedup (deliberately kept simple, per §6). asyncio
  is single-threaded, so the ``OrderedDict`` mutations are race-free; the only
  effect of two concurrent misses on the same key is a benign DOUBLE COMPUTE
  whose results are byte-identical (idempotent ``put``). Worst case is redundant
  work, never a wrong answer.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Generic, TypeVar

_V = TypeVar("_V")

# Default number of distinct child computations to retain. Small — a single-user
# desktop app rarely juggles many distinct building blocks at once, and each
# entry pins an equity curve in memory.
DEFAULT_CAPACITY = 50


def canonical_key(payload: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 hex digest of ``payload``.

    ``payload`` must be a JSON-able dict (e.g. ``PortfolioRequest.model_dump(
    mode="json")``). ``sort_keys`` makes the encoding independent of dict
    insertion order at every nesting level, so two semantically identical bodies
    always hash equal; ``separators`` strips incidental whitespace. ``default=str``
    is defensive insurance — ``mode="json"`` should already have coerced every
    value to a JSON native.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class BoundedLRUCache(Generic[_V]):
    """A small fixed-capacity LRU cache with hit/miss stats.

    ``None`` is used internally as the "absent" sentinel by :meth:`get`, so
    values must never be ``None`` (composed-leg values are tuples of arrays —
    never ``None``).
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._store: "OrderedDict[str, _V]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._store)

    def peek(self, key: str) -> bool:
        """Membership test that does NOT reorder or touch stats (for tests)."""
        return key in self._store

    def get(self, key: str) -> _V | None:
        """Return the cached value (marking it most-recently-used) or ``None``.

        Updates ``hits``/``misses``. This is the stat authority — do not
        double-count by calling it and :meth:`get_or_compute` for one lookup.
        """
        if key in self._store:
            self._store.move_to_end(key)
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def put(self, key: str, value: _V) -> None:
        """Insert/refresh ``key`` as most-recently-used, evicting the LRU entry
        if capacity is exceeded."""
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        """Drop all entries and reset stats (used between tests)."""
        self._store.clear()
        self.hits = 0
        self.misses = 0

    async def get_or_compute(
        self, key: str, compute: Callable[[], Awaitable[_V]]
    ) -> _V:
        """Return the cached value for ``key`` or ``await compute()`` and store it.

        A miss increments ``misses`` (via :meth:`get`), so ``misses`` equals the
        number of real computations. See the module docstring for the (benign)
        concurrent-miss behaviour.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        value = await compute()
        self.put(key, value)
        return value


__all__ = ["BoundedLRUCache", "canonical_key", "DEFAULT_CAPACITY"]
