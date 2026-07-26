"""v2 compatibility layer — the ``tcg_instruments_v2`` star behind v1's protocols.

The single place in the codebase that knows both warehouse dialects. Everything
above it (engine, routers, saved portfolios) speaks v1 symbols and v1 DTOs only,
so one strategy definition runs unchanged on either source and any difference in
the result is attributable to the data rather than to the plumbing.
"""

from __future__ import annotations

from tcg.data._v2_compat.adapter import V2MarketDataAdapter
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2DataUnavailable,
    V2InstrumentUnavailable,
    V2SymbolError,
    V2UnsupportedCycle,
    V2UnsupportedField,
)

__all__ = [
    "V2MarketDataAdapter",
    "V2CollectionUnavailable",
    "V2DataUnavailable",
    "V2InstrumentUnavailable",
    "V2SymbolError",
    "V2UnsupportedCycle",
    "V2UnsupportedField",
]
