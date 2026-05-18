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
"""

from __future__ import annotations

from typing import Any, Mapping

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


# Roots whose engine-side Black-76 compute is blocked. MUST stay in sync
# with `tcg.engine.options.pricing._gating._BLOCKED_ROOTS` (kept here as
# data-layer metadata because the data layer cannot import the engine —
# guardrail `engine-data-isolation`). The drift is caught by
# `tests/unit/data/options/test_provider.py::test_no_compute_roots_mirrors_engine_gate`.
_NO_COMPUTE_ROOTS: frozenset[str] = frozenset({"OPT_ETH"})


# Fraction of docs per root that carry ``eodGreeks`` in the live Mongo,
# measured 2026-05-18 against `tcg-instrument`. Drives the left-nav badge
# variant on the frontend (`>=0.9` solid "Greeks", `0.1-0.9` split badge,
# `<0.1` falls through to the gray "Comp. Greeks" if compute is available).
#
# Why hardcoded instead of a live `count_documents` on each /api/options/roots
# call: counting docs-with-eodGreeks across all ~10 collections at request
# time is dominated by Mongo round-trip and an unindexed key check; in
# benchmarks against the production DB it pushed the endpoint past the
# frontend's 60s timeout. Ratios are vendor-determined and change very
# slowly (a new vendor would also touch `_PRIORITY_BY_ROOT`, so this stays
# co-located). Update when vendor coverage changes meaningfully.
_STORED_GREEKS_RATIO_BY_ROOT: dict[str, float] = {
    "OPT_SP_500": 0.997,
    "OPT_NASDAQ_100": 1.0,
    "OPT_GOLD": 1.0,
    "OPT_T_NOTE_10_Y": 0.997,
    "OPT_T_BOND": 1.0,
    "OPT_EURUSD": 1.0,
    "OPT_JPYUSD": 0.299,
    "OPT_BTC": 0.366,
    # CBOE ships no greeks for VIX (0/59,272 docs as of 2026-05-18); engine
    # computes via Black-76 + FUT_VIX forward (Phase 2 of the rollout).
    "OPT_VIX": 0.0,
    # No Deribit feed wired in; engine is also gated. Badge omitted.
    "OPT_ETH": 0.0,
}


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


def has_computed_greeks_for_root(collection: str) -> bool:
    """Return True when the engine can compute greeks for this root.

    Mirrors the engine's ``_BLOCKED_ROOTS`` gate (kept in sync via the
    `_NO_COMPUTE_ROOTS` registry above) because the data layer cannot
    import from ``tcg.engine`` (engine-data-isolation guardrail).
    """
    return collection not in _NO_COMPUTE_ROOTS


def stored_greeks_ratio_for_root(collection: str) -> float:
    """Return the fraction of docs in this collection carrying ``eodGreeks``.

    Returns 0.0 for unknown roots (a new root defaults to "no stored greeks"
    until measured). See `_STORED_GREEKS_RATIO_BY_ROOT` for the measurement
    methodology and how to update it.
    """
    return _STORED_GREEKS_RATIO_BY_ROOT.get(collection, 0.0)


def provider_priority(collection: str) -> tuple[str, ...]:
    """Public read of the per-root priority list (for diagnostics / tests)."""
    return _PRIORITY_BY_ROOT.get(collection, ())
