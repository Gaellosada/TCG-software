"""Per-root greeks gating + cold-start coverage ratios for OPT_* roots.

Motor-free since the dwh SQL cutover. The live consumers are the dwh-backed
``SqlOptionsDataReader`` (``has_greeks_for_root`` for the data-layer greek
block list, ``_SEED_RATIOS`` for the left-nav coverage badge). The former
Mongo machinery — per-root ``$sample`` ratio measurement, its TTL cache, and
``select_provider`` (which inspected a doc's ``eodDatas`` provider keys) — was
removed: dwh stores one curated series per instrument (no per-doc provider
arrays), and computing exact per-root stored-greeks ratios over the 103M-row
greeks fact cannot finish inside the statement timeout, so the badge uses the
measured ``_SEED_RATIOS`` baseline.

Greeks gating: OPT_ETH returns ``has_greeks=False`` (no curated greeks vendor
wired in); quotes may still surface. OPT_VIX is not blanket-blocked at the data
layer (CBOE greeks pass through with ``source="stored"`` where present); the
engine-side compute path is independently gated (see
``tcg.engine.options.pricing._gating``).
"""

from __future__ import annotations

# Roots whose quotes may surface but Greeks must NEVER surface from the data
# layer. OPT_ETH stays blocked until a curated greeks vendor is wired in.
_GREEKS_BLOCKED_ROOTS: frozenset[str] = frozenset({"OPT_ETH"})


# Per-root stored-greeks coverage ratio (measured 2026-05-18 against the source
# data). Drives the left-nav badge variant only; ``has_greeks_for_root`` gates
# whether greeks may surface at all.
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


def has_greeks_for_root(collection: str) -> bool:
    """Return False for roots where Greeks are blocked at the data layer.

    Currently only OPT_ETH is blocked (no curated greeks vendor wired in).
    OPT_VIX returns True so any stored CBOE greeks pass through; the engine-side
    compute path is independently gated.
    """
    return collection not in _GREEKS_BLOCKED_ROOTS
