"""Public protocols for the data module.

Three protocols define the data contract between tcg.data and its consumers.
Implementations are private; callers depend only on these interfaces.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt

from tcg.types.common import PaginatedResult
from tcg.types.market import (
    AssetClass,
    ContinuousLegSpec,
    ContinuousRollConfig,
    ContinuousSeries,
    InstrumentId,
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
