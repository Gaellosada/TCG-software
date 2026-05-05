"""Common name -> canonical ticker resolution.

A tiny, hand-curated map covers the most frequent natural-language references
users type into prompts ("apple" -> "AAPL", "spy" -> "SPY"). The lookup is
case-insensitive and returns ``None`` when the term is unknown so callers can
surface an `instrument_lookup_failed` probe rather than silently guessing.
"""
from __future__ import annotations

TICKER_ALIASES: dict[str, str] = {
    "apple": "AAPL",
    "tesla": "TSLA",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "spy": "SPY",
    "spx": "SPX",
    "vix": "VIX",
}


def resolve_ticker(name: str | None) -> str | None:
    """Return the canonical ticker for *name*, case-insensitive, or ``None``.

    The function deliberately refuses to be clever: no fuzzy matching, no
    string-distance scoring. Callers that want suggestions should compose them
    on top by inspecting :data:`TICKER_ALIASES` directly.
    """
    if name is None:
        return None
    key = str(name).strip().casefold()
    if not key:
        return None
    return TICKER_ALIASES.get(key)
