"""Strike-factor configuration per OPT_* root.

Phase 1A output.  ``STRIKE_FACTOR_VERIFIED`` gates UI display: roots with False
render a verification-pending banner per spec §4.7.

The multiplier table that historically lived here (``STRIKE_FACTOR``) was dead
code: every Phase-1 caller computes ``K_over_S = strike / underlying_price``
on the raw strike (see ``chain.py`` and ``selection/_match.py``), so the
multiplier was never applied.  Re-introduce the table only when a caller
actually consumes it.
"""

STRIKE_FACTOR_VERIFIED: dict[str, bool] = {
    "OPT_SP_500": True,
    "OPT_GOLD": True,
    "OPT_NASDAQ_100": True,
    "OPT_BTC": True,
    "OPT_T_NOTE_10_Y": False,
    "OPT_T_BOND": False,
    "OPT_EURUSD": False,
    "OPT_JPYUSD": False,
    "OPT_VIX": True,
    "OPT_ETH": True,
}
