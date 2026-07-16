"""FAILING regression test — the dwh pool starves under realistic latency.

Context
-------
``04e13d7`` capped the resolver's *per-call* fan-out to ``DEFAULT_DWH_POOL_MAX_SIZE
- 1`` (= 3 at max_size 4) and shipped ``test_stream_pool_concurrency.py`` asserting
``peak concurrent query_chain_bulk <= pool size`` for ONE ``resolve_option_stream``
call.  That invariant holds — yet the live ``OPT_SP_500`` PoolTimeout PERSISTS.

Why the existing test misses the bug
------------------------------------
1. The per-call semaphore (``_DWH_RESOLVE_CONCURRENCY``) bounds ONE resolve.  It is
   a module-level ``asyncio.Semaphore`` *re-created inside* ``_resolve_bulk`` on every
   call (``stream_resolver.py`` ~:648), so **two concurrent resolves get two
   independent semaphores** — nothing bounds their *combined* demand on the single
   shared dwh pool.  Concurrent resolves happen in production:
     - the Data-page ``BasketChart`` fires the composite series AND (when the
       per-leg breakdown is on) one ``getBasketSeries`` per leg — concurrent
       ``POST /api/data/basket/series`` requests, each its own resolve;
     - any two browser tabs / chart panels resolving option streams at once.
   With a pool of ``max_size`` and a per-call cap of ``max_size - 1``, just TWO
   concurrent resolves demand up to ``2 * (max_size - 1)`` connections — which
   EXCEEDS ``max_size`` for any ``max_size >= 2`` (e.g. 6 > 4, or 14 > 8) → the
   acquire window elapses → ``PoolTimeout`` ("couldn't get a connection …").  The
   assertions below derive from ``DEFAULT_DWH_POOL_MAX_SIZE`` so they hold at any
   pool size.

2. The existing test's fake reader returns in ``asyncio.sleep(0.01)``.  The live
   ``query_chain_bulk`` on ``OPT_SP_500`` takes ~28-60 s (measured live: a single
   narrow-window single-date chain = 28 s; the bulk variant exceeds the 60 s
   ``statement_timeout``).  A connection is therefore HELD for tens of seconds, so
   even a cap-respecting fan-out keeps every slot busy long past the acquire
   timeout.  A 0.01 s fake cannot surface this.

This test pins the REAL invariant the fix must satisfy: **peak concurrent dwh
connections, summed across ALL in-flight resolves, must stay within the pool.**
It models the dwh pool as a real bounded gate with the SAME 30 s-style acquire
timeout ``psycopg_pool`` uses, injects realistic per-query latency, and runs two
ordinary resolves concurrently — exactly what the app does.  It FAILS on current
code (a ``PoolTimeout`` is raised, surfaced as the per-date ``data_access_error``
the resolver records) and will pass once acquisition is bounded process-wide
(e.g. a shared pool-sized gate around every ``query_chain_bulk`` / underlying
lookup, or a pool large enough for the real fan-out).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.market import DEFAULT_DWH_POOL_MAX_SIZE
from tcg.types.options import ByStrike, NearestToTarget, RollOffset

from _stream_fakes import _contract, _row


# ~12 distinct monthly expirations: a realistic (if shrunk) OPT_SP_500 fan-out.
_N_MONTHS = 12
_EXPIRATIONS = [date(2023, 1, 20) + timedelta(days=28 * i) for i in range(_N_MONTHS)]
_DATES = [e - timedelta(days=30) for e in _EXPIRATIONS]

# Acquire timeout mirrors psycopg_pool's default (30 s) but SHRUNK so the test is
# fast: a slot must free within this window or we raise PoolTimeout, exactly like
# the live pool.  The per-query latency is set ABOVE per-slot turnover so a 6-wide
# demand on a 4-slot pool cannot drain inside the window.
_ACQUIRE_TIMEOUT = 0.30
# Each query holds its slot this long — comfortably longer than _ACQUIRE_TIMEOUT so
# the 2 extra waiters (6 demand − 4 slots) time out instead of being served in time.
_QUERY_LATENCY = 0.50


class PoolTimeout(Exception):
    """Stand-in for ``psycopg_pool.PoolTimeout`` (same trigger: no slot in time)."""


class _BoundedSharedPool:
    """A process-wide bounded connection gate — models the ONE shared dwh pool.

    ``max_size`` slots; ``acquire`` blocks up to ``timeout`` then raises
    ``PoolTimeout`` (psycopg_pool semantics).  Every concurrent resolve shares the
    SAME instance — which is the production reality (one ``DwhConnectionPool``) and
    precisely what the per-call resolver semaphore fails to account for.
    """

    def __init__(self, max_size: int, acquire_timeout: float) -> None:
        self._sem = asyncio.Semaphore(max_size)
        self._timeout = acquire_timeout
        self.peak = 0
        self._inflight = 0

    async def acquire(self) -> None:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise PoolTimeout(
                f"couldn't get a connection after {self._timeout:.2f} sec"
            ) from exc
        self._inflight += 1
        self.peak = max(self.peak, self._inflight)

    def release(self) -> None:
        self._inflight -= 1
        self._sem.release()


class _PooledBulkReader:
    """Bulk reader that takes a slot from the SHARED pool for the query's whole
    duration — exactly what the real reader does (``async with pool.connection()``
    around a 28-60 s ``OPT_SP_500`` query)."""

    def __init__(self, pool: _BoundedSharedPool) -> None:
        self._pool = pool

    async def query_chain_bulk(
        self,
        *,
        root,
        dates,
        type,
        expiration_min,
        expiration_max,
        strike_min=None,
        strike_max=None,
        expiration_cycle=None,
    ):
        await self._pool.acquire()
        try:
            await asyncio.sleep(_QUERY_LATENCY)  # the slow dwh query holds the slot
            result = {}
            for d in dates:
                rows = [
                    (_contract(strike=4500, expiration=e), _row(row_date=d, mid=1.0))
                    for e in _EXPIRATIONS
                    if expiration_min <= e <= expiration_max
                ]
                if rows:
                    result[d] = rows
            return result
        finally:
            self._pool.release()


class _PooledProbeReader:
    """NearestToTarget probe reader — also takes a shared-pool slot (the live probe
    ``query_chain`` is one more dwh round-trip competing for the same pool)."""

    def __init__(self, pool: _BoundedSharedPool) -> None:
        self._pool = pool

    async def query_chain(
        self,
        *,
        root,
        date,
        type,
        expiration_min,
        expiration_max,
        strike_min=None,
        strike_max=None,
        expiration_cycle=None,
        limit=None,
    ):
        await self._pool.acquire()
        try:
            await asyncio.sleep(_QUERY_LATENCY)
            return [
                (_contract(strike=4500, expiration=e), _row(row_date=date, mid=1.0))
                for e in _EXPIRATIONS
                if expiration_min <= e <= expiration_max
            ]
        finally:
            self._pool.release()


async def _resolve_once(pool: _BoundedSharedPool, gate: asyncio.Semaphore | None):
    """One ordinary ByStrike resolve over the SHARED pool (no probe/underlying I/O
    in Phase C — ByStrike is pure CPU there, so this is the *minimal* path; the
    only pool demand is Phase B's bulk fan-out).  ``gate`` is the process-wide
    shared concurrency bound the core layer owns and injects into EVERY resolve;
    ``None`` reproduces the pre-fix per-call-cap behaviour."""
    return await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=_PooledProbeReader(pool),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=_PooledBulkReader(pool),
        available_expirations=_EXPIRATIONS,
        concurrency_gate=gate,
    )


async def test_concurrent_resolves_starve_the_shared_pool():
    """Two ordinary resolves sharing the ONE dwh pool must NOT exhaust it.

    Without a shared gate the per-call cap (``_DWH_RESOLVE_CONCURRENCY`` = 3) bounds
    each resolve, but the two hold independent semaphores, so combined peak demand
    reaches ``2 * 3 = 6`` against a ``max_size``-4 pool → with realistic per-query
    latency the 2 over-subscribed waiters exceed the acquire window → ``PoolTimeout``
    (in the bulk path it propagates out of Phase B's gather, aborting the resolve;
    the live reader wraps it as the reported "couldn't get a connection after 30.00
    sec").  This was the gap 04e13d7 left open.

    The fix: the core layer owns ONE process-wide ``asyncio.Semaphore`` sized to the
    pool and injects it (``concurrency_gate``) into EVERY resolve, so combined demand
    can never exceed the pool.  Here we model that by sharing one gate across both
    resolves and asserting they COMPLETE without a pool-acquisition failure.
    """
    pool = _BoundedSharedPool(
        max_size=DEFAULT_DWH_POOL_MAX_SIZE, acquire_timeout=_ACQUIRE_TIMEOUT
    )
    # Process-wide shared gate, sized to the pool (reserve one slot for the
    # interleaved probe/underlying/expiration lookups that share the pool — the
    # same reservation the per-call default makes).
    gate = asyncio.Semaphore(max(1, DEFAULT_DWH_POOL_MAX_SIZE - 1))

    try:
        (v1, e1, c1), (v2, e2, c2) = await asyncio.gather(
            _resolve_once(pool, gate), _resolve_once(pool, gate)
        )
    except PoolTimeout as exc:
        pytest.fail(
            f"concurrent resolves starved the shared dwh pool: {exc} "
            f"(peak demand {pool.peak} > max_size {DEFAULT_DWH_POOL_MAX_SIZE}) — "
            f"the process-wide concurrency_gate is not bounding pool acquisition"
        )

    # If it ever completes, the invariant is: combined demand stayed within the
    # pool and every date resolved (no per-date starvation either).
    assert pool.peak <= DEFAULT_DWH_POOL_MAX_SIZE, (
        f"peak shared-pool demand {pool.peak} exceeded pool max_size "
        f"{DEFAULT_DWH_POOL_MAX_SIZE} — concurrent resolves are not globally bounded"
    )
    starved = [e for e in (e1 + e2) if e == "data_access_error"]
    assert not starved, f"{len(starved)} dates failed (PoolTimeout) under contention"
    assert all(c is not None for c in c1 + c2)


async def test_without_gate_the_same_setup_still_starves():
    """Guard the guard: with NO shared gate the identical two-resolve setup STILL
    starves the pool (peak demand 2x3 > 4) — proving the passing test above is the
    gate's doing, not a vacuous pass.

    Phase-B fetch errors are now caught per-expiration (FIX C: a PoolTimeout
    degrades to a per-date NaN instead of aborting the resolve), so the run no
    longer RAISES — but starvation still shows up as every date failing with
    ``data_access_error`` and the pool being saturated.  The gate's value is
    therefore "real values vs starved NaN" (not "completes vs raises").
    """
    pool = _BoundedSharedPool(
        max_size=DEFAULT_DWH_POOL_MAX_SIZE, acquire_timeout=_ACQUIRE_TIMEOUT
    )
    (v1, e1, c1), (v2, e2, c2) = await asyncio.gather(
        _resolve_once(pool, None), _resolve_once(pool, None)
    )
    # The pool was saturated beyond the per-call reservation.
    assert pool.peak >= DEFAULT_DWH_POOL_MAX_SIZE, (
        "expected the un-gated concurrent resolves to saturate the pool"
    )
    # Starvation surfaced as data_access_error on at least some dates (vs the
    # gated test above, where every date resolves to a real value).
    starved = [e for e in (e1 + e2) if e == "data_access_error"]
    assert starved, (
        "expected un-gated concurrent resolves to starve (data_access_error dates) "
        "— if none, the shared pool was not actually over-subscribed"
    )
