"""DefaultMarketDataService -- concrete implementation of MarketDataService.

Composes the dwh-backed ``SqlInstrumentReader`` + ``SqlOptionsDataReader``
(PostgreSQL warehouse) and an ``LRUCache``. Continuous futures are built by the
unchanged ``ContinuousSeriesBuilder`` fed SQL-sourced contracts.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Sequence

import numpy as np
import numpy.typing as npt

from tcg.types.common import PaginatedResult
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import (
    AssetClass,
    ContinuousLegSpec,
    ContinuousRollConfig,
    ContinuousSeries,
    FuturesContractMeta,
    InstrumentId,
    PriceSeries,
)
from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)

from tcg.data._cache import LRUCache
from tcg.data._sql.connection import DwhConnectionPool
from tcg.data._sql.instruments import SqlInstrumentReader
from tcg.data._sql.options import SqlOptionsDataReader
from tcg.data._rolling import ContinuousSeriesBuilder
from tcg.data._utils import date_to_int, filter_date_range
from tcg.data.options.protocol import OptionsDataReader


class DefaultMarketDataService:
    """Read-only market data backed by PostgreSQL dwh with LRU caching.

    Satisfies the ``MarketDataService`` protocol.
    """

    def __init__(
        self,
        dwh_pool: DwhConnectionPool,
        cache_size: int = 200,
    ) -> None:
        self._sql = SqlInstrumentReader(dwh_pool)
        self._options = SqlOptionsDataReader(dwh_pool)
        self._cache = LRUCache(cache_size)
        self._roller = ContinuousSeriesBuilder()

    # --- Discovery ---

    async def list_collections(
        self,
        asset_class: AssetClass | None = None,
    ) -> list[str]:
        """List all non-option collections (INDEX, ETF, FUND, FOREX, FUT_*).

        Delegates to the reader, which maps dwh ``asset_class`` onto the
        Mongo-era coarse ``AssetClass`` (ETF/FUND/FOREX → EQUITY, INDEX →
        INDEX, FUT_* → FUTURE) and excludes OPT_*.
        """
        return await self._sql.list_collections(asset_class)

    @staticmethod
    def asset_class_for(collection: str) -> AssetClass | None:
        """Classify a collection NAME into its coarse ``AssetClass``.

        Pure name-prefix logic (no DB hit), preserving the old
        ``CollectionRegistry.asset_class_for`` contract the portfolio router
        relies on: ``FUT_*`` → FUTURE, ``INDEX`` → INDEX, ETF/FUND/FOREX →
        EQUITY. Returns ``None`` for unknown / OPT_* names so callers raise a
        clean validation error rather than guessing.
        """
        if collection.startswith("FUT_"):
            return AssetClass.FUTURE
        if collection == "INDEX":
            return AssetClass.INDEX
        if collection in ("ETF", "FUND", "FOREX"):
            return AssetClass.EQUITY
        return None

    async def list_instruments(
        self,
        collection: str,
        *,
        skip: int = 0,
        limit: int = 50,
    ) -> PaginatedResult[InstrumentId]:
        """List instruments in a collection by source_collection.

        Rejects unknown collections up-front (clean 404) so unvalidated route
        input can't silently return an empty page for a typo'd collection.
        """
        if not await self._sql.collection_exists(collection):
            raise DataNotFoundError(f"Collection '{collection}' not found")
        instruments, total = await self._sql.list_instruments(
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
        """Fetch OHLCV prices for a single instrument.

        Rejects unknown collections up-front (parity with the Mongo path) so
        unvalidated route input can't probe arbitrary ``source_collection``
        values; a genuine unknown instrument within a valid collection still
        returns ``None`` (404 at the route).
        """
        if not await self._sql.collection_exists(collection):
            raise DataNotFoundError(f"Collection '{collection}' not found")
        cache_key = self._make_key(collection, instrument_id, provider, start, end)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = await self._sql.read_prices(
            collection,
            instrument_id,
            provider=provider,
            start=start,
            end=end,
        )

        if result is not None:
            self._cache.put(cache_key, result)
        return result

    # --- Continuous futures series ---

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
        fetches contracts from SQL, builds the continuous series via the
        rolling engine (unchanged), and optionally filters by date range.
        """
        if not collection.startswith("FUT_"):
            raise DataNotFoundError(
                f"Collection '{collection}' is not a futures collection"
            )

        cache_key = self._make_continuous_key(collection, roll_config, start, end)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        contracts = await self._sql.fetch_futures_contracts(
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
                rd for rd in result.roll_dates if start_int <= rd <= end_int
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
        """Return available expiration cycles for a collection.

        Used for both futures collections and option roots (the underlying
        DISTINCT is on ``expiration_cycle``, which both carry).
        """
        return await self._sql.fetch_available_cycles(collection)

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
                    spec.collection,
                    spec.symbol,
                    start=start,
                    end=end,
                )
            elif isinstance(spec, ContinuousLegSpec):
                result = await self.get_continuous(
                    spec.collection,
                    spec.roll_config,
                    start=start,
                    end=end,
                )
                series = result.prices if result is not None else None
            else:
                raise ValidationError(
                    f"Leg '{label}': expected InstrumentId or "
                    f"ContinuousLegSpec, got {type(spec).__name__}"
                )

            if series is None:
                raise DataNotFoundError(f"No price data found for leg '{label}'")
            fetched[label] = series

        # --- 2. Compute date intersection (inner join) ---
        date_sets = [set(ps.dates.tolist()) for ps in fetched.values()]
        common: set[int] = date_sets[0]
        for ds in date_sets[1:]:
            common &= ds

        if not common:
            raise ValidationError("No overlapping dates across instruments")

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

    # --- Options (Phase 1B Module 1) ---

    @property
    def options_reader(self) -> OptionsDataReader:
        """Return the underlying options data reader."""
        return self._options

    async def get_option_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        return await self._options.get_contract(collection, contract_id)

    async def query_options_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        return await self._options.query_chain(
            root,
            date,
            type,
            expiration_min,
            expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
        )

    async def list_option_roots(self) -> list[OptionRootInfo]:
        return await self._options.list_roots()

    async def list_option_expirations(self, root: str) -> list[date]:
        return await self._options.list_expirations(root)

    async def option_trade_date_coverage(
        self, root: str
    ) -> tuple[date | None, date | None]:
        return await self._options.trade_date_coverage(root)

    async def list_option_expirations_filtered(
        self,
        root: str,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
    ) -> list[date]:
        return await self._options.list_expirations_filtered(
            root, option_type=option_type, cycle=cycle
        )

    async def list_option_expirations_by_date(
        self,
        root: str,
        start: date,
        end: date,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
        expiration_max: date | None = None,
    ) -> dict[date, list[date]]:
        return await self._options.list_expirations_by_date(
            root,
            start,
            end,
            option_type=option_type,
            cycle=cycle,
            expiration_max=expiration_max,
        )

    # --- Futures contract lookup by expiration ---

    async def find_futures_contract_by_expiration(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the symbol (contract _id) of the futures contract in *collection*
        whose expiration field equals *expiration_int* (YYYYMMDD int).

        Returns None when no contract matches.
        Used by the VIX greeks resolver (Phase 2) to map an OPT_VIX
        expiration to the matching FUT_VIX contract.
        """
        return await self._sql.find_contract_by_expiration(collection, expiration_int)

    async def find_front_futures_contract_on_or_after(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the symbol of the FRONT futures contract in *collection* — the
        nearest one whose expiration is >= *expiration_int* (YYYYMMDD int), or
        None.

        Used by the option-on-future underlying resolver to map a serial/weekly
        option (no listed future of its own expiration) to the front-quarterly
        future (the Black-76 forward).
        """
        return await self._sql.find_front_contract_on_or_after(
            collection, expiration_int
        )

    async def list_futures_contract_meta(
        self,
        collection: str,
        *,
        cycle: str | None = None,
    ) -> list[FuturesContractMeta]:
        """List a futures root's contracts (symbol / expiration / contract_size).

        Cheap ``dim_instrument``-only scan; feeds futures-notional option sizing
        (``nearest_abs`` reference selection + the live ``M_fut`` read).
        """
        return await self._sql.list_futures_contract_meta(collection, cycle=cycle)

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
            f":{roll_config.roll_offset_days}:{roll_config.rank}"
            f":{start}:{end}"
        )
