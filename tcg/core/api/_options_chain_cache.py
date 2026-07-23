"""In-process, loop-scoped intermediate cache for ``query_chain_bulk`` fetches.

Why this exists (Wave 3a — optimize-options-simulation-perf)
------------------------------------------------------------
The option-stream resolver fans out one ``query_chain_bulk`` per expiration
group over the resolve window.  Iterative strategy development (tweak a
short-10Δ put into a short-50Δ put, keep the SAME underlying + range) re-issues
byte-identical bulk fetches: the strike window derived by the resolver is
``option_type``-aware, NOT delta-aware, so 10Δ and 50Δ over the same range call
``query_chain_bulk`` with identical ``(root, dates, type, exp_min, exp_max,
strike_min, strike_max, cycle)``.  Re-querying the dwh for the same ~680k rows on
every tweak dominates wall time.

This module holds a process/loop-scoped, module-global, byte-aware LRU with
single-flight, keyed on the EXACT argument tuple the adapter receives.  It stores
the exact (frozen, immutable) Python objects the SQL reader produced and hands
them straight back — no serialization, so there is no float/``NaN``/``date``
round-trip and byte-identity is trivial.  The decorator that consumes this cache
(``CachedBulkChainReader`` in ``_options_wiring``) returns a SHALLOW COPY of each
per-date list on every hit so a caller can never mutate cached state.

Design authority: ``workspace/tasks/optimize-options-simulation-perf/output/
design_chain_cache.md``.

Scope / lifetime
----------------
In-memory only; a process restart flushes it (the moment a dwh backfill is most
likely, so this is the SAFE default for a byte-identical-to-current-dwh
guarantee).  A 6 h TTL backstop bounds staleness for a long-lived process, and
``CHAIN_CACHE_VERSION`` salts the key so a query/row-shape change can never serve
a stale-shaped entry across a deploy.

The cache instance is keyed by ``id(running_loop)`` (mirroring
``_options_concurrency._GATES``): production has one loop for the process
lifetime → one cache (the cross-request reuse we want); pytest's per-test loops
each get a fresh instance (no cross-loop ``asyncio.Future`` reuse, no leakage
between tests).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from datetime import date
from typing import Awaitable, Callable, Hashable, Sequence

from tcg.data._utils import date_to_int
from tcg.types.options import OptionContractDoc, OptionDailyRow

# Bump on ANY change to the ``query_chain_bulk`` SELECT column set, the row
# shape, or the ``OptionContractDoc`` / ``OptionDailyRow`` dataclass fields, so a
# stale-shaped entry can never be served across a deploy that changed the query.
CHAIN_CACHE_VERSION = 1

_DEFAULT_MAX_ROWS = 1_000_000
_DEFAULT_TTL_SECONDS = 21_600  # 6 h

# Type aliases for readability.
_Row = tuple[OptionContractDoc, OptionDailyRow]
_PerDateMapping = dict[int, list[_Row]]
_Key = tuple[Hashable, ...]


def _normalize_cycle(
    expiration_cycle: str | Sequence[str] | None,
) -> None | str | tuple[str, ...]:
    """Return a hashable, order-independent representation of the cycle filter.

    ``None`` and a plain ``str`` pass through unchanged; a sequence becomes a
    sorted tuple of its unique members so ``["M", "W"]`` and ``["W", "M"]`` key
    identically (the SQL result is a pure function of the cycle *set*).
    """
    if expiration_cycle is None or isinstance(expiration_cycle, str):
        return expiration_cycle
    return tuple(sorted(set(expiration_cycle)))


def _dateset_key(dates: Sequence[date]) -> tuple[int, ...]:
    """Canonical, order-free key component for the requested trade-date set.

    ``query_chain_bulk`` de-dupes its ``dates`` and the per-date result is
    ``ORDER BY (trade_date, instrument_id)`` — independent of the input order —
    so the fetched *content* is a pure function of the date SET.  A sorted,
    de-duped tuple of ``YYYYMMDD`` ints is therefore a correct, exact key
    component (two calls with the same set but different order share the entry;
    the decorator rebuilds the dict in the CURRENT call's order on a hit).
    """
    return tuple(sorted({date_to_int(d) for d in dates}))


def make_chain_bulk_key(
    *,
    root: str,
    dates: Sequence[date],
    type: str,
    expiration_min: date,
    expiration_max: date,
    strike_min: float | None,
    strike_max: float | None,
    expiration_cycle: str | Sequence[str] | None,
) -> _Key:
    """Build the LRU key from the EXACT args ``query_chain_bulk`` receives.

    The floats ``strike_min`` / ``strike_max`` are keyed VERBATIM (not rounded):
    a mismatched float is simply a miss (re-fetch), never a wrong hit, so this is
    correctness-safe even if an upstream change (e.g. Wave 2B strike-window
    derivation) perturbs the window — a new window is a new key.
    """
    return (
        CHAIN_CACHE_VERSION,
        root,
        type,
        date_to_int(expiration_min),
        date_to_int(expiration_max),
        strike_min,
        strike_max,
        _normalize_cycle(expiration_cycle),
        _dateset_key(dates),
    )


class ChainBulkCache:
    """Byte-aware LRU + single-flight over ``query_chain_bulk`` results.

    Values are stored as ``{trade_date_int: list_of_rows}`` holding the EXACT
    frozen dataclass objects the reader returned, in the SQL ``ORDER BY`` order.
    The consuming decorator rebuilds the date-keyed dict in the current call's
    order with a shallow copy of each list on every hit (mutation safety).
    """

    def __init__(
        self,
        *,
        max_rows: int = _DEFAULT_MAX_ROWS,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_rows = max_rows
        self._ttl = ttl_seconds
        self._clock = clock
        # key -> (per_date_mapping, created_at, n_rows).  OrderedDict = LRU;
        # move_to_end on read/insert, popitem(last=False) evicts the oldest.
        self._store: "OrderedDict[_Key, tuple[_PerDateMapping, float, int]]" = (
            OrderedDict()
        )
        self._total_rows = 0
        # key -> in-flight future resolving to the per-date mapping (single-flight).
        self._inflight: dict[_Key, "asyncio.Future[_PerDateMapping]"] = {}

    # -- internal helpers ---------------------------------------------------

    def _get_fresh(self, key: _Key) -> _PerDateMapping | None:
        """Return the cached mapping (LRU-bumped) or ``None`` on miss/expiry."""
        entry = self._store.get(key)
        if entry is None:
            return None
        mapping, created_at, n_rows = entry
        if self._clock() - created_at > self._ttl:
            # Expired: drop and treat as a miss.
            del self._store[key]
            self._total_rows -= n_rows
            return None
        self._store.move_to_end(key)
        return mapping

    def _put(self, key: _Key, mapping: _PerDateMapping, n_rows: int) -> None:
        """Insert ``mapping`` and evict LRU entries until under the row cap."""
        prev = self._store.get(key)
        if prev is not None:
            self._total_rows -= prev[2]
        self._store[key] = (mapping, self._clock(), n_rows)
        self._store.move_to_end(key)
        self._total_rows += n_rows
        # Evict oldest until under cap, but never evict the just-inserted entry
        # (a single entry larger than the cap is kept rather than never cached).
        while self._total_rows > self._max_rows and len(self._store) > 1:
            _evicted_key, (_m, _c, evicted_rows) = self._store.popitem(last=False)
            self._total_rows -= evicted_rows

    # -- public API ---------------------------------------------------------

    async def get_or_fetch(
        self,
        key: _Key,
        fetch: Callable[[], Awaitable[dict[date, list[_Row]]]],
    ) -> _PerDateMapping:
        """Return the int-keyed per-date mapping for ``key``, fetching once.

        Single-flight: concurrent callers that miss the same key share ONE
        ``fetch()``.  On success the result is cached and all waiters resolve to
        the same mapping; on exception nothing is cached and the error
        propagates to every waiter (each caller then degrades its own group
        exactly as the un-cached path does).  ``fetch`` is a zero-arg factory so
        the coroutine is created only when this call actually performs the
        fetch (never for a hit or a shared-flight wait).
        """
        cached = self._get_fresh(key)
        if cached is not None:
            return cached

        existing = self._inflight.get(key)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[_PerDateMapping]" = loop.create_future()
        self._inflight[key] = fut
        try:
            result = await fetch()
        except BaseException as exc:  # noqa: BLE001 — re-raised; never cached
            self._inflight.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)
            # Retrieve to avoid "future exception never retrieved" warnings for
            # a future no waiter ever awaited.
            fut.exception()
            raise

        mapping: _PerDateMapping = {date_to_int(d): rows for d, rows in result.items()}
        n_rows = sum(len(rows) for rows in mapping.values())
        self._put(key, mapping, n_rows)
        self._inflight.pop(key, None)
        if not fut.done():
            fut.set_result(mapping)
        return mapping

    # -- introspection (tests / diagnostics) --------------------------------

    @property
    def total_rows(self) -> int:
        return self._total_rows

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# Loop-scoped module-global instance (mirrors _options_concurrency._GATES)
# ---------------------------------------------------------------------------

_CACHES: dict[int, ChainBulkCache] = {}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_chain_bulk_cache() -> ChainBulkCache | None:
    """Return the process/loop-scoped chain cache, or ``None`` when disabled.

    Lazily creates (and caches) one ``ChainBulkCache`` per running event loop.
    Returns ``None`` when ``TCG_CHAIN_CACHE_ENABLED`` is false — the master
    kill switch, giving byte-identical-to-today behaviour.  Size / TTL are read
    from the environment at first construction for the loop.
    """
    if not _env_bool("TCG_CHAIN_CACHE_ENABLED", True):
        return None
    loop = asyncio.get_running_loop()
    key = id(loop)
    cache = _CACHES.get(key)
    if cache is None:
        cache = ChainBulkCache(
            max_rows=_env_int("TCG_CHAIN_CACHE_MAX_ROWS", _DEFAULT_MAX_ROWS),
            ttl_seconds=_env_int("TCG_CHAIN_CACHE_TTL_SECONDS", _DEFAULT_TTL_SECONDS),
        )
        _CACHES[key] = cache
    return cache


__all__ = [
    "CHAIN_CACHE_VERSION",
    "ChainBulkCache",
    "get_chain_bulk_cache",
    "make_chain_bulk_key",
]
