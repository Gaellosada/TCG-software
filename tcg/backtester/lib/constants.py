"""Project-wide constants — magic numbers and string maps surfaced once.

Other lib modules MUST import from here rather than redefining literals locally.
The `tests/test_constants.py` regression bans new `\\b252\\b` literals outside
`lib/constants.py` and `lib/options.py:_BASIS_DENOM`.
"""
from __future__ import annotations

#: Number of trading days per calendar year used for annualisation
#: (Sharpe denominator, vol scaling). 252 is the US-equity convention; pass an
#: explicit `trading_days=` kwarg to lib functions for crypto (365) or other
#: 24/7 markets.
TRADING_DAYS_PER_YEAR: int = 252

#: Day-count basis denominators keyed by their string label. Mirrors
#: `lib/options.py:_BASIS_DENOM` — keep both in sync if you add a basis.
DAY_COUNT_BASES: dict[str, float] = {"365": 365.0, "365.25": 365.25, "252": 252.0}

#: Map from asset-class label (used in spec.universe.asset_class) to the
#: exchange/calendar code consumed by `pandas_market_calendars`. Probe 14
#: (`calendar_mismatch`) reads this to detect calendars set inconsistently
#: with the traded asset class.
CALENDAR_BY_ASSET_CLASS: dict[str, str] = {
    "INDEX": "XNYS",
    "ETF": "XNYS",
    "EQUITY": "XNYS",
    "FUT_VIX": "CFE",
    "FUT_SPX": "GLBX",
    "OPT_SPX": "CBOE",  # cash-settled index options
    "FOREX": "FX_24x5",
    "CRYPTO": "CRYPTO_24x7",
}

#: Equity-style underlying tickers whose options are American-style. Probe 18
#: (`exercise_style_mismatch`) fires when a spec declares pricing="european"
#: but the root is in this set.
AMERICAN_STYLE_ROOTS: frozenset[str] = frozenset(
    {"SPY", "QQQ", "IWM", "DIA", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL", "META"}
)

#: Cash-settled index roots whose options are European-style.
EUROPEAN_STYLE_ROOTS: frozenset[str] = frozenset(
    {"SPX", "NDX", "RUT", "VIX", "XSP", "XEO"}
)


__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "DAY_COUNT_BASES",
    "CALENDAR_BY_ASSET_CLASS",
    "AMERICAN_STYLE_ROOTS",
    "EUROPEAN_STYLE_ROOTS",
]
