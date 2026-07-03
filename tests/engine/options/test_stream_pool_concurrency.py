"""Regression test for the dwh pool-starvation fix.

The option-stream resolver fans out one chain query per unique expiration; each
acquires a dwh pool connection (``async with pool.connection()``).  Before the
fix the fan-out cap was a hardcoded 8 (bulk) / 16 (per-date) — sized for the old
Mongo 100-slot pool — which over-subscribed the 4-slot dwh pool and produced
``PoolTimeout`` on large roots (OPT_SP_500: ~95 expirations in a 2-year window).

The broken invariant is ``peak concurrent dwh connections <= pool max_size``.
These tests pin it WITHOUT a live dwh: a fake bulk reader whose ``query_chain_bulk``
tracks the live in-flight count (with a tiny ``await`` to force overlap) over a
many-expiration resolve, asserting the peak never exceeds the pool size.  They
fail against the old 8/16 caps and pass with the pool-derived cap.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series import stream_resolver as sr
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.market import DEFAULT_DWH_POOL_MAX_SIZE
from tcg.types.options import ByStrike, NearestToTarget, RollOffset

from _stream_fakes import _contract, _row


# Many distinct monthly expirations — the OPT_SP_500-shaped fan-out.  Each trade
# date wants a different expiration (NearestToTarget ~30 DTE), so Phase B builds
# one bulk-fetch task per expiration → many concurrent acquirers.
_N_MONTHS = 24
_EXPIRATIONS = [date(2022, 1, 21) + timedelta(days=28 * i) for i in range(_N_MONTHS)]
# One trade date per expiration, ~30 days before it (so NearestToTarget(30) picks
# that month's expiration).
_DATES = [e - timedelta(days=30) for e in _EXPIRATIONS]


class _ConcurrencyTrackingBulkReader:
    """Bulk chain reader that records the PEAK number of overlapping
    ``query_chain_bulk`` calls — a proxy for peak concurrent pool connections
    (the real reader does ``async with pool.connection()`` for the call's
    duration).  A tiny ``await asyncio.sleep`` forces real overlap so the
    semaphore's effect is observable."""

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0

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
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.01)  # hold the "connection" so overlap is real
            result = {}
            for d in dates:
                rows = [
                    (c, r)
                    for (c, r) in (
                        (
                            _contract(strike=4500, expiration=e),
                            _row(row_date=d, mid=1.0),
                        )
                        for e in _EXPIRATIONS
                    )
                    if expiration_min <= c.expiration <= expiration_max
                ]
                if rows:
                    result[d] = rows
            return result
        finally:
            self.inflight -= 1


class _ProbeReader:
    """Per-date reader serving the NearestToTarget probe query (one wide-window
    call); not concurrency-relevant here."""

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
    ):
        return [
            (_contract(strike=4500, expiration=e), _row(row_date=date, mid=1.0))
            for e in _EXPIRATIONS
            if expiration_min <= e <= expiration_max
        ]


def test_resolve_concurrency_cap_within_pool_size():
    """The derived cap never lets the resolver exceed the pool size."""
    assert sr._DWH_RESOLVE_CONCURRENCY <= DEFAULT_DWH_POOL_MAX_SIZE
    assert sr._MAX_INFLIGHT_PER_DATE <= DEFAULT_DWH_POOL_MAX_SIZE
    # Reserve at least one slot for the interleaved expirations / spot lookups.
    assert sr._DWH_RESOLVE_CONCURRENCY <= DEFAULT_DWH_POOL_MAX_SIZE - 1
    assert sr._DWH_RESOLVE_CONCURRENCY >= 1


async def test_bulk_resolve_peak_connections_within_pool_size():
    """A many-expiration bulk resolve must never hold more concurrent
    dwh connections than the pool provides (the violated invariant).

    With ~24 expirations the OLD cap (8) would drive peak 8 > 4; the
    pool-derived cap keeps peak <= max_size (in fact <= max_size - 1)."""
    reader = _ConcurrencyTrackingBulkReader()
    values, errors, contracts = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=_ProbeReader(),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=reader,
        available_expirations=_EXPIRATIONS,
    )
    # Sanity: the fan-out really was large (many expirations resolved), so the
    # cap is actually being exercised (not vacuously true on a 1-task resolve).
    assert reader.peak >= 2, "test did not exercise concurrent fan-out"
    # THE INVARIANT: peak concurrent connections <= pool size.
    assert reader.peak <= DEFAULT_DWH_POOL_MAX_SIZE, (
        f"peak {reader.peak} exceeded pool max_size {DEFAULT_DWH_POOL_MAX_SIZE}"
    )
    # And specifically <= the derived cap (reserving a slot).
    assert reader.peak <= sr._DWH_RESOLVE_CONCURRENCY
    # The resolve still succeeded for every date (no PoolTimeout, real values).
    assert all(c is not None for c in contracts)
    assert all(e is None for e in errors)


async def test_old_unbounded_cap_would_violate_the_invariant():
    """Guard the guard: with an OVER-SIZED cap (pool_size + 4) the SAME fan-out
    drives peak > pool size — proving the test detects the regression, not a
    vacuous pass.  Sized relative to the pool so it holds at any
    ``DEFAULT_DWH_POOL_MAX_SIZE`` (the ~24-expiration fan-out here reaches it).
    (Monkeypatch only the bulk semaphore size for this one check.)"""
    reader = _ConcurrencyTrackingBulkReader()
    # Temporarily patch the module's derived cap to an over-subscribing value.
    orig = sr._DWH_RESOLVE_CONCURRENCY
    sr._DWH_RESOLVE_CONCURRENCY = DEFAULT_DWH_POOL_MAX_SIZE + 4
    try:
        await resolve_option_stream(
            dates=_DATES,
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NearestToTarget(target_dte_days=30),
            selection=ByStrike(strike=4500.0),
            stream="mid",
            roll_offset=RollOffset(),
            chain_reader=_ProbeReader(),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=reader,
            available_expirations=_EXPIRATIONS,
        )
    finally:
        sr._DWH_RESOLVE_CONCURRENCY = orig
    # An over-sized cap over-subscribes the pool — exactly the starvation bug.
    assert reader.peak > DEFAULT_DWH_POOL_MAX_SIZE
