"""Default provider mappings for each collection type.

Exact matches are checked first; prefix fallbacks are checked second.
"""

from __future__ import annotations

PROVIDER_DEFAULTS: dict[str, str] = {
    # Exact matches (checked first)
    "INDEX": "YAHOO",
    "ETF": "YAHOO",
    "FUND": "BLOOMBERG",
    "FOREX": "YAHOO",
    "FUT_BTC": "DERIBIT",
    "FUT_ETH": "DERIBIT",
    "OPT_BTC": "DERIBIT",
    "OPT_ETH": "DERIBIT",
    # Prefix fallbacks (checked second)
    "FUT_": "IVOLATILITY",
    "OPT_": "IVOLATILITY",
}
