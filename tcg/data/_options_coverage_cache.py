"""In-process, loop-scoped TTL cache for option trade-date coverage spans.

Why this exists (portfolio-cache-status-latency)
-------------------------------------------------
The Portfolio-page cache-status label resolves each option leg's date range via
``GET /api/options/coverage``.  Its cycle-scoped / v2 paths cost ~4-12s each (a
cold full-``fact_value`` scan on v2), so a cold session pays ~35s just to render
the label.  Data loads are rare (no daily sync), so the span a root reports is
stable for long stretches — an ideal memoisation target.

Corruption safety (preserve this property)
------------------------------------------
BOTH the label AND the compute path resolve the portfolio range through the SAME
coverage service methods (``option_trade_date_coverage`` /
``option_cycle_trade_date_span``).  Caching the coverage result therefore keeps
label and compute in lock-step: they read the identical ``(first, last)``, so the
compute-cache key always matches its stored result — no key/result mismatch is
possible.  The only effect is bounded staleness (<= TTL) that self-heals after a
(rare) data load.  This is why the cache MUST be shared by every caller and why
the compute-cache key (``_portfolio_cache_key``) is left untouched.

Design — mirrors ``_options_chain_cache.py``
--------------------------------------------
Loop-scoped module-global (keyed by ``id(running_loop)``), env-configurable TTL,
single-flight dedup, and a ``source`` discriminator so the v1 warehouse and the
v2 warehouse — whose option history starts years apart — never share an entry.
The stored value is the EXACT ``(date | None, date | None)`` tuple the uncached
service method returns; dates are immutable, so a hit hands back a byte-identical
result with no copy or serialisation round-trip.

Failure policy: an exception from the reader (e.g. ``OptionsDataAccessError``) is
propagated and NEVER cached, so a transient dwh outage is never pinned — a retry
re-computes.  A successful ``(None, None)`` (a root with no bars) IS cached: it is
a definite answer, not a failure, and it self-heals at the TTL once data lands.

Lifetime: in-memory only; a process restart flushes it (the moment a dwh backfill
is most likely).  The TTL backstop bounds staleness for a long-lived process and
``COVERAGE_CACHE_VERSION`` salts the key so a shape change can never serve a
stale-shaped entry across a deploy.

Environment variables (not documented elsewhere — this is the source of truth,
mirroring the undocumented ``TCG_CHAIN_CACHE_*`` precedent in
``_options_chain_cache.py``):
- ``TCG_COVERAGE_CACHE_ENABLED`` (default ``true``) — master kill switch; a
  false value makes ``get_coverage_cache()`` return ``None``, giving
  byte-identical-to-uncached behaviour.
- ``TCG_COVERAGE_CACHE_TTL_SECONDS`` (default ``21600`` — 6 h) — staleness
  backstop per entry.
- ``TCG_COVERAGE_CACHE_MAX_ENTRIES`` (default ``512``) — LRU cap on the number
  of cached spans.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from datetime import date
from typing import Awaitable, Callable, Hashable, Sequence

# Bump on ANY change to what the cached methods return or how the key is built,
# so a stale-shaped entry can never be served across a deploy.
COVERAGE_CACHE_VERSION = 1

_DEFAULT_MAX_ENTRIES = 512
# 6 h — matches the options chain cache (``tcg.core.api._options_chain_cache``)
# default.  The continuous
# precedent (``tcg.data._cache.LRUCache``) is process-lifetime with no TTL; a
# coverage span, unlike a continuous series, seeds the portfolio range floor, so
# a bounded TTL is added deliberately to let a rare data backfill self-heal
# without a restart.  Data loads are rare (no daily sync), so 6 h is safe.
_DEFAULT_TTL_SECONDS = 21_600

# Value stored per key: the exact tuple the service method returned.
_Span = tuple["date | None", "date | None"]
_Key = tuple[Hashable, ...]


def service_source_id(service: object) -> str:
    """Identity of the warehouse behind a coverage result.

    The cache is loop-global, NOT service-scoped, so the v1 service and the v2
    compat adapter — which accept the identical method signature and return
    DIFFERENT spans — must be discriminated.  Derived from the service's
    fully-qualified class name so a new source added later is separated
    automatically, with no wiring step anyone can forget.
    """
    cls = type(service)
    return f"{cls.__module__}.{cls.__qualname__}"


def _normalize_cycle(
    cycle: str | Sequence[str] | None,
) -> None | str | tuple[str, ...]:
    """Hashable, order-independent representation of the cycle filter."""
    if cycle is None or isinstance(cycle, str):
        return cycle
    return tuple(sorted(set(cycle)))


def make_coverage_key(*, source: str, root: str) -> _Key:
    """Key for the collection-wide ``option_trade_date_coverage(root)``."""
    return (COVERAGE_CACHE_VERSION, source, "coverage", root)


def make_span_key(
    *,
    source: str,
    root: str,
    start: date | None,
    end: date | None,
    cycle: str | Sequence[str] | None,
) -> _Key:
    """Key for ``option_cycle_trade_date_span(root, start, end, cycle)``.

    ``start`` / ``end`` are part of the key because the span is a function of the
    requested window (all current callers pass ``None``/``None`` — the unbounded
    collection extent — but a bounded caller must not collide with it).
    """
    return (
        COVERAGE_CACHE_VERSION,
        source,
        "span",
        root,
        start.isoformat() if start is not None else None,
        end.isoformat() if end is not None else None,
        _normalize_cycle(cycle),
    )


class CoverageCache:
    """TTL cache + single-flight over option coverage/span results.

    Values are the immutable ``(first, last)`` tuples the reader produced, so a
    hit returns a byte-identical result with no copy.  A simple LRU by insertion
    order bounds the entry count; entries are tiny (two dates), so an entry-count
    cap is sufficient (no row/byte accounting like the chain cache).
    """

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        # key -> (value, created_at).  OrderedDict = LRU: move_to_end on
        # read/insert, popitem(last=False) evicts the oldest.
        self._store: "OrderedDict[_Key, tuple[_Span, float]]" = OrderedDict()
        # key -> in-flight future resolving to the value (single-flight).
        self._inflight: dict[_Key, "asyncio.Future[_Span]"] = {}

    # -- internal helpers ---------------------------------------------------

    def _get_fresh(self, key: _Key) -> _Span | None:
        """Return the cached span (LRU-bumped) or ``None`` on miss/expiry.

        A stored value is ALWAYS a 2-tuple, so returning ``None`` unambiguously
        signals absence — a genuine ``(None, None)`` span is a truthy 2-tuple and
        is returned as a hit.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        value, created_at = entry
        if self._clock() - created_at > self._ttl:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value

    def _put(self, key: _Key, value: _Span) -> None:
        """Insert ``value`` and evict the oldest entry while over the cap."""
        self._store[key] = (value, self._clock())
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    # -- public API ---------------------------------------------------------

    async def get_or_fetch(
        self,
        key: _Key,
        fetch: Callable[[], Awaitable[_Span]],
    ) -> _Span:
        """Return the span for ``key``, fetching once.

        Single-flight: concurrent callers that miss the same key share ONE
        ``fetch()``.  On success the result is cached and every waiter resolves
        to the same tuple; on exception nothing is cached and the error
        propagates to every waiter (each caller then behaves exactly as the
        un-cached path does).  ``fetch`` is a zero-arg factory so the coroutine is
        created only when this call actually performs the fetch.
        """
        cached = self._get_fresh(key)
        if cached is not None:
            return cached

        existing = self._inflight.get(key)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[_Span]" = loop.create_future()
        self._inflight[key] = fut
        try:
            result = await fetch()
        except BaseException as exc:  # noqa: BLE001 — re-raised; never cached
            self._inflight.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)
            fut.exception()  # avoid "exception never retrieved" warnings
            raise

        self._put(key, result)
        self._inflight.pop(key, None)
        if not fut.done():
            fut.set_result(result)
        return result

    # -- introspection (tests / diagnostics) --------------------------------

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# Loop-scoped module-global instance (mirrors _options_chain_cache._CACHES)
# ---------------------------------------------------------------------------

