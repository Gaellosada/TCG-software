"""Equity-curve statistics for the Statistics component.

Computes the full :class:`StatisticsSuite` (return / risk-adjusted / tail
/ drawdown) from a YYYYMMDD-dated equity curve. Parallel to
:func:`tcg.engine.metrics.compute_metrics` — kept separate because the
shape differs (nested suite, no trade-based fields) and because callers
pass equity directly rather than fetching it from a portfolio compute
result.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from tcg.types.statistics import (
    DrawdownStats,
    ReturnStats,
    RiskAdjustedStats,
    StatisticsSuite,
    TailStats,
)

_TRADING_DAYS_PER_YEAR = 252
# Minimum sample size below which skew / kurtosis are statistically
# unreliable. The brief specifies < 30 → null.
_MIN_OBS_FOR_HIGHER_MOMENTS = 30


def compute_statistics(
    dates: npt.NDArray[np.int64],
    equity: npt.NDArray[np.float64],
    risk_free_rate: float = 0.04,
) -> StatisticsSuite:
    """Compute the full StatisticsSuite from an equity curve.

    Parameters
    ----------
    dates:
        YYYYMMDD integer dates, length N, sorted ascending. Used only
        for monthly bucketing of best/worst month.
    equity:
        Equity values, length N. Must have N >= 2 and all values > 0.
    risk_free_rate:
        Annualized risk-free rate as a decimal (0.04 = 4%).

    Raises
    ------
    ValueError
        If lengths mismatch, N < 2, or any equity value is non-positive.
    """
    equity = np.asarray(equity, dtype=np.float64)
    dates = np.asarray(dates, dtype=np.int64)

    n = len(equity)
    if len(dates) != n:
        raise ValueError(
            f"dates length {len(dates)} != equity length {n}"
        )
    if n < 2:
        raise ValueError("equity must have at least 2 observations")
    if not np.all(equity > 0):
        raise ValueError("equity values must all be strictly positive")

    daily_returns = np.diff(equity) / equity[:-1]
    n_returns = len(daily_returns)

    return_stats = _compute_return_stats(dates, equity, daily_returns, risk_free_rate)
    drawdown_stats = _compute_drawdown_stats(equity)
    risk_stats = _compute_risk_adjusted_stats(
        daily_returns,
        return_stats.cagr,
        drawdown_stats.max_drawdown,
        risk_free_rate,
    )
    tail_stats = _compute_tail_stats(daily_returns)

    return StatisticsSuite(
        return_=return_stats,
        risk_adjusted=risk_stats,
        tail=tail_stats,
        drawdown=drawdown_stats,
        risk_free_rate_used=float(risk_free_rate),
        num_observations=n_returns,
    )


def _compute_return_stats(
    dates: npt.NDArray[np.int64],
    equity: npt.NDArray[np.float64],
    daily_returns: npt.NDArray[np.float64],
    risk_free_rate: float,
) -> ReturnStats:
    n = len(equity)
    total_return = float(equity[-1] / equity[0] - 1.0)

    n_days = n - 1
    cagr = float(
        (equity[-1] / equity[0]) ** (_TRADING_DAYS_PER_YEAR / n_days) - 1.0
    )

    excess_return = cagr - float(risk_free_rate)

    if len(daily_returns) > 1:
        annualized_volatility = float(
            np.std(daily_returns, ddof=1) * np.sqrt(_TRADING_DAYS_PER_YEAR)
        )
    else:
        annualized_volatility = 0.0

    best_day = float(np.max(daily_returns))
    worst_day = float(np.min(daily_returns))

    best_month, worst_month = _best_worst_month(dates, equity)

    return ReturnStats(
        total_return=total_return,
        cagr=cagr,
        excess_return=excess_return,
        annualized_volatility=annualized_volatility,
        best_day=best_day,
        worst_day=worst_day,
        best_month=best_month,
        worst_month=worst_month,
    )


def _best_worst_month(
    dates: npt.NDArray[np.int64],
    equity: npt.NDArray[np.float64],
) -> tuple[float | None, float | None]:
    """Bucket the equity curve by calendar month and return (best, worst)
    compounded monthly returns.

    Partial first / last months are included — each bucket's return is
    ``equity[last_in_bucket] / equity[first_in_bucket_or_prior_close] - 1``.
    The "prior close" is the last equity value before the bucket starts;
    for the very first bucket we use ``equity[0]`` itself, so the first
    month's return represents the within-month change starting from the
    initial equity.
    """
    months = (dates // 100).astype(np.int64)
    n = len(equity)

    boundaries: list[int] = [0]
    for i in range(1, n):
        if months[i] != months[i - 1]:
            boundaries.append(i)
    boundaries.append(n)  # sentinel

    if len(boundaries) <= 1:
        return None, None

    monthly_returns: list[float] = []
    for k in range(len(boundaries) - 1):
        bucket_start = boundaries[k]
        bucket_end = boundaries[k + 1] - 1  # inclusive
        # Anchor: equity just before the bucket begins (or equity[0] for k=0).
        anchor_idx = bucket_start - 1 if bucket_start > 0 else 0
        anchor = equity[anchor_idx]
        end_val = equity[bucket_end]
        if anchor > 0:
            monthly_returns.append(float(end_val / anchor - 1.0))

    if not monthly_returns:
        return None, None
    return max(monthly_returns), min(monthly_returns)


def _compute_drawdown_stats(
    equity: npt.NDArray[np.float64],
) -> DrawdownStats:
    cummax = np.maximum.accumulate(equity)
    drawdown = equity / cummax - 1.0  # always <= 0

    max_drawdown = float(np.min(drawdown))

    # Average drawdown: mean of drawdown values that are strictly negative.
    # Defined as 0.0 when the curve never goes underwater.
    underwater_mask = drawdown < 0
    if underwater_mask.any():
        avg_drawdown = float(np.mean(drawdown[underwater_mask]))
    else:
        avg_drawdown = 0.0

    current_drawdown = float(drawdown[-1])

    # Longest drawdown duration: length of the longest contiguous run
    # of underwater bars.
    longest = 0
    current_run = 0
    for is_under in underwater_mask:
        if is_under:
            current_run += 1
            if current_run > longest:
                longest = current_run
        else:
            current_run = 0
    longest_drawdown_days = int(longest)

    time_underwater_days = int(np.sum(underwater_mask))

    return DrawdownStats(
        max_drawdown=max_drawdown,
        avg_drawdown=avg_drawdown,
        current_drawdown=current_drawdown,
        longest_drawdown_days=longest_drawdown_days,
        time_underwater_days=time_underwater_days,
    )


def _compute_risk_adjusted_stats(
    daily_returns: npt.NDArray[np.float64],
    cagr: float,
    max_drawdown: float,
    risk_free_rate: float,
) -> RiskAdjustedStats:
    daily_rf = (1.0 + risk_free_rate) ** (1.0 / _TRADING_DAYS_PER_YEAR) - 1.0
    excess = daily_returns - daily_rf

    if len(excess) > 1:
        std_excess = float(np.std(excess, ddof=1))
    else:
        std_excess = 0.0
    if std_excess > 0:
        sharpe_ratio = float(
            np.mean(excess) / std_excess * np.sqrt(_TRADING_DAYS_PER_YEAR)
        )
    else:
        sharpe_ratio = 0.0

    # Sortino: target semi-deviation against the Rf baseline. RMS of
    # negative excess returns, divided by the full sample count (Sortino
    # & Price 1994) — same convention as compute_metrics.
    downside = excess[excess < 0]
    if len(excess) > 0 and len(downside) > 0:
        downside_std = float(np.sqrt(np.sum(downside ** 2) / len(excess)))
        annualized_downside = downside_std * np.sqrt(_TRADING_DAYS_PER_YEAR)
        if annualized_downside > 0:
            sortino_ratio = float(
                (np.mean(excess) * _TRADING_DAYS_PER_YEAR) / annualized_downside
            )
        else:
            sortino_ratio = 0.0
    else:
        sortino_ratio = 0.0

    abs_dd = abs(max_drawdown)
    if abs_dd > 0:
        calmar_ratio = float((cagr - risk_free_rate) / abs_dd)
    else:
        calmar_ratio = 0.0

    return RiskAdjustedStats(
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        calmar_ratio=calmar_ratio,
    )


def _compute_tail_stats(
    daily_returns: npt.NDArray[np.float64],
) -> TailStats:
    var_95 = float(np.quantile(daily_returns, 0.05))
    var_99 = float(np.quantile(daily_returns, 0.01))

    tail_mask = daily_returns <= var_95
    if tail_mask.any():
        cvar_5 = float(np.mean(daily_returns[tail_mask]))
    else:
        cvar_5 = float(var_95)

    n_returns = len(daily_returns)
    if n_returns >= _MIN_OBS_FOR_HIGHER_MOMENTS:
        skewness: float | None = _sample_skewness(daily_returns)
        kurtosis: float | None = _sample_excess_kurtosis(daily_returns)
    else:
        skewness = None
        kurtosis = None

    return TailStats(
        var_95=var_95,
        var_99=var_99,
        cvar_5=cvar_5,
        skewness=skewness,
        kurtosis=kurtosis,
    )


def _sample_skewness(x: npt.NDArray[np.float64]) -> float | None:
    """Adjusted Fisher–Pearson sample skewness (matches scipy default).

    Returns None when standard deviation is zero (constant series).
    """
    n = len(x)
    mean = float(np.mean(x))
    centered = x - mean
    m2 = float(np.mean(centered ** 2))
    if m2 == 0.0:
        return None
    m3 = float(np.mean(centered ** 3))
    g1 = m3 / (m2 ** 1.5)
    # Adjusted (bias-corrected) skewness — same form scipy.stats.skew uses
    # with bias=False.
    adj = np.sqrt(n * (n - 1)) / (n - 2) if n > 2 else 1.0
    return float(adj * g1)


def _sample_excess_kurtosis(x: npt.NDArray[np.float64]) -> float | None:
    """Sample EXCESS kurtosis (kurtosis - 3), bias-adjusted, matching
    scipy.stats.kurtosis(bias=False, fisher=True).

    Returns None when standard deviation is zero.
    """
    n = len(x)
    mean = float(np.mean(x))
    centered = x - mean
    m2 = float(np.mean(centered ** 2))
    if m2 == 0.0:
        return None
    m4 = float(np.mean(centered ** 4))
    g2 = m4 / (m2 ** 2) - 3.0
    if n > 3:
        adj = ((n - 1) / ((n - 2) * (n - 3))) * ((n + 1) * g2 + 6)
        return float(adj)
    return float(g2)
