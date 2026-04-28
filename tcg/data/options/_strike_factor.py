"""Strike-factor configuration per OPT_* root.

Phase 1A output. Multiplier applied to raw `OPT_*.strike` to bring it onto the
joined underlying's price scale for K/S computation. `strike_factor_verified`
gates UI display: roots with False render a verification-pending banner per
spec §4.7.
"""

STRIKE_FACTOR: dict[str, float] = {
    "OPT_SP_500": 1.0,
    "OPT_GOLD": 1.0,
    "OPT_NASDAQ_100": 1.0,
    "OPT_BTC": 1.0,
    "OPT_T_NOTE_10_Y": 1.0,
    "OPT_T_BOND": 1.0,
    "OPT_EURUSD": 1.0,
    "OPT_JPYUSD": 1.0,
    "OPT_VIX": 1.0,
    "OPT_ETH": 1.0,
}

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
