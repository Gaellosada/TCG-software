from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricsSuite:
    """Core performance metrics for any equity curve.

    Minimum metric set per advice doc section 9.
    """
    total_return: float              # As decimal (0.2 = 20%)
    annualized_return: float         # Assumes 252 trading days
    sharpe_ratio: float              # Annualized
    max_drawdown: float              # Negative (e.g. -0.56 = 56% loss)
    calmar_ratio: float              # annualized_return / abs(max_drawdown)
    cvar_5: float                    # Conditional VaR at 5% (expected shortfall)
    time_underwater_days: int        # Number of bars in drawdown
    annualized_volatility: float     # Annualized std dev of daily returns
    sortino_ratio: float             # Like Sharpe but only penalizes downside deviation
    num_trades: int
    win_rate: float | None = None    # Fraction of profitable trades (0.0-1.0)
