"""LRU cache for price series and other frequently-accessed data.

Simple OrderedDict-based implementation. Thread-safe is not required
since Motor is single-threaded per event loop.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class LRUCache:
    """Least-recently-used cache with a fixed maximum size.

    On ``get``, found entries are moved to the end (most recent).
    On ``put``, the oldest entry is evicted when capacity is exceeded.
    """

    def __init__(self, max_size: int = 200) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> Any | None:
        """Return cached value or ``None`` if not present."""
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value: Any) -> None:
        """Insert or update *key*. Evicts LRU entry if at capacity."""
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        """Remove all entries."""
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return key in self._cache
