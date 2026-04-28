"""Provider selection per OPT_* root.

Decision E binds OPT_ETH to a scan-and-pick strategy (first non-empty
``eodDatas[provider]`` across all known providers). Every other root
maps to a single deterministic provider per spec §3.1 / §4.3.

Greeks gating: OPT_VIX / OPT_ETH always return ``has_greeks=False``;
quote data may still surface from ``eodDatas`` (CBOE for VIX).
"""

from __future__ import annotations

from typing import Any, Mapping

# Order matters for OPT_ETH (Decision E): scan in this order, return first match.
_ETH_PROVIDER_PRIORITY: tuple[str, ...] = (
    "DERIBIT",
    "INTERNAL",
    "IVOLATILITY",
    "CBOE",
)

# Roots whose quotes (eodDatas) may surface but Greeks must NEVER surface.
_GREEKS_BLOCKED_ROOTS: frozenset[str] = frozenset({"OPT_VIX", "OPT_ETH"})


def select_provider(
    collection: str,
    eod_datas: Mapping[str, Any] | None = None,
) -> str | None:
    """Return the chosen provider key for *collection*, or None when no
    provider has any data.

    Parameters
    ----------
    collection:
        OPT_* collection name (e.g. ``"OPT_SP_500"``).
    eod_datas:
        The raw ``eodDatas`` map from the doc, used only when the rule
        for *collection* is "scan all providers" (currently OPT_ETH).
        For deterministic-provider roots this argument is ignored.
    """
    if collection == "OPT_BTC":
        return "INTERNAL"
    if collection == "OPT_VIX":
        return "CBOE"
    if collection == "OPT_ETH":
        if not eod_datas:
            return None
        for candidate in _ETH_PROVIDER_PRIORITY:
            bars = eod_datas.get(candidate)
            if bars:  # non-empty list
                return candidate
        # Fallback: any non-empty provider in insertion order.
        for key, bars in eod_datas.items():
            if bars:
                return key
        return None
    # Default: every other OPT_* root uses IVOLATILITY.
    return "IVOLATILITY"


def has_greeks_for_root(collection: str) -> bool:
    """Return False for roots where Greeks are blocked (VIX / ETH)."""
    return collection not in _GREEKS_BLOCKED_ROOTS
