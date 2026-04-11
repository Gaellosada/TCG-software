"""Shared domain types -- the contracts between all TCG modules.

Re-exports every public type for convenience:
    from tcg.types import SimConfig, InstrumentId, Provenance, ...
"""

from tcg.types.common import PaginatedResult
from tcg.types.config import MongoConfig
from tcg.types.errors import (
    DataAccessError,
    DataNotFoundError,
    ErrorResponse,
    SimulationError,
    StrategyExecutionError,
    TCGError,
    ValidationError,
)
from tcg.types.market import (
    AdjustmentMethod,
    AssetClass,
    ContinuousRollConfig,
    ContinuousSeries,
    ContractPriceData,
    ContractSpec,
    InstrumentId,
    PriceSeries,
    RollStrategy,
)
from tcg.types.metrics import MetricsSuite
from tcg.types.portfolio import (
    PortfolioResult,
    PortfolioSpec,
    RebalanceFreq,
)
from tcg.types.provenance import DataVersion, Provenance, ResultSource
from tcg.types.simulation import (
    EngineType,
    EquityCurve,
    PositionSizingConfig,
    SimConfig,
    SimResult,
    SimulationRequest,
    SizingMethod,
    Trade,
)
from tcg.types.strategy import StrategyDefinition, StrategyMeta, StrategyStage

__all__ = [
    # common
    "PaginatedResult",
    # config
    "MongoConfig",
    # errors
    "DataAccessError",
    "DataNotFoundError",
    "ErrorResponse",
    "SimulationError",
    "StrategyExecutionError",
    "TCGError",
    "ValidationError",
    # market
    "AdjustmentMethod",
    "AssetClass",
    "ContinuousRollConfig",
    "ContinuousSeries",
    "ContractPriceData",
    "ContractSpec",
    "InstrumentId",
    "PriceSeries",
    "RollStrategy",
    # metrics
    "MetricsSuite",
    # portfolio
    "PortfolioResult",
    "PortfolioSpec",
    "RebalanceFreq",
    # provenance
    "DataVersion",
    "Provenance",
    "ResultSource",
    # simulation
    "EngineType",
    "EquityCurve",
    "PositionSizingConfig",
    "SimConfig",
    "SimResult",
    "SimulationRequest",
    "SizingMethod",
    "Trade",
    # strategy
    "StrategyDefinition",
    "StrategyMeta",
    "StrategyStage",
]
