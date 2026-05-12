"""StatisticsSuite — nested performance statistics for any equity curve.

Distinct from :class:`tcg.types.metrics.MetricsSuite` which is shaped for
the portfolio/trade engine: this suite is purely equity-curve driven (no
trade-based fields) and is consumed by the ``POST /api/statistics``
endpoint and the reusable Statistics React component.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReturnStats:
    total_return: float
    cagr: float
    annualized_volatility: float
    best_day: float
    worst_day: float
    best_month: float | None
    worst_month: float | None


@dataclass(frozen=True)
class RiskAdjustedStats:
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float


@dataclass(frozen=True)
class TailStats:
    var_95: float
    var_99: float
    cvar_5: float
    skewness: float | None
    kurtosis: float | None


@dataclass(frozen=True)
class DrawdownStats:
    max_drawdown: float
    avg_drawdown: float
    current_drawdown: float
    longest_drawdown_days: int
    time_underwater_days: int


@dataclass(frozen=True)
class StatisticsSuite:
    return_: ReturnStats
    risk_adjusted: RiskAdjustedStats
    tail: TailStats
    drawdown: DrawdownStats
    risk_free_rate_used: float
    num_observations: int
