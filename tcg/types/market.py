from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import numpy as np
import numpy.typing as npt


class AssetClass(StrEnum):
    EQUITY = "equity"
    INDEX = "index"
    FUTURE = "future"


class RollStrategy(StrEnum):
    FRONT_MONTH = "front_month"


class AdjustmentMethod(StrEnum):
    """Back-adjustment method for continuous futures series."""
    NONE = "none"                  # Raw concatenation (prototype default)
    PROPORTIONAL = "proportional"  # Ratio adjustment at roll points
    DIFFERENCE = "difference"      # Additive adjustment at roll points


@dataclass(frozen=True)
class InstrumentId:
    """Unique identifier for any tradeable instrument."""
    symbol: str
    asset_class: AssetClass
    collection: str              # MongoDB collection or logical grouping
    exchange: str | None = None


@dataclass(frozen=True)
class ContractSpec:
    """Contract specification for futures."""
    instrument_id: InstrumentId
    expiration: date | None = None
    expiration_cycle: str | None = None  # "monthly", "weekly", "quarterly"
    multiplier: float = 1.0              # e.g. 1000 for VIX futures


@dataclass(frozen=True)
class PriceSeries:
    """Columnar OHLCV data for a single instrument.

    All arrays have identical length. Dates are YYYYMMDD integers
    for fast comparison; conversion to ISO strings is a display concern.
    """
    dates: npt.NDArray[np.int64]
    open: npt.NDArray[np.float64]
    high: npt.NDArray[np.float64]
    low: npt.NDArray[np.float64]
    close: npt.NDArray[np.float64]
    volume: npt.NDArray[np.float64]

    def __len__(self) -> int:
        return len(self.dates)


@dataclass(frozen=True)
class ContractPriceData:
    """Price data for a single futures contract, used for rolling."""
    contract_id: str
    expiration: int  # YYYYMMDD integer (consistent with PriceSeries.dates)
    prices: PriceSeries


@dataclass(frozen=True)
class ContinuousRollConfig:
    """How to build a continuous futures series.

    ``cycle`` maps to the legacy ``expirationCycle`` field on Future
    documents in MongoDB (e.g., "HMUZ" for quarterly, "FGHJKMNQUVXZ"
    for monthly). Used to filter and order contracts for rolling.
    If None, all contracts in the collection are used.
    """
    strategy: RollStrategy
    adjustment: AdjustmentMethod = AdjustmentMethod.NONE
    cycle: str | None = None


@dataclass(frozen=True)
class ContinuousSeries:
    """Stitched price series from rolling multiple contracts."""
    collection: str
    roll_config: ContinuousRollConfig
    prices: PriceSeries
    roll_dates: tuple[int, ...]     # YYYYMMDD at each roll boundary
    contracts: tuple[str, ...]      # Ordered contract IDs used
