from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import numpy.typing as npt

from tcg.types.metrics import MetricsSuite
from tcg.types.provenance import Provenance


class RebalanceFreq(StrEnum):
    NONE = "none"              # Buy-and-hold (drift with prices)
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUALLY = "annually"


@dataclass(frozen=True)
class PortfolioSpec:
    """Definition of a static weighted portfolio.

    weights are allocation fractions. They do NOT need to sum to 1 --
    the computation normalizes internally. Negative weights = short.
    """
    name: str
    legs: dict[str, str]            # label -> instrument description
    weights: dict[str, float]       # label -> allocation weight
    rebalance: RebalanceFreq = RebalanceFreq.NONE


@dataclass(frozen=True)
class PortfolioResult:
    """Output of a weighted portfolio computation."""
    dates: tuple[str, ...]
    portfolio_equity: tuple[float, ...]
    leg_equities: dict[str, tuple[float, ...]]   # Per-leg equity curves
    portfolio_returns: npt.NDArray[np.float64]    # Daily returns
    leg_returns: dict[str, npt.NDArray[np.float64]]
    metrics: MetricsSuite | None = None
    provenance: Provenance | None = None
