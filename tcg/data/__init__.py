"""Data module -- encapsulates ALL data complexity behind clean protocols.

Public exports:
    MarketDataService   (protocol)
    StrategyStore       (protocol)
    ResultStore         (protocol)
    create_services()   (factory -- the only concrete thing exported)
"""

from __future__ import annotations

from typing import Any

from tcg.data.protocols import MarketDataService, ResultStore, StrategyStore
from tcg.data.service import DefaultMarketDataService
from tcg.data.service_v2 import DefaultMarketDataServiceV2
from tcg.data._sql.connection import DwhConnectionPool
from tcg.data._v2_compat.adapter import V2MarketDataAdapter
from tcg.data._v2_compat.options_reader import V2OptionsDataReader


async def create_services(dwh_pool: DwhConnectionPool) -> dict[str, Any]:
    """Factory function. Builds market data services from the dwh pool.

    All services share the SAME read-only ``tcg_read`` pool — the v2 ones bind
    their schema per-query, they do NOT open a second pool.

    Parameters
    ----------
    dwh_pool:
        A ``DwhConnectionPool`` handle (already connected, read-only).

    Returns
    -------
    dict with three entries:

    ``"market_data"``
        v1 :class:`DefaultMarketDataService` over ``tcg_instruments`` — the
        DEFAULT for every compute (``data_source="v1"``).
    ``"market_data_v2"``
        :class:`DefaultMarketDataServiceV2` — the **object-id-keyed explorer**
        service backing the "Database v2" page. A different shape entirely; it
        does NOT satisfy ``MarketDataService`` and is never a compute source.
    ``"market_data_v2_compat"``
        :class:`V2MarketDataAdapter` — the ``MarketDataService``-shaped view of
        ``tcg_instruments_v2`` (v1 symbols/collections out), i.e. the service a
        compute binds for ``data_source="v2"``.
    """
    market_data = DefaultMarketDataService(dwh_pool)
    market_data_v2 = DefaultMarketDataServiceV2(dwh_pool)
    market_data_v2_compat = V2MarketDataAdapter(
        dwh_pool, options_reader=V2OptionsDataReader(dwh_pool)
    )
    return {
        "market_data": market_data,
        "market_data_v2": market_data_v2,
        "market_data_v2_compat": market_data_v2_compat,
    }


__all__ = [
    "MarketDataService",
    "StrategyStore",
    "ResultStore",
    "create_services",
]
