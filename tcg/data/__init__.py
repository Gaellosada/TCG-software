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
from tcg.data._sql.connection import DwhConnectionPool


async def create_services(dwh_pool: DwhConnectionPool) -> dict[str, Any]:
    """Factory function. Builds market data service from dwh pool.

    Parameters
    ----------
    dwh_pool:
        A ``DwhConnectionPool`` handle (already connected, read-only).

    Returns
    -------
    dict with ``"market_data"`` key mapped to a ``DefaultMarketDataService``.
    """
    market_data = DefaultMarketDataService(dwh_pool)
    return {"market_data": market_data}


__all__ = [
    "MarketDataService",
    "StrategyStore",
    "ResultStore",
    "create_services",
]
