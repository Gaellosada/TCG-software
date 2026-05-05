"""Public type re-exports for clean snippet imports.

Snippets import `from tcg_backtester.lib import types` and reference the spec
dataclasses (ExecutionConfig, SizingConfig, BacktestSpec, BacktestResult, Trade)
plus the data containers (PriceSeries, OptionContractSeries, OptionChainSnapshot)
and metrics (MetricsSuite). This module is the single import surface for them.
"""
from __future__ import annotations

from .data_load import (
    OptionChainSnapshot,
    OptionContractSeries,
    OptionDailyRow,
    PriceSeries,
)
from .engine import (
    AtmSelector,
    BacktestResult,
    BacktestSpec,
    ContractSelector,
    DaysToHold,
    DeltaSelector,
    DteSelector,
    ExecutionConfig,
    ExitRule,
    ExitSignal,
    ExpirySelector,
    FixedExpirySelector,
    HoldToExpiration,
    MonthlySelector,
    MoneynessSelector,
    OptionLeg,
    OptionLegSpec,
    SizingConfig,
    StrikeOffsetPctSelector,
    Trade,
    TrailingStop,
    WeeklySelector,
)
from .metrics import MetricsSuite

__all__ = [
    "ExecutionConfig",
    "SizingConfig",
    "OptionLeg",
    "OptionLegSpec",
    "ContractSelector",
    "AtmSelector",
    "DeltaSelector",
    "StrikeOffsetPctSelector",
    "MoneynessSelector",
    "ExpirySelector",
    "DteSelector",
    "WeeklySelector",
    "MonthlySelector",
    "FixedExpirySelector",
    "ExitRule",
    "HoldToExpiration",
    "DaysToHold",
    "ExitSignal",
    "TrailingStop",
    "BacktestSpec",
    "BacktestResult",
    "Trade",
    "PriceSeries",
    "OptionContractSeries",
    "OptionChainSnapshot",
    "OptionDailyRow",
    "MetricsSuite",
]
