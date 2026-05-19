"""Provider selection per OPT_* root.

Each root has an explicit list of accepted providers (the priorities
below). ``select_provider`` walks them in order and returns the first
that has bars on the supplied doc. There is intentionally NO "any
provider with data" fallback — if a doc carries a provider we have
not curated, the row is dropped loudly so the unexpected-vendor case
is visible (and easy to fix by extending the list) rather than
silently masked.

Greeks gating: OPT_ETH always returns ``has_greeks=False`` (no
curated greeks vendor wired in yet); quote data may still surface
from ``eodDatas`` (COINAPI / DERIBIT). OPT_VIX is no longer
blanket-blocked at the data layer — any greeks present under
``eodDatas.CBOE[*]`` pass through with ``source="stored"``. (The
engine-side compute path remains blocked until the FUT_VIX
forward-curve wiring lands; see ``tcg.engine.options.pricing._gating``.)

Stored-greeks coverage ratios
-----------------------------

``get_stored_greeks_ratios`` returns the fraction of docs per root
that carry stored greeks (``eodGreeks`` top-level field). It is served
from a process-local cache with a 24-hour TTL:

  - First call (or expired entry): runs ``$sample`` of 500 docs per
    root, in parallel via ``asyncio.gather``; computes per-root
    ratios; populates the cache atomically under an ``asyncio.Lock``
    (no thundering herd).
  - Subsequent calls within the TTL: return the cached snapshot.
  - On measurement failure with a prior cache: serve the stale cache
    (better than nothing — ratios drift slowly).
  - On measurement failure on cold start: serve ``_SEED_RATIOS``
    (hardcoded approximations from 2026-05-18).

The cache is reset by restarting uvicorn. We deliberately do not
expose a manual refresh endpoint — coverage is a slow-moving vendor
metric, and the ratios only drive the left-nav badge variant.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

# Per-root accepted providers, ordered by preference. The crypto roots
# are heterogeneous in production (some docs carry COINAPI, others
# DERIBIT, oldest are INTERNAL); enumerate all observed values.
_PRIORITY_BY_ROOT: dict[str, tuple[str, ...]] = {
    "OPT_BTC": ("COINAPI", "DERIBIT", "INTERNAL"),
    "OPT_ETH": ("COINAPI", "DERIBIT", "INTERNAL"),
    "OPT_VIX": ("CBOE",),
    # Equity / commodity / FX roots — IVolatility ingest only.
    "OPT_SP_500": ("IVOLATILITY",),
    "OPT_NASDAQ_100": ("IVOLATILITY",),
    "OPT_GOLD": ("IVOLATILITY",),
    "OPT_T_NOTE_10_Y": ("IVOLATILITY",),
    "OPT_T_BOND": ("IVOLATILITY",),
    "OPT_EURUSD": ("IVOLATILITY",),
    "OPT_JPYUSD": ("IVOLATILITY",),
}

# Roots whose quotes (eodDatas) may surface but Greeks must NEVER surface.
# OPT_ETH stays blocked at the data layer until a curated greeks vendor
# is wired in (Phase 2+ scope). OPT_VIX was unblocked when CBOE greeks
# pass-through was enabled — engine-side compute remains gated until
# the FUT_VIX forward-curve resolver is fixed.
_GREEKS_BLOCKED_ROOTS: frozenset[str] = frozenset({"OPT_ETH"})


# Cold-start seed ratios (measured 2026-05-18 against `tcg-instrument`).
# Used only on the very first call when the live measurement fails AND
# no prior cache exists. Once the lazy refresh succeeds, subsequent
# misses serve the measured snapshot instead.
_SEED_RATIOS: dict[str, float] = {
    "OPT_SP_500": 0.997,
    "OPT_NASDAQ_100": 1.0,
    "OPT_GOLD": 1.0,
    "OPT_T_NOTE_10_Y": 0.997,
    "OPT_T_BOND": 1.0,
    "OPT_EURUSD": 1.0,
    "OPT_JPYUSD": 0.299,
    "OPT_BTC": 0.366,
    # CBOE ships no greeks for VIX as of 2026-05-18; engine computes via
    # Black-76 + FUT_VIX forward (Phase 2 of the rollout).
    "OPT_VIX": 0.0,
    # No Deribit feed wired in; engine is also gated.
    "OPT_ETH": 0.0,
}


# ---------------------------------------------------------------------------
# Lazy TTL cache for stored-greeks coverage ratios
# ---------------------------------------------------------------------------

# 24-hour TTL — coverage ratios are vendor-determined and drift slowly.
_RATIO_TTL_SECONDS: float = 24 * 3600

# Sample size per root for the $sample aggregation. 500 is enough for
# Wilson-interval ratio confidence of ~±4 pp on a midline-50% root, and
# fits within the <2s parallel budget against production Mongo.
_RATIO_SAMPLE_SIZE: int = 500

# Top-level Mongo field whose presence/absence drives the ratio.
_RATIO_GREEKS_FIELD: str = "eodGreeks"

# Single lock prevents thundering herd on cold start / TTL expiry.
_RATIO_REFRESH_LOCK: asyncio.Lock = asyncio.Lock()


@dataclass(frozen=True)
class _RatioCacheEntry:
    ratios: dict[str, float]
    measured_at: float  # time.monotonic() epoch


_ratio_cache: _RatioCacheEntry | None = None


async def _measure_stored_greeks_ratios(
    db: AsyncIOMotorDatabase,
) -> dict[str, float]:
    """Run ``$sample`` per known root in parallel via ``asyncio.gather``.

    Returns a dict ``{root: ratio_of_docs_with_eodGreeks}``. Each root is
    measured against its own collection (``db[root]``). Per-root failures
    are silently dropped (logged at WARNING) so a single bad collection
    cannot poison the whole snapshot.
    """
    roots = tuple(_PRIORITY_BY_ROOT.keys())

    async def _one(root: str) -> tuple[str, float] | None:
        pipeline = [
            {"$sample": {"size": _RATIO_SAMPLE_SIZE}},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "with_greeks": {
                        "$sum": {
                            "$cond": [
                                {"$ifNull": [f"${_RATIO_GREEKS_FIELD}", False]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]
        try:
            async for doc in db[root].aggregate(pipeline):
                total = doc.get("total") or 0
                with_greeks = doc.get("with_greeks") or 0
                ratio = (with_greeks / total) if total else 0.0
                return root, ratio
            return root, 0.0
        except Exception as exc:  # noqa: BLE001 — Mongo errors and surprises both go here
            logger.warning(
                "stored_greeks_ratio measurement failed for root=%s: %s", root, exc
            )
            return None

    pairs = await asyncio.gather(*(_one(r) for r in roots), return_exceptions=False)
    return {root: ratio for pair in pairs if pair is not None for (root, ratio) in (pair,)}


async def get_stored_greeks_ratios(
    db: AsyncIOMotorDatabase,
) -> dict[str, float]:
    """Return the 24h-TTL cached ratio dict, refreshing on miss.

    Behavior:
      * within TTL: return cache as-is.
      * expired or empty: acquire the lock; double-check; re-measure;
        populate cache; return measured values.
      * measurement failure WITH prior cache: serve the stale cache
        (logged at WARNING).
      * measurement failure WITHOUT prior cache: serve ``_SEED_RATIOS``.

    The function is safe to call concurrently; only one measurement
    runs at a time (the lock single-threads the refresh, others wait).
    """
    global _ratio_cache
    now = time.monotonic()
    if _ratio_cache is not None and (now - _ratio_cache.measured_at) < _RATIO_TTL_SECONDS:
        return _ratio_cache.ratios

    async with _RATIO_REFRESH_LOCK:
        # Double-check: another waiter may have refreshed while we waited.
        if (
            _ratio_cache is not None
            and (time.monotonic() - _ratio_cache.measured_at) < _RATIO_TTL_SECONDS
        ):
            return _ratio_cache.ratios
        try:
            ratios = await _measure_stored_greeks_ratios(db)
            _ratio_cache = _RatioCacheEntry(
                ratios=ratios, measured_at=time.monotonic()
            )
            return ratios
        except Exception as exc:  # noqa: BLE001 — defensive; _measure already handles per-root
            logger.warning("stored_greeks_ratio cache refresh failed: %s", exc)
            if _ratio_cache is not None:
                return _ratio_cache.ratios
            return dict(_SEED_RATIOS)


def _reset_ratio_cache_for_tests() -> None:
    """Test-only seam: clear the cached snapshot.

    Tests that exercise TTL expiry / cold-start use this between cases
    to avoid leaking state across the suite. NOT a public API; do not
    call from production code.
    """
    global _ratio_cache
    _ratio_cache = None


def select_provider(
    collection: str,
    eod_datas: Mapping[str, Any] | None = None,
) -> str | None:
    """Return the chosen provider key for *collection*, or None when no
    accepted provider has data on the supplied doc.

    Walks ``_PRIORITY_BY_ROOT[collection]`` in order; returns the first
    name whose ``eodDatas[name]`` list is non-empty. No fallback to
    other providers — if the doc only carries an unknown provider, the
    row is dropped (loudly) so the curation gap is visible rather than
    masked.
    """
    if not eod_datas:
        return None
    for candidate in _PRIORITY_BY_ROOT.get(collection, ()):
        bars = eod_datas.get(candidate)
        if bars:
            return candidate
    return None


def has_greeks_for_root(collection: str) -> bool:
    """Return False for roots where Greeks are blocked at the data layer.

    Currently only OPT_ETH is blocked (no curated greeks vendor wired in).
    OPT_VIX returns True so any CBOE-stored greeks pass through; the
    engine-side compute path is independently gated.
    """
    return collection not in _GREEKS_BLOCKED_ROOTS


def provider_priority(collection: str) -> tuple[str, ...]:
    """Public read of the per-root priority list (for diagnostics / tests)."""
    return _PRIORITY_BY_ROOT.get(collection, ())
