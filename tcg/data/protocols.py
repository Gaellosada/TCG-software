"""Public protocols for the data module.

Three protocols define the data contract between tcg.data and its consumers.
Implementations are private; callers depend only on these interfaces.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, Sequence

import numpy as np
import numpy.typing as npt

from tcg.types.common import PaginatedResult
from tcg.types.market import (
    AssetClass,
    ContinuousLegSpec,
    ContinuousRollConfig,
    ContinuousSeries,
    FuturesContractMeta,
    InstrumentId,
    OptionsContinuousV2,
    PriceSeries,
)
from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)
from tcg.types.simulation import SimResult
from tcg.types.strategy import StrategyDefinition, StrategyMeta, StrategyStage

from tcg.data.options.protocol import OptionsDataReader


class MarketDataService(Protocol):
    """Read-only access to market data (indexes and futures).

    Callers never know the storage backend.
    """

    # --- Discovery ---

    async def list_collections(
        self,
        asset_class: AssetClass | None = None,
    ) -> list[str]: ...

    async def list_instruments(
        self,
        collection: str,
        *,
        skip: int = 0,
        limit: int = 50,
    ) -> PaginatedResult[InstrumentId]: ...

    # --- Price data (single instrument) ---

    async def get_prices(
        self,
        collection: str,
        instrument_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
        provider: str | None = None,
    ) -> PriceSeries | None: ...

    # --- Continuous futures series ---

    async def get_continuous(
        self,
        collection: str,
        roll_config: ContinuousRollConfig,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> ContinuousSeries | None: ...

    # --- Futures metadata ---

    async def get_available_cycles(
        self,
        collection: str,
    ) -> list[str]: ...

    # --- Multi-instrument (date alignment) ---

    async def get_aligned_prices(
        self,
        legs: dict[str, InstrumentId | ContinuousLegSpec],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[npt.NDArray[np.int64], dict[str, PriceSeries]]: ...

    # --- Options (Phase 1B Module 1) ---
    #
    # Stored-only, read-only. Implementations MUST NOT call into Module 2
    # (``tcg.engine.options.pricing``) — see guardrail #2.

    async def get_option_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries: ...

    async def query_options_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]: ...

    async def list_option_roots(self) -> list[OptionRootInfo]: ...

    async def list_option_expirations(self, root: str) -> list[date]: ...

    async def option_trade_date_coverage(
        self, root: str
    ) -> tuple[date | None, date | None]: ...

    async def list_option_expirations_filtered(
        self,
        root: str,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
    ) -> list[date]: ...

    async def list_option_expirations_by_date(
        self,
        root: str,
        start: date,
        end: date,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
        expiration_max: date | None = None,
    ) -> dict[date, list[date]]: ...

    # --- Futures contract lookup by expiration (Phase 2 VIX greeks) ---

    async def find_futures_contract_by_expiration(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the ``_id`` string of the futures contract whose
        ``expiration`` field equals *expiration_int* (YYYYMMDD int).
        Returns ``None`` when no contract matches.
        """
        ...

    async def find_front_futures_contract_on_or_after(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the ``_id`` of the FRONT futures contract — the nearest one in
        *collection* whose ``expiration`` is >= *expiration_int* (YYYYMMDD int).

        Used to resolve the front-quarterly future for an option-on-future whose
        own expiration has no listed future (serial/weekly months on a quarterly
        futures curve).  Returns ``None`` when no contract expires on/after the
        date.
        """
        ...

    async def list_futures_contract_meta(
        self,
        collection: str,
        *,
        cycle: str | None = None,
    ) -> list[FuturesContractMeta]:
        """List a futures root's contracts (symbol / expiration / contract_size).

        Cheap ``dim_instrument``-only scan feeding futures-notional option sizing:
        the ``nearest_abs`` reference selection (closest expiration in |time|) and
        the live ``M_fut`` read (the selected contract's ``contract_size``; NULL →
        signed-off config fallback).
        """
        ...

    @property
    def options_reader(self) -> OptionsDataReader:
        """Return the underlying options data reader.

        Exposes the ``OptionsDataReader`` port so callers in ``tcg.core``
        can pass it to engine adapters without accessing private attributes.
        """
        ...


class MarketDataServiceV2(Protocol):
    """Read-only access to the ``tcg_instruments_v2`` star schema.

    Mirrors :class:`MarketDataService` for v2: consumers in ``tcg.core`` depend
    on this interface, not on the concrete ``DefaultMarketDataServiceV2``.
    """

    async def list_objects(self) -> list[dict]: ...

    async def get_object_detail(self, object_id: int) -> dict: ...

    async def get_series(
        self,
        serie_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict: ...

    async def get_continuous_future(
        self,
        object_id: int,
        roll_config: ContinuousRollConfig,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> ContinuousSeries | None: ...

    async def get_future_cycles(self, object_id: int) -> list[str]: ...

    async def get_continuous_options(
        self,
        object_id: int,
        *,
        criterion: str,
        target: float,
        option_type: str,
        start: date | None = None,
        end: date | None = None,
    ) -> OptionsContinuousV2: ...


class StrategyStore(Protocol):
    """CRUD for strategy definitions."""

    async def save(self, strategy: StrategyDefinition) -> str: ...

    async def get(self, strategy_id: str) -> StrategyDefinition | None: ...

    async def get_by_name(self, name: str) -> StrategyDefinition | None: ...

    async def list(
        self,
        stage: StrategyStage | None = None,
        *,
        skip: int = 0,
        limit: int = 50,
    ) -> PaginatedResult[StrategyMeta]: ...

    async def delete(self, strategy_id: str) -> bool: ...

    async def update_stage(
        self,
        strategy_id: str,
        stage: StrategyStage,
    ) -> bool: ...


class ResultStore(Protocol):
    """Storage and retrieval of simulation results with provenance."""

    async def save(
        self,
        strategy_id: str,
        result: SimResult,
        label: str | None = None,
    ) -> str: ...

    async def get(self, result_id: str) -> SimResult | None: ...

    async def list_for_strategy(
        self,
        strategy_id: str,
        *,
        skip: int = 0,
        limit: int = 50,
    ) -> PaginatedResult[SimResult]: ...
