"""V2MarketDataAdapter — the ``MarketDataService`` protocol, served from v2.

Satisfies the EXISTING protocol (``tcg/data/protocols.py``) so the engine, the
routers and every saved portfolio are untouched: the same leg definition runs on
both warehouses and any difference in the result is attributable to the data
alone (guardrail Sign 3 — ``tcg.engine`` never learns a data source exists).

Mirrors :class:`tcg.data.service.DefaultMarketDataService` method for method,
including its ``LRUCache`` and its UNCHANGED ``ContinuousSeriesBuilder``. The
rolling algorithm, the adjustment methods and the date filtering are SHARED, not
reimplemented, so a v1↔v2 difference in a continuous series is attributable to
the input bars alone.

**No silent fallthrough.** An unsupported collection or instrument RAISES
(:mod:`tcg.data._v2_compat.errors`, all HTTP 400). It never quietly returns v1
data and never returns empty: a portfolio that silently mixed sources would
produce a number attributable to neither warehouse.

**Options** are delegated wholesale to an injected ``OptionsDataReader`` (worker
2b's ``V2OptionsDataReader``, wired in Wave 3). This module deliberately does not
import it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Sequence

import numpy as np
import numpy.typing as npt

from tcg.data._cache import LRUCache
from tcg.data._rolling import ContinuousSeriesBuilder
from tcg.data._sql.connection import DwhConnectionPool
from tcg.data._utils import date_to_int, filter_date_range, int_to_date
from tcg.data._v2_compat import _sql_v2
from tcg.data._v2_compat._mapping import (
    V2_FUTURES_COLLECTION,
    V2_INDEX_COLLECTION,
    V2_INDEX_SYMBOL,
    V2_OPTIONS_COLLECTION,
    V2_RATE_COLLECTION,
    V2_RATE_SYMBOLS,
    expiration_int_from_futures_symbol,
    futures_symbol_from_expiration,
    v2_supports_collection,
)
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2FuturesContractUnavailable,
    V2InstrumentUnavailable,
)
from tcg.data.options.protocol import OptionsDataReader
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

# The non-option collections v2 serves, in the order list_collections returns.
# ``RATE`` is the object-level rate-series collection (US CMT yields); it carries
# no ``AssetClass`` (``asset_class_for`` returns ``None``), so it is only ever
# reached by an explicit ``collection="RATE"`` fetch — never a class-filtered
# discovery — exactly like a cash-rate leg needs.
_V2_PRICE_COLLECTIONS: tuple[str, ...] = (
    V2_INDEX_COLLECTION,
    V2_FUTURES_COLLECTION,
    V2_RATE_COLLECTION,
)


class V2MarketDataAdapter:
    """Read-only market data over ``tcg_instruments_v2``, in v1's shapes.

    Satisfies the ``MarketDataService`` protocol.
    """

    def __init__(
        self,
        dwh_pool: DwhConnectionPool,
        options_reader: OptionsDataReader | None = None,
        cache_size: int = 200,
    ) -> None:
        self._pool = dwh_pool
        self._options = options_reader
        self._cache = LRUCache(cache_size)
        self._roller = ContinuousSeriesBuilder()

    # --- Discovery --------------------------------------------------------- #

    async def list_collections(
        self,
        asset_class: AssetClass | None = None,
    ) -> list[str]:
        """List the non-option collections v2 can serve.

        Excludes ``OPT_*`` exactly as v1's reader does (options are discovered
        through ``list_option_roots``, not here).
        """
        out = [
            c
            for c in _V2_PRICE_COLLECTIONS
            if asset_class is None or self.asset_class_for(c) == asset_class
        ]
        return list(out)

    @staticmethod
    def asset_class_for(collection: str) -> AssetClass | None:
        """Classify a collection NAME into its coarse ``AssetClass``.

        Byte-identical logic to ``DefaultMarketDataService.asset_class_for``
        (asserted by ``test_v2_adapter.py``) — the portfolio router relies on
        this contract and it must NOT drift between sources.
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
        """List a v2-served collection's instruments, v1 symbols throughout."""
        self._require_collection(collection)
        if collection == V2_INDEX_COLLECTION:
            items = [
                InstrumentId(
                    symbol=V2_INDEX_SYMBOL,
                    asset_class=AssetClass.INDEX,
                    collection=V2_INDEX_COLLECTION,
                )
            ]
        elif collection == V2_FUTURES_COLLECTION:
            expirations = await _sql_v2.list_futures_expirations(self._pool)
            items = [
                InstrumentId(
                    symbol=futures_symbol_from_expiration(date_to_int(e)),
                    asset_class=AssetClass.FUTURE,
                    collection=V2_FUTURES_COLLECTION,
                )
                for e in expirations
            ]
        elif collection == V2_RATE_COLLECTION:
            # Rate series carry no dedicated AssetClass (no enum churn — spec
            # §1.5). They are tagged INDEX as the closest v1-shaped analogue (a
            # single non-tradeable level series); the RATE collection name is the
            # real discriminator the picker filters on. No dwh round-trip — the
            # loaded rate symbols are a small static set.
            items = [
                InstrumentId(
                    symbol=sym,
                    asset_class=AssetClass.INDEX,
                    collection=V2_RATE_COLLECTION,
                )
                for sym in V2_RATE_SYMBOLS
            ]
        else:
            # OPT_SP_500 is discovered through the options reader, not here —
            # mirroring v1, whose list_collections also excludes OPT_*.
            raise V2CollectionUnavailable(collection)

        return PaginatedResult(
            items=tuple(items[skip : skip + limit]),
            total=len(items),
            skip=skip,
            limit=limit,
        )

    # --- Price data -------------------------------------------------------- #

    async def get_prices(
        self,
        collection: str,
        instrument_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
        provider: str | None = None,
    ) -> PriceSeries | None:
        """Fetch OHLCV for one instrument.

        ``provider`` is accepted for protocol parity; v2 stores one curated
        series per instrument, so it does not branch the query.

        An unknown collection or a non-``IND_SP_500`` index symbol RAISES. So
        does a futures symbol whose expiration v2 does not list at all
        (:class:`V2FuturesContractUnavailable`) — v1 and v2 disagree on two ES
        expirations, and answering ``None`` there made an identity mismatch
        look like missing data.

        A symbol v2 DOES list but which has no bars inside the requested
        ``start``/``end`` window still returns ``None``, exactly as v1 does:
        that is an ordinary empty window, not an identity failure.
        """
        self._require_collection(collection)
        key = f"v2:{collection}:{instrument_id}:{provider}:{start}:{end}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        if collection == V2_INDEX_COLLECTION:
            if instrument_id != V2_INDEX_SYMBOL:
                raise V2InstrumentUnavailable(instrument_id, collection)
            result = await _sql_v2.read_index_prices(self._pool, start=start, end=end)
        elif collection == V2_RATE_COLLECTION:
            # Rate objects are surfaced by their ``object.symbol`` natural key.
            # An unknown symbol is an identity mismatch (loud), NOT missing data:
            # answering ``None`` there would let a typo look like an empty window.
            if instrument_id not in V2_RATE_SYMBOLS:
                raise V2InstrumentUnavailable(instrument_id, collection)
            result = await _sql_v2.read_rate_values(
                self._pool, instrument_id, start=start, end=end
            )
        else:
            expiration_int = expiration_int_from_futures_symbol(instrument_id)
            result = await _sql_v2.read_futures_prices(
                self._pool, expiration_int, start=start, end=end
            )
            if result is None:
                # Zero rows has two very different causes. Separate them on the
                # DIMENSION (one cheap indexed read, only on this cold path):
                # an expiration v2 never listed is an identity mismatch and
                # must be loud; an expiration it lists but with no bars in the
                # window is an ordinary empty range and stays ``None``.
                await self._require_v2_futures_expiration(instrument_id, expiration_int)

        if result is not None:
            self._cache.put(key, result)
        return result

    async def get_price_bounds(
        self,
        collection: str,
        instrument_id: str,
        *,
        provider: str | None = None,
    ) -> tuple[int | None, int | None]:
        """First/last available ``trade_date`` (``(min, max)`` YYYYMMDD ints, or
        ``(None, None)``) for one instrument — the endpoints of
        :meth:`get_prices`.

        The v1 default-warehouse path serves the cache-status range fan-out, so
        v2 never receives this on the hot path; the adapter derives the bounds
        from its own :meth:`get_prices` (correct, off the hot path) purely to
        satisfy the shared ``MarketDataService`` contract.
        """
        series = await self.get_prices(collection, instrument_id, provider=provider)
        if series is None or len(series) == 0:
            return (None, None)
        return (int(series.dates[0]), int(series.dates[-1]))

    # --- Continuous futures ------------------------------------------------ #

    async def get_continuous(
        self,
        collection: str,
        roll_config: ContinuousRollConfig,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> ContinuousSeries | None:
        """Build a continuous futures series from individual v2 contracts.

        Feeds the UNCHANGED roller, then applies the same date filter v1 does.
        """
        if not collection.startswith("FUT_"):
            raise DataNotFoundError(
                f"Collection '{collection}' is not a futures collection"
            )
        self._require_collection(collection)

        key = (
            f"v2:continuous:{collection}:{roll_config.strategy}"
            f":{roll_config.adjustment}:{roll_config.cycle}"
            f":{roll_config.roll_offset_days}:{roll_config.rank}"
            f":{start}:{end}"
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        contracts = await _sql_v2.read_futures_contracts(self._pool)
        if not contracts:
            return None

        result = self._roller.build(contracts, roll_config, collection=collection)
        if len(result.prices) == 0:
            return None

        if start is not None or end is not None:
            filtered_prices = filter_date_range(result.prices, start, end)
            if filtered_prices is None:
                return None
            start_int = date_to_int(start) if start is not None else 0
            end_int = date_to_int(end) if end is not None else 99999999
            result = ContinuousSeries(
                collection=result.collection,
                roll_config=result.roll_config,
                prices=filtered_prices,
                roll_dates=tuple(
                    rd for rd in result.roll_dates if start_int <= rd <= end_int
                ),
                contracts=result.contracts,
            )

        self._cache.put(key, result)
        return result

    async def get_available_cycles(self, collection: str) -> list[str]:
        """Return available expiration cycles.

        Every v1 ``FUT_SP_500`` contract carries the EMPTY STRING as its
        ``expiration_cycle``, so v1's DISTINCT returns exactly ``[""]``. The
        adapter mirrors that rather than reporting v2's object-level
        ``'quarterly'``, which no v1 caller would recognise.
        """
        self._require_collection(collection)
        if collection == V2_FUTURES_COLLECTION:
            return [_sql_v2.V1_FUTURES_CYCLE]
        return []

    async def get_aligned_prices(
        self,
        legs: dict[str, InstrumentId | ContinuousLegSpec],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[npt.NDArray[np.int64], dict[str, PriceSeries]]:
        """Fetch multiple instruments and inner-join them on common dates.

        Same algorithm as v1 (fetch → intersect → mask), so alignment cannot be
        a source of v1/v2 divergence.
        """
        if not legs:
            raise ValidationError("No legs provided for alignment")

        fetched: dict[str, PriceSeries] = {}
        for label, spec in legs.items():
            if isinstance(spec, InstrumentId):
                series = await self.get_prices(
                    spec.collection, spec.symbol, start=start, end=end
                )
            elif isinstance(spec, ContinuousLegSpec):
                result = await self.get_continuous(
                    spec.collection, spec.roll_config, start=start, end=end
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

        date_sets = [set(ps.dates.tolist()) for ps in fetched.values()]
        common: set[int] = date_sets[0]
        for ds in date_sets[1:]:
            common &= ds
        if not common:
            raise ValidationError("No overlapping dates across instruments")

        common_dates = np.array(sorted(common), dtype=np.int64)
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

    # --- Futures contract lookup ------------------------------------------- #

    async def find_futures_contract_by_expiration(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the v1 symbol of the contract expiring exactly on that date."""
        self._require_collection(collection)
        expirations = await _sql_v2.list_futures_expirations(self._pool)
        for e in expirations:
            if date_to_int(e) == expiration_int:
                return futures_symbol_from_expiration(expiration_int)
        return None

    async def find_front_futures_contract_on_or_after(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the v1 symbol of the NEAREST contract expiring on/after date."""
        self._require_collection(collection)
        expirations = await _sql_v2.list_futures_expirations(self._pool)
        for e in expirations:  # ascending from SQL
            e_int = date_to_int(e)
            if e_int >= expiration_int:
                return futures_symbol_from_expiration(e_int)
        return None

    async def list_futures_contract_meta(
        self,
        collection: str,
        *,
        cycle: str | None = None,
    ) -> list[FuturesContractMeta]:
        """List the root's contracts (symbol / expiration / contract_size).

        ``cycle`` filters against v1's stored value, the empty string — so
        ``cycle=""`` keeps everything and any other value yields nothing,
        reproducing v1's behaviour on this single-cycle root.
        """
        self._require_collection(collection)
        meta = await _sql_v2.list_futures_contract_meta(self._pool)
        if cycle is not None:
            meta = [m for m in meta if m.expiration_cycle == cycle]
        return meta

    # --- Options (delegated) ----------------------------------------------- #

    @property
    def options_reader(self) -> OptionsDataReader:
        """Return the injected options reader.

        Raises when none was injected rather than returning ``None``, so an
        options leg on a price-only adapter fails at the boundary with a named
        cause instead of an ``AttributeError`` deep in the engine.
        """
        if self._options is None:
            raise V2CollectionUnavailable(V2_OPTIONS_COLLECTION)
        return self._options

    async def get_option_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        return await self.options_reader.get_contract(collection, contract_id)

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
        return await self.options_reader.query_chain(
            root,
            date,
            type,
            expiration_min,
            expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
        )

    async def list_option_roots(self) -> list[OptionRootInfo]:
        return await self.options_reader.list_roots()

    async def list_option_expirations(self, root: str) -> list[date]:
        return await self.options_reader.list_expirations(root)

    async def option_trade_date_coverage(
        self, root: str
    ) -> tuple[date | None, date | None]:
        return await self.options_reader.trade_date_coverage(root)

    async def option_cycle_trade_date_span(
        self,
        root: str,
        start: date | None = None,
        end: date | None = None,
        cycle: str | Sequence[str] | None = None,
    ) -> tuple[date | None, date | None]:
        return await self.options_reader.cycle_trade_date_span(
            root, start, end, cycle=cycle
        )

    async def list_option_expirations_filtered(
        self,
        root: str,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
    ) -> list[date]:
        return await self.options_reader.list_expirations_filtered(
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
        return await self.options_reader.list_expirations_by_date(
            root,
            start,
            end,
            option_type=option_type,
            cycle=cycle,
            expiration_max=expiration_max,
        )

    # --- Internal ---------------------------------------------------------- #

    @staticmethod
    def _require_collection(collection: str) -> None:
        """Gate every entry point. NEVER falls through to v1 (spec §1.4)."""
        if not v2_supports_collection(collection):
            raise V2CollectionUnavailable(collection)

    async def _require_v2_futures_expiration(
        self,
        symbol: str,
        expiration_int: int,
    ) -> None:
        """Raise unless v2 lists an ES contract with exactly this expiration.

        Called ONLY when a single-contract read came back empty, so the extra
        dimension query never touches the hot path. Reports the nearest listed
        expiration because the realistic cause is the known one-day listing
        offset, not absent data — see
        :class:`~tcg.data._v2_compat.errors.V2FuturesContractUnavailable`.
        """
        listed = await _sql_v2.list_futures_expirations(self._pool)
        if any(date_to_int(e) == expiration_int for e in listed):
            return
        nearest: int | None = None
        if listed:
            # Compare real dates, not YYYYMMDD ints — integer distance is not
            # day distance across a month or year boundary.
            target = int_to_date(expiration_int)
            nearest = date_to_int(min(listed, key=lambda e: abs((e - target).days)))
        raise V2FuturesContractUnavailable(symbol, nearest)
