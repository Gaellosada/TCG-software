"""DefaultMarketDataService -- concrete implementation of MarketDataService.

Composes MongoInstrumentReader, CollectionRegistry, and LRUCache.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import numpy.typing as npt

from motor.motor_asyncio import AsyncIOMotorDatabase

from tcg.types.common import PaginatedResult
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import (
    AssetClass,
    ContinuousLegSpec,
    ContinuousRollConfig,
    ContinuousSeries,
    InstrumentId,
    PriceSeries,
)

from tcg.data._cache import LRUCache
from tcg.data._mongo.instruments import MongoInstrumentReader
from tcg.data._mongo.registry import CollectionRegistry
from tcg.data._rolling import ContinuousSeriesBuilder
from tcg.data._utils import date_to_int, filter_date_range


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
        self._roller = ContinuousSeriesBuilder()

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
        # Reject unknown collections up-front so unvalidated user input
        # from API routes can't reach into Mongo system collections.
        if collection not in self._registry:
            raise DataNotFoundError(
                f"Collection '{collection}' not found in registry"
            )
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
        """Build a continuous futures series from individual contracts.

        Validates that the collection is a futures collection (``FUT_`` prefix),
        fetches contracts from MongoDB, builds the continuous series via the
        rolling engine, and optionally filters by date range.
        """
        if collection not in self._registry:
            raise DataNotFoundError(
                f"Collection '{collection}' not found in registry"
            )
        if not collection.startswith("FUT_"):
            raise DataNotFoundError(
                f"Collection '{collection}' is not a futures collection"
            )

        cache_key = self._make_continuous_key(collection, roll_config, start, end)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        contracts = await self._mongo.fetch_futures_contracts(
            collection, cycle=roll_config.cycle
        )
        if not contracts:
            return None

        result = self._roller.build(contracts, roll_config, collection=collection)

        # Empty series means no usable data
        if len(result.prices) == 0:
            return None

        # Apply date range filter
        if start is not None or end is not None:
            filtered_prices = filter_date_range(result.prices, start, end)
            if filtered_prices is None:
                return None

            start_int = date_to_int(start) if start is not None else 0
            end_int = date_to_int(end) if end is not None else 99999999
            filtered_roll_dates = tuple(
                rd for rd in result.roll_dates
                if start_int <= rd <= end_int
            )

            result = ContinuousSeries(
                collection=result.collection,
                roll_config=result.roll_config,
                prices=filtered_prices,
                roll_dates=filtered_roll_dates,
                contracts=result.contracts,
            )

        self._cache.put(cache_key, result)
        return result

    async def get_available_cycles(self, collection: str) -> list[str]:
        """Return available expiration cycles for a futures collection."""
        if collection not in self._registry:
            raise DataNotFoundError(
                f"Collection '{collection}' not found in registry"
            )
        return await self._mongo.fetch_available_cycles(collection)

    async def get_aligned_prices(
        self,
        legs: dict[str, InstrumentId | ContinuousLegSpec],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[npt.NDArray[np.int64], dict[str, PriceSeries]]:
        """Fetch multiple instruments and align them on common dates (inner join).

        Parameters
        ----------
        legs:
            Mapping of user-chosen labels to either an ``InstrumentId``
            (for spot/index data) or a ``ContinuousLegSpec`` (for
            rolled futures).
        start, end:
            Optional date bounds applied to every leg before alignment.

        Returns
        -------
        (common_dates, aligned_series)
            ``common_dates`` is a sorted int64 array of YYYYMMDD dates
            present in **all** legs.  ``aligned_series`` maps each label
            to its ``PriceSeries`` filtered to those common dates.

        Raises
        ------
        ValidationError
            If ``legs`` is empty or the date intersection is empty.
        DataNotFoundError
            If any leg cannot be fetched.
        """
        if not legs:
            raise ValidationError("No legs provided for alignment")

        # --- 1. Fetch each leg ---
        fetched: dict[str, PriceSeries] = {}
        for label, spec in legs.items():
            if isinstance(spec, InstrumentId):
                series = await self.get_prices(
                    spec.collection, spec.symbol, start=start, end=end,
                )
            elif isinstance(spec, ContinuousLegSpec):
                result = await self.get_continuous(
                    spec.collection, spec.roll_config, start=start, end=end,
                )
                series = result.prices if result is not None else None
            else:
                raise ValidationError(
                    f"Leg '{label}': expected InstrumentId or "
                    f"ContinuousLegSpec, got {type(spec).__name__}"
                )

            if series is None:
                raise DataNotFoundError(
                    f"No price data found for leg '{label}'"
                )
            fetched[label] = series

        # --- 2. Compute date intersection (inner join) ---
        date_sets = [set(ps.dates.tolist()) for ps in fetched.values()]
        common: set[int] = date_sets[0]
        for ds in date_sets[1:]:
            common &= ds

        if not common:
            raise ValidationError(
                "No overlapping dates across instruments"
            )

        common_dates = np.array(sorted(common), dtype=np.int64)

        # --- 3. Filter each series to common dates ---
        aligned: dict[str, PriceSeries] = {}
        for label, ps in fetched.items():
            mask = np.isin(ps.dates, common_dates)
            aligned[label] = PriceSeries(
                dates=ps.dates[mask],
                open=ps.open[mask],
                high=ps.high[mask],
                low=ps.low[mask],
                close=ps.close[mask],
                volume=ps.volume[mask],
            )

        return common_dates, aligned

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

    @staticmethod
    def _make_continuous_key(
        collection: str,
        roll_config: ContinuousRollConfig,
        start: date | None,
        end: date | None,
    ) -> str:
        """Build a deterministic cache key for continuous series."""
        return (
            f"continuous:{collection}:{roll_config.strategy}"
            f":{roll_config.adjustment}:{roll_config.cycle}"
            f":{roll_config.roll_offset_days}"
            f":{start}:{end}"
        )
