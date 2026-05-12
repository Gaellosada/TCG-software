"""Engine module -- portfolio computation and metrics.

Public API re-exported from tcg.engine.metrics.
"""

from tcg.engine.metrics import (
    aggregate_returns,
    compute_daily_returns,
    compute_equity_curve,
    compute_metrics,
    compute_weighted_portfolio,
)
from tcg.engine.statistics import compute_statistics
from tcg.types.portfolio import PortfolioComputeResult

__all__ = [
    "PortfolioComputeResult",
    "aggregate_returns",
    "compute_daily_returns",
    "compute_equity_curve",
    "compute_metrics",
    "compute_statistics",
    "compute_weighted_portfolio",
]
