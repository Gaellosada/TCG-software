from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import numpy as np
import numpy.typing as npt


# Single source of truth for the dwh connection-pool size, shared across the
# layer boundary: ``tcg.data._sql.connection.DwhConnectionPool`` uses it as its
# ``max_size`` default, and ``tcg.engine.options.series.stream_resolver`` derives
# its per-resolve concurrency cap from it (``max(1, N - 1)``) so the two cannot
# drift (the import-linter forbids engine<->data imports, so a shared constant
# here in the dependency-free ``tcg.types`` layer is the seam).  The resolver
# MUST keep its concurrent dwh-connection fan-out <= this, reserving one slot for
# the interleaved expirations / underlying-price lookups that share the pool.
#
# 8 (was 4): the option-stream resolver fans out one query per expiration (Phase B)
# plus underlying lookups; a larger pool lets more run concurrently, roughly halving
# the ceil(N / k) wall on a multi-year option leg.  The derived resolver cap and the
# process-wide gate (``max(1, N - 1)`` = 7) scale from this automatically.  8 is
# comfortable for the single-user desktop app against the shared RDS (well within the
# warehouse's connection budget); do NOT raise much further — it would let more heavy
# chain queries pile on the shared warehouse without proportional benefit.  Pure
# parallelism: this changes ONLY concurrency, never any computed value.
DEFAULT_DWH_POOL_MAX_SIZE: int = 8


class AssetClass(StrEnum):
    EQUITY = "equity"
    INDEX = "index"
    FUTURE = "future"


class RollStrategy(StrEnum):
    FRONT_MONTH = "front_month"
    # Roll on the last TRADING day of each contract's expiration month,
    # regardless of the contract's actual expiry (Issue #3).  Pairs naturally
    # with a cycle whose contracts live past month-end; a contract that expires
    # before its month-end roll leaves a mid-month-expiry seam (WARN, not block).
    END_OF_MONTH = "end_of_month"


class AdjustmentMethod(StrEnum):
    """Back-adjustment method for continuous futures series."""

    NONE = "none"  # Raw concatenation (prototype default)
    RATIO = "ratio"  # Ratio adjustment at roll points
    DIFFERENCE = "difference"  # Additive adjustment at roll points


@dataclass(frozen=True)
class InstrumentId:
    """Unique identifier for any tradeable instrument."""

    symbol: str
    asset_class: AssetClass
    collection: str  # MongoDB collection or logical grouping
    exchange: str | None = None


@dataclass(frozen=True)
class ContractSpec:
    """Contract specification for futures."""

    instrument_id: InstrumentId
    expiration: date | None = None
    expiration_cycle: str | None = None  # "monthly", "weekly", "quarterly"
    multiplier: float = 1.0  # e.g. 1000 for VIX futures


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

    @staticmethod
    def empty() -> PriceSeries:
        """Return a PriceSeries with zero-length arrays."""
        return PriceSeries(
            dates=np.array([], dtype=np.int64),
            open=np.array([], dtype=np.float64),
            high=np.array([], dtype=np.float64),
            low=np.array([], dtype=np.float64),
            close=np.array([], dtype=np.float64),
            volume=np.array([], dtype=np.float64),
        )


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
    roll_offset_days: int = 0


@dataclass(frozen=True)
class ContinuousLegSpec:
    """A continuous futures leg for multi-instrument alignment.

    Pairs a ``ContinuousRollConfig`` with the collection it applies to,
    since ``ContinuousRollConfig`` is a pure configuration object that
    does not carry storage location.
    """

    collection: str
    roll_config: ContinuousRollConfig


@dataclass(frozen=True)
class ContinuousSeries:
    """Stitched price series from rolling multiple contracts."""

    collection: str
    roll_config: ContinuousRollConfig
    prices: PriceSeries
    roll_dates: tuple[int, ...]  # YYYYMMDD at each roll boundary
    contracts: tuple[str, ...]  # Ordered contract IDs used
