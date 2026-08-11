"""Unit tests for the option trade-date coverage cache
(portfolio-cache-status-latency).

Covers:
1. Hit / miss — identical args → a single fetch, byte-identical value.
2. ``(None, None)`` is cached (a definite empty span, not a failure).
3. Single-flight — concurrent identical misses share ONE fetch.
4. Failures are NOT pinned — the exception propagates and a retry re-fetches.
5. Key correctness — each of (source, root, cycle, start, end) changes → a miss;
   version salt too.
6. TTL expiry forces a recompute (injected clock).
7. LRU eviction by entry cap.
8. Master kill switch — ``TCG_COVERAGE_CACHE_ENABLED=false`` → ``None`` (bypass).
9. Service wiring — the v1 service caches through this cache and a v2 service
   (different class) never shares its entry; a hit equals the computed miss.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from tcg.data._options_coverage_cache import (
    COVERAGE_CACHE_VERSION,
    CoverageCache,
    get_coverage_cache,
    make_coverage_key,
    make_span_key,
    service_source_id,
)

D_FIRST = date(2005, 1, 3)
D_LAST = date(2022, 12, 30)


@pytest.fixture(autouse=True)
def _fresh_loop_global_cache():
    """Reset the loop-scoped module-global cache between tests.

    pytest-asyncio reuses one event loop, so the process/loop-scoped cache (the
    correct cross-request reuse in production) would otherwise leak entries
    between tests.  Clearing it keeps the service-wiring tests hermetic.
    """
    import tcg.data._options_coverage_cache as mod

    mod._CACHES.clear()
    yield
    mod._CACHES.clear()


class FakeReader:
    """Stand-in options reader; counts calls and returns a fixed span."""

    def __init__(self, *, span=(D_FIRST, D_LAST), delay=0.0, raises=None):
        self.coverage_calls = 0
        self.span_calls = 0
        self._span = span
        self._delay = delay
        self._raises = raises

    async def trade_date_coverage(self, root):
        self.coverage_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._span

    async def cycle_trade_date_span(self, root, start, end, *, cycle=None):
        self.span_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._span


# --------------------------------------------------------------------------- #
# 1. Hit / miss                                                               #
# --------------------------------------------------------------------------- #


async def test_hit_returns_same_value_as_computed_miss():
    cache = CoverageCache()
    reader = FakeReader()
    key = make_coverage_key(source="v1", root="OPT_SP_500")

    miss = await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))
    hit = await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))

    assert reader.coverage_calls == 1  # second served from cache
    assert miss == (D_FIRST, D_LAST)
    assert hit == miss
    assert hit is miss  # immutable tuple shared byte-for-byte, no copy


async def test_none_none_span_is_cached_not_treated_as_miss():
    cache = CoverageCache()
    reader = FakeReader(span=(None, None))
    key = make_coverage_key(source="v1", root="OPT_EMPTY")

    r1 = await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))
    r2 = await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))

    assert r1 == (None, None)
    assert reader.coverage_calls == 1  # a (None, None) span is a hit, not a miss


# --------------------------------------------------------------------------- #
# 3. Single-flight                                                            #
# --------------------------------------------------------------------------- #


async def test_single_flight_shares_one_fetch():
    cache = CoverageCache()
    reader = FakeReader(delay=0.02)
    key = make_span_key(
        source="v2", root="OPT_SP_500", start=None, end=None, cycle="W3"
    )
    r1, r2 = await asyncio.gather(
        cache.get_or_fetch(
            key, lambda: reader.cycle_trade_date_span("x", None, None, cycle="W3")
        ),
        cache.get_or_fetch(
            key, lambda: reader.cycle_trade_date_span("x", None, None, cycle="W3")
        ),
    )
    assert reader.span_calls == 1  # both concurrent misses shared one fetch
    assert r1 == r2 == (D_FIRST, D_LAST)


# --------------------------------------------------------------------------- #
# 4. Failures are not pinned                                                  #
# --------------------------------------------------------------------------- #


async def test_failure_propagates_and_is_not_cached():
    boom = RuntimeError("dwh down")
    cache = CoverageCache()
    reader = FakeReader(delay=0.01, raises=boom)
    key = make_coverage_key(source="v1", root="OPT_SP_500")

    results = await asyncio.gather(
        cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x")),
        cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x")),
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results)
    assert reader.coverage_calls == 1  # shared flight
    assert len(cache) == 0  # nothing cached

    # A retry re-fetches (now succeeding) — the transient failure was not pinned.
    reader._raises = None
    ok = await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))
    assert reader.coverage_calls == 2
    assert ok == (D_FIRST, D_LAST)


# --------------------------------------------------------------------------- #
# 5. Key correctness                                                          #
# --------------------------------------------------------------------------- #


def test_coverage_and_span_keys_are_distinct():
    cov = make_coverage_key(source="v1", root="OPT_SP_500")
    span = make_span_key(
        source="v1", root="OPT_SP_500", start=None, end=None, cycle=None
    )
    assert cov != span


@pytest.mark.parametrize(
    "a,b",
    [
        # source (v1 vs v2) differs
        (
            dict(source="v1", root="OPT_SP_500"),
            dict(source="v2", root="OPT_SP_500"),
        ),
        # root differs
        (
            dict(source="v1", root="OPT_SP_500"),
            dict(source="v1", root="OPT_VIX"),
        ),
    ],
)
def test_coverage_key_distinguishes_dimensions(a, b):
    assert make_coverage_key(**a) != make_coverage_key(**b)


@pytest.mark.parametrize(
    "override",
    [
        {"source": "v2"},
        {"root": "OPT_VIX"},
        {"cycle": "M"},
        {"start": date(2010, 1, 1)},
        {"end": date(2020, 1, 1)},
    ],
)
def test_span_key_distinguishes_dimensions(override):
    base = dict(source="v1", root="OPT_SP_500", start=None, end=None, cycle="W3")
    assert make_span_key(**base) != make_span_key(**{**base, **override})


def test_span_cycle_normalisation_order_free():
    a = make_span_key(
        source="v1", root="OPT_SP_500", start=None, end=None, cycle=["M", "W"]
    )
    b = make_span_key(
        source="v1", root="OPT_SP_500", start=None, end=None, cycle=["W", "M"]
    )
    assert a == b


def test_make_key_version_salted(monkeypatch):
    k1 = make_coverage_key(source="v1", root="OPT_SP_500")
    import tcg.data._options_coverage_cache as mod

    monkeypatch.setattr(mod, "COVERAGE_CACHE_VERSION", COVERAGE_CACHE_VERSION + 1)
    k2 = make_coverage_key(source="v1", root="OPT_SP_500")
    assert k1 != k2


# --------------------------------------------------------------------------- #
# 6. TTL expiry                                                               #
# --------------------------------------------------------------------------- #


async def test_ttl_expiry_forces_recompute():
    clock = {"t": 1000.0}
    cache = CoverageCache(ttl_seconds=100.0, clock=lambda: clock["t"])
    reader = FakeReader()
    key = make_coverage_key(source="v1", root="OPT_SP_500")

    await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))
    clock["t"] += 50.0
    await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))
    assert reader.coverage_calls == 1  # within TTL → hit

    clock["t"] += 100.0  # 150s past creation > 100s TTL
    await cache.get_or_fetch(key, lambda: reader.trade_date_coverage("x"))
    assert reader.coverage_calls == 2  # expired → re-fetched


# --------------------------------------------------------------------------- #
# 7. LRU eviction                                                             #
# --------------------------------------------------------------------------- #


async def test_lru_evicts_oldest_over_cap():
    cache = CoverageCache(max_entries=2)
    reader = FakeReader()

    async def fetch(root):
        key = make_coverage_key(source="v1", root=root)
        return await cache.get_or_fetch(key, lambda: reader.trade_date_coverage(root))

    await fetch("A")
    await fetch("B")
    await fetch("C")  # evicts A (oldest)
    assert len(cache) == 2

    await fetch("B")  # still cached → hit
    assert reader.coverage_calls == 3
    await fetch("A")  # evicted → miss → re-fetch
    assert reader.coverage_calls == 4


# --------------------------------------------------------------------------- #
# 8. Master switch                                                            #
# --------------------------------------------------------------------------- #


async def test_master_switch_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("TCG_COVERAGE_CACHE_ENABLED", "false")
    assert get_coverage_cache() is None
    monkeypatch.setenv("TCG_COVERAGE_CACHE_ENABLED", "true")
    assert isinstance(get_coverage_cache(), CoverageCache)


# --------------------------------------------------------------------------- #
# 9. Service wiring — both endpoint and internal callers benefit              #
# --------------------------------------------------------------------------- #


async def test_v1_service_method_caches_and_hit_equals_miss():
    from tcg.data.service import DefaultMarketDataService

    svc = DefaultMarketDataService.__new__(DefaultMarketDataService)
    reader = FakeReader()
    svc._options = reader  # type: ignore[attr-defined]

    miss = await svc.option_trade_date_coverage("OPT_SP_500")
    hit = await svc.option_trade_date_coverage("OPT_SP_500")

    assert reader.coverage_calls == 1  # 2nd call served from the shared cache
    assert miss == hit == (D_FIRST, D_LAST)


async def test_v1_and_v2_services_do_not_share_entries():
    """Same (root, cycle) via two different service classes → two fetches:
    the ``source`` discriminator keeps v1 and v2 (different histories) apart."""
    from tcg.data.service import DefaultMarketDataService
    from tcg.data._v2_compat.adapter import V2MarketDataAdapter

    v1 = DefaultMarketDataService.__new__(DefaultMarketDataService)
    v1_reader = FakeReader(span=(D_FIRST, D_LAST))
    v1._options = v1_reader  # type: ignore[attr-defined]

    v2 = V2MarketDataAdapter.__new__(V2MarketDataAdapter)
    v2_reader = FakeReader(span=(date(2011, 1, 3), D_LAST))
    v2._options = v2_reader  # type: ignore[attr-defined]  # backs .options_reader

    # Sanity: the two service classes have distinct source ids.
    assert service_source_id(v1) != service_source_id(v2)

    r_v1 = await v1.option_trade_date_coverage("OPT_SP_500")
    r_v2 = await v2.option_trade_date_coverage("OPT_SP_500")

    assert v1_reader.coverage_calls == 1
    assert v2_reader.coverage_calls == 1  # NOT served from v1's entry
    assert r_v1 == (D_FIRST, D_LAST)
    assert r_v2 == (date(2011, 1, 3), D_LAST)  # v2's own (different) span
