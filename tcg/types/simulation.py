from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from tcg.types.market import ContinuousRollConfig, InstrumentId
from tcg.types.provenance import Provenance


class EngineType(StrEnum):
    VECTORIZED = "vectorized"
    EVENT_DRIVEN = "event_driven"   # Future


class SizingMethod(StrEnum):
    FIXED_FRACTIONAL = "fixed_fractional"
    VOL_TARGET = "vol_target"


@dataclass(frozen=True)
class PositionSizingConfig:
    method: SizingMethod = SizingMethod.FIXED_FRACTIONAL
    target_vol: float | None = None  # Annual, for VOL_TARGET
    lookback: int = 60               # Bars for realized vol estimation


@dataclass(frozen=True)
class SimConfig:
    """Configuration for a simulation run."""
    initial_capital: float = 100_000.0
    commission_pct: float = 0.001     # 10 bps
    slippage_pct: float = 0.0005      # 5 bps
    sizing: PositionSizingConfig = field(
        default_factory=PositionSizingConfig
    )


@dataclass(frozen=True)
class SimulationRequest:
    """Input to SimulationService.run(). Lives in types so CLI and
    API can both construct it without importing core."""
    code: str
    instruments: dict[str, InstrumentId | ContinuousRollConfig]
    config: SimConfig = field(default_factory=SimConfig)
    start: date | None = None
    end: date | None = None
    strategy_config: dict[str, Any] = field(default_factory=dict)
    benchmark_instruments: list[InstrumentId | ContinuousRollConfig] = field(
        default_factory=list
    )  # Extra instruments to overlay as buy-and-hold benchmarks


@dataclass(frozen=True)
class Trade:
    """Record of a single executed trade."""
    date: str                       # ISO 8601
    instrument: str                 # Leg label
    action: Literal["BUY", "SELL"]
    quantity: float                 # Absolute units traded
    price: float                    # Fill price
    cost: float                     # Transaction cost
    signal: float                   # Raw signal that triggered this


@dataclass(frozen=True)
class EquityCurve:
    """Portfolio value time series with benchmark comparison.

    Benchmarks are computed automatically:
    - leg_benchmarks: buy-and-hold of each instrument the strategy trades
    - extra_benchmarks: user-selected instruments for comparison
    Both are overlaid on the equity curve chart.
    """
    dates: tuple[str, ...]
    values: tuple[float, ...]
    leg_benchmarks: dict[str, tuple[float, ...]] = field(
        default_factory=dict
    )  # Auto: buy-and-hold of each traded leg
    extra_benchmarks: dict[str, tuple[float, ...]] = field(
        default_factory=dict
    )  # User-selected: any instrument as overlay


@dataclass(frozen=True)
class SimResult:
    """Complete output of a simulation run."""
    equity_curve: EquityCurve
    trades: tuple[Trade, ...]
    signals: dict[str, tuple[float, ...]]  # Per-leg signal history
    provenance: Provenance
    config: SimConfig
