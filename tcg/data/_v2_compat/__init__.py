"""v2 compatibility layer — the ``tcg_instruments_v2`` star behind v1's protocols.

The single place in the codebase that knows both warehouse dialects. Everything
above it (engine, routers, saved portfolios) speaks v1 symbols and v1 DTOs only,
so one strategy definition runs unchanged on either source and any difference in
the result is attributable to the data rather than to the plumbing.
"""

from __future__ import annotations

from tcg.data._v2_compat._mapping import (
    EW_OBJECT_BY_CYCLE,
    V2_INDEX_COLLECTION,
    V2_INDEX_SYMBOL,
    V2_OPTIONS_COLLECTION,
    V2_SUPPORTED_COLLECTIONS,
)
from tcg.data._v2_compat.adapter import V2MarketDataAdapter
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2DataUnavailable,
    V2FuturesContractUnavailable,
    V2InstrumentUnavailable,
    V2MissingCycleFilter,
    V2SymbolError,
    V2UnsupportedCycle,
    V2UnsupportedField,
)
from tcg.data._v2_compat.options_reader import (
    V2_UNAVAILABLE_OPTION_STREAMS,
    V2OptionsDataReader,
)

__all__ = [
    "V2MarketDataAdapter",
    "V2OptionsDataReader",
    "V2CollectionUnavailable",
    "V2DataUnavailable",
    "V2FuturesContractUnavailable",
    "V2InstrumentUnavailable",
    "V2MissingCycleFilter",
    "V2SymbolError",
    "V2UnsupportedCycle",
    "V2UnsupportedField",
    # Constants the API boundary needs to validate a v2 run BEFORE the engine
    # is entered (see ``tcg.core.api._v2_preconditions``).
    "EW_OBJECT_BY_CYCLE",
    "V2_INDEX_COLLECTION",
    "V2_INDEX_SYMBOL",
    "V2_OPTIONS_COLLECTION",
    "V2_SUPPORTED_COLLECTIONS",
    "V2_UNAVAILABLE_OPTION_STREAMS",
]
