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


async def create_services(mongo_db: Any) -> dict[str, Any]:
    """Factory function. Discovers collections, builds all services.

    ``CollectionRegistry`` is created here and passed to the service --
    it never escapes the data module boundary.

    Parameters
    ----------
    mongo_db:
        An ``AsyncIOMotorDatabase`` handle. Typed as ``Any`` to avoid
        leaking Motor into the public interface.

    Returns
    -------
    dict with ``"market_data"`` key mapped to a ``DefaultMarketDataService``.
    """
    from tcg.data._mongo.registry import CollectionRegistry

    raw_names: list[str] = await mongo_db.list_collection_names()
    registry = CollectionRegistry(raw_names)
    market_data = DefaultMarketDataService(mongo_db, registry)
    return {"market_data": market_data}


__all__ = [
    "MarketDataService",
    "StrategyStore",
    "ResultStore",
    "create_services",
]
