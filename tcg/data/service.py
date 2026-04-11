"""DefaultMarketDataService -- concrete implementation of MarketDataService.

Composes MongoInstrumentReader, CollectionRegistry, and LRUCache.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import numpy.typing as npt

from motor.motor_asyncio import AsyncIOMotorDatabase

from tcg.types.common import PaginatedResult
from tcg.types.errors import DataNotFoundError
from tcg.types.market import (
    AssetClass,
    ContinuousRollConfig,
    ContinuousSeries,
    InstrumentId,
    PriceSeries,
)

from tcg.data._cache import LRUCache
from tcg.data._mongo.instruments import MongoInstrumentReader
from tcg.data._mongo.registry import CollectionRegistry


class DefaultMarketDataService:
    """Read-only market data backed by MongoDB with LRU caching.

    Satisfies the ``MarketDataService`` protocol.
    """

    def __init__(
        self,
        mongo_db: AsyncIOMotorDatabase,
        registry: CollectionRegistry,
        cache_size: int = 200,
    ) -> None:
        self._mongo = MongoInstrumentReader(mongo_db)
        self._registry = registry
        self._cache = LRUCache(cache_size)

    # --- Discovery ---

    async def list_collections(
        self,
        asset_class: AssetClass | None = None,
    ) -> list[str]:
        if asset_class is None:
            return list(self._registry.all_active)
        return [
            c
            for c in self._registry.all_active
            if self._registry.asset_class_for(c) == asset_class
        ]

    async def list_instruments(
        self,
        collection: str,
        *,
        skip: int = 0,
        limit: int = 50,
    ) -> PaginatedResult[InstrumentId]:
        if collection not in self._registry:
            raise DataNotFoundError(
                f"Collection '{collection}' not found in registry"
            )

        instruments, total = await self._mongo.list_instruments(
            collection, skip=skip, limit=limit
        )
        return PaginatedResult(
            items=tuple(instruments),
            total=total,
            skip=skip,
            limit=limit,
        )

    # --- Price data ---

    async def get_prices(
        self,
        collection: str,
        instrument_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
        provider: str | None = None,
    ) -> PriceSeries | None:
        cache_key = self._make_key(
            collection, instrument_id, provider, start, end
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = await self._mongo.read_prices(
            collection,
            instrument_id,
            provider=provider,
            start=start,
            end=end,
        )

        if result is not None:
            self._cache.put(cache_key, result)
        return result

    # --- Phase 2 / Phase 3 stubs ---

    async def get_continuous(
        self,
        collection: str,
        roll_config: ContinuousRollConfig,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> ContinuousSeries | None:
        raise NotImplementedError("Continuous rolling is Phase 2")

    async def get_aligned_prices(
        self,
        legs: dict[str, InstrumentId | ContinuousRollConfig],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[npt.NDArray[np.int64], dict[str, PriceSeries]]:
        raise NotImplementedError("Aligned prices is Phase 3")

    # --- Internal ---

    @staticmethod
    def _make_key(
        collection: str,
        instrument_id: str,
        provider: str | None,
        start: date | None,
        end: date | None,
    ) -> str:
        """Build a deterministic cache key."""
        return f"{collection}:{instrument_id}:{provider}:{start}:{end}"
