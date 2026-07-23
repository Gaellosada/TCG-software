"""Unit tests for the intermediate option-chain data cache (Wave 3a).

Covers the six required behaviours:
1. Hit/miss + the motivating 10Δ→50Δ case (identical bulk args → one fetch).
2. Mutation safety (a caller mutating a returned list cannot poison later hits).
3. Single-flight (concurrent identical fetches share one underlying call).
4. Key correctness (each keyed dimension change → a miss; version salt too).
5. Bypass (cache=None / disabled master switch does not read or write).
6. TTL / LRU eviction (injected clock + small row bound).

The cache treats row objects opaquely, so lightweight sentinels stand in for
the frozen ``(OptionContractDoc, OptionDailyRow)`` tuples.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from tcg.core.api._options_chain_cache import (
    CHAIN_CACHE_VERSION,
    ChainBulkCache,
    get_chain_bulk_cache,
    make_chain_bulk_key,
)
from tcg.core.api._options_wiring import CachedBulkChainReader

D1 = date(2022, 1, 3)
D2 = date(2022, 1, 4)
D3 = date(2022, 1, 5)

BASE_ARGS = dict(
    root="OPT_SP_500",
    dates=[D1, D2, D3],
    type="P",
    expiration_min=date(2022, 2, 1),
    expiration_max=date(2022, 3, 31),
    strike_min=1000.0,
    strike_max=4000.0,
    expiration_cycle="M",
)


class FakeBulkAdapter:
    """Stand-in for ``_BulkOptionsDataPortAdapter``; counts fetches.

    Returns a deterministic dict keyed by the requested dates, each mapping to a
    fresh list of sentinel "rows" (unique per call so we can prove object reuse
    on hits and independence of copies).
    """

    def __init__(self, *, delay: float = 0.0, raises: BaseException | None = None):
        self.calls = 0
        self._delay = delay
        self._raises = raises

    async def query_chain_bulk(
        self,
        *,
        root,
        dates,
        type,
        expiration_min,
        expiration_max,
        strike_min,
        strike_max,
        expiration_cycle,
    ):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        call_id = self.calls
        return {
            d: [(root, type, d, call_id, i) for i in range(2)]
            for d in dict.fromkeys(dates)
        }


# --------------------------------------------------------------------------- #
# 1. Hit / miss                                                               #
# --------------------------------------------------------------------------- #


async def test_hit_miss_identical_args_single_fetch():
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, ChainBulkCache())

    r1 = await reader.query_chain_bulk(**BASE_ARGS)
    r2 = await reader.query_chain_bulk(**BASE_ARGS)

    assert inner.calls == 1  # second served from cache
    # Same content, same row objects (byte-identity), independent containers.
    assert list(r1.keys()) == list(r2.keys()) == [D1, D2, D3]
    for d in (D1, D2, D3):
        assert r1[d] == r2[d]
        assert r1[d][0] is r2[d][0]  # frozen rows shared
        assert r1[d] is not r2[d]  # list containers distinct (shallow copy)


async def test_delta_agnostic_same_window_is_hit():
    """The motivating 10Δ→50Δ case: strike window is type-aware not delta-aware,
    so both resolves call query_chain_bulk with byte-identical args → one fetch."""
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, ChainBulkCache())
    # 10Δ run and 50Δ run hit the adapter with the same bulk args.
    await reader.query_chain_bulk(**BASE_ARGS)  # "10 delta" resolve
    await reader.query_chain_bulk(**BASE_ARGS)  # "50 delta" resolve
    assert inner.calls == 1


async def test_dateset_order_and_dupes_still_hit():
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, ChainBulkCache())
    await reader.query_chain_bulk(**BASE_ARGS)
    # Same date SET, different order + a duplicate → still a hit.
    args = {**BASE_ARGS, "dates": [D3, D1, D2, D1]}
    r = await reader.query_chain_bulk(**args)
    assert inner.calls == 1
    # Returned dict follows THIS call's de-duped order.
    assert list(r.keys()) == [D3, D1, D2]


# --------------------------------------------------------------------------- #
# 2. Mutation safety                                                          #
# --------------------------------------------------------------------------- #


async def test_mutation_of_returned_list_does_not_poison_cache():
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, ChainBulkCache())
    r1 = await reader.query_chain_bulk(**BASE_ARGS)
    original_len = len(r1[D1])
    r1[D1].append(("MUTANT",))  # caller mutates its copy

    r2 = await reader.query_chain_bulk(**BASE_ARGS)
    assert inner.calls == 1  # still a hit
    assert len(r2[D1]) == original_len  # unaffected by the mutation
    assert ("MUTANT",) not in r2[D1]


# --------------------------------------------------------------------------- #
# 3. Single-flight                                                            #
# --------------------------------------------------------------------------- #


async def test_single_flight_shares_one_fetch():
    inner = FakeBulkAdapter(delay=0.02)
    reader = CachedBulkChainReader(inner, ChainBulkCache())
    r1, r2 = await asyncio.gather(
        reader.query_chain_bulk(**BASE_ARGS),
        reader.query_chain_bulk(**BASE_ARGS),
    )
    assert inner.calls == 1  # both concurrent misses shared one fetch
    for d in (D1, D2, D3):
        assert r1[d] == r2[d]
        assert r1[d] is not r2[d]  # each caller still owns its own containers


async def test_single_flight_exception_propagates_and_is_not_cached():
    boom = RuntimeError("dwh down")
    inner = FakeBulkAdapter(delay=0.02, raises=boom)
    reader = CachedBulkChainReader(inner, ChainBulkCache())
    results = await asyncio.gather(
        reader.query_chain_bulk(**BASE_ARGS),
        reader.query_chain_bulk(**BASE_ARGS),
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results)
    assert inner.calls == 1  # shared flight
    # Failure not cached → a retry re-fetches (now succeeding).
    inner._raises = None
    ok = await reader.query_chain_bulk(**BASE_ARGS)
    assert inner.calls == 2
    assert list(ok.keys()) == [D1, D2, D3]


# --------------------------------------------------------------------------- #
# 4. Key correctness                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "override",
    [
        {"root": "OPT_NDX"},
        {"type": "C"},
        {"expiration_min": date(2022, 2, 2)},
        {"expiration_max": date(2022, 4, 1)},
        {"strike_min": 1001.0},
        {"strike_max": 3999.0},
        {"expiration_cycle": "W"},
        {"expiration_cycle": None},
        {"dates": [D1, D2]},  # different date SET
    ],
)
async def test_each_dimension_change_is_a_miss(override):
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, ChainBulkCache())
    await reader.query_chain_bulk(**BASE_ARGS)
    await reader.query_chain_bulk(**{**BASE_ARGS, **override})
    assert inner.calls == 2


def test_make_key_stable_and_version_salted(monkeypatch):
    k1 = make_chain_bulk_key(**BASE_ARGS)
    k2 = make_chain_bulk_key(**BASE_ARGS)
    assert k1 == k2
    assert k1[0] == CHAIN_CACHE_VERSION
    # Bumping the version salt changes the key (stale-shaped entry can't be hit).
    import tcg.core.api._options_chain_cache as mod

    monkeypatch.setattr(mod, "CHAIN_CACHE_VERSION", CHAIN_CACHE_VERSION + 1)
    k3 = make_chain_bulk_key(**BASE_ARGS)
    assert k3 != k1


def test_cycle_normalization_order_free():
    a = make_chain_bulk_key(**{**BASE_ARGS, "expiration_cycle": ["M", "W"]})
    b = make_chain_bulk_key(**{**BASE_ARGS, "expiration_cycle": ["W", "M"]})
    assert a == b


# --------------------------------------------------------------------------- #
# 5. Bypass                                                                   #
# --------------------------------------------------------------------------- #


async def test_bypass_none_cache_never_caches():
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, None)  # bypass
    await reader.query_chain_bulk(**BASE_ARGS)
    await reader.query_chain_bulk(**BASE_ARGS)
    assert inner.calls == 2  # every call hits the inner adapter


async def test_master_switch_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("TCG_CHAIN_CACHE_ENABLED", "false")
    assert get_chain_bulk_cache() is None
    monkeypatch.setenv("TCG_CHAIN_CACHE_ENABLED", "true")
    assert isinstance(get_chain_bulk_cache(), ChainBulkCache)


# --------------------------------------------------------------------------- #
# 6. TTL / LRU eviction                                                       #
# --------------------------------------------------------------------------- #


async def test_ttl_expiry_forces_refetch():
    clock = {"t": 1000.0}
    cache = ChainBulkCache(ttl_seconds=100.0, clock=lambda: clock["t"])
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, cache)

    await reader.query_chain_bulk(**BASE_ARGS)
    clock["t"] += 50.0
    await reader.query_chain_bulk(**BASE_ARGS)
    assert inner.calls == 1  # within TTL → hit

    clock["t"] += 100.0  # now 150s past creation > 100s TTL
    await reader.query_chain_bulk(**BASE_ARGS)
    assert inner.calls == 2  # expired → re-fetched


async def test_lru_row_bound_evicts_oldest():
    # Each fetch yields 2 rows/date × 3 dates = 6 rows. Cap at 8 rows → holding
    # two distinct entries (12 rows) is impossible, so the oldest is evicted.
    cache = ChainBulkCache(max_rows=8)
    inner = FakeBulkAdapter()
    reader = CachedBulkChainReader(inner, cache)

    await reader.query_chain_bulk(**BASE_ARGS)  # entry A (6 rows)
    await reader.query_chain_bulk(
        **{**BASE_ARGS, "root": "OPT_NDX"}
    )  # entry B → evicts A
    assert cache.total_rows == 6
    assert len(cache) == 1

    # A was evicted → re-fetch (miss); B still cached → hit.
    await reader.query_chain_bulk(**BASE_ARGS)  # miss (A gone)
    assert inner.calls == 3
    await reader.query_chain_bulk(
        **{**BASE_ARGS, "root": "OPT_NDX"}
    )  # B evicted by A now
    # A (re-added) evicted B, so this is also a miss.
    assert inner.calls == 4