_CACHES: dict[int, CoverageCache] = {}


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


def get_coverage_cache() -> CoverageCache | None:
    """Return the process/loop-scoped coverage cache, or ``None`` when disabled.

    Lazily creates one ``CoverageCache`` per running event loop (so pytest's
    per-test loops each get a fresh, isolated instance).  Returns ``None`` when
    ``TCG_COVERAGE_CACHE_ENABLED`` is false — the master kill switch, giving
    byte-identical-to-uncached behaviour.  Size / TTL are read from the
    environment at first construction for the loop.
    """
    if not _env_bool("TCG_COVERAGE_CACHE_ENABLED", True):
        return None
    loop = asyncio.get_running_loop()
    key = id(loop)
    cache = _CACHES.get(key)
    if cache is None:
        cache = CoverageCache(
            max_entries=_env_int(
                "TCG_COVERAGE_CACHE_MAX_ENTRIES", _DEFAULT_MAX_ENTRIES
            ),
            ttl_seconds=_env_int(
                "TCG_COVERAGE_CACHE_TTL_SECONDS", _DEFAULT_TTL_SECONDS
            ),
        )
        _CACHES[key] = cache
    return cache


__all__ = [
    "COVERAGE_CACHE_VERSION",
    "CoverageCache",
    "get_coverage_cache",
    "make_coverage_key",
    "make_span_key",
    "service_source_id",
]
