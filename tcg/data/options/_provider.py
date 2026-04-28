"""Provider selection per OPT_* root.

Each root has an explicit list of accepted providers (the priorities
below). ``select_provider`` walks them in order and returns the first
that has bars on the supplied doc. There is intentionally NO "any
provider with data" fallback — if a doc carries a provider we have
not curated, the row is dropped loudly so the unexpected-vendor case
is visible (and easy to fix by extending the list) rather than
silently masked.

Greeks gating: OPT_VIX / OPT_ETH always return ``has_greeks=False``;
quote data may still surface from ``eodDatas`` (CBOE for VIX,
COINAPI / DERIBIT for ETH).
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
_GREEKS_BLOCKED_ROOTS: frozenset[str] = frozenset({"OPT_VIX", "OPT_ETH"})


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
    """Return False for roots where Greeks are blocked (VIX / ETH)."""
    return collection not in _GREEKS_BLOCKED_ROOTS


def provider_priority(collection: str) -> tuple[str, ...]:
    """Public read of the per-root priority list (for diagnostics / tests)."""
    return _PRIORITY_BY_ROOT.get(collection, ())
