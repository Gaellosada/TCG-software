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


async def create_services(dwh_pool: DwhConnectionPool) -> dict[str, Any]:
    """Factory function. Builds market data services from the dwh pool.

    Both the v1 (``tcg_instruments``) and v2 (``tcg_instruments_v2``) services
    share the SAME read-only ``tcg_read`` pool — v2 binds its schema per-query,
    it does NOT open a second pool.

    Parameters
    ----------
    dwh_pool:
        A ``DwhConnectionPool`` handle (already connected, read-only).

    Returns
    -------
    dict with ``"market_data"`` (v1 :class:`DefaultMarketDataService`) and
    ``"market_data_v2"`` (:class:`DefaultMarketDataServiceV2`).
    """
    market_data = DefaultMarketDataService(dwh_pool)
    market_data_v2 = DefaultMarketDataServiceV2(dwh_pool)
    return {"market_data": market_data, "market_data_v2": market_data_v2}


__all__ = [
    "MarketDataService",
    "StrategyStore",
    "ResultStore",
    "create_services",
]
