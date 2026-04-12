"""Portfolio return computation, equity curves, rebalancing, and metrics.

All functions operate on NumPy arrays with YYYYMMDD integer dates.
No dependency on tcg.data or tcg.core — only tcg.types.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from tcg.types.metrics import MetricsSuite

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Daily returns
# ---------------------------------------------------------------------------


def compute_daily_returns(
    prices: npt.NDArray[np.float64],
    return_type: str,
) -> npt.NDArray[np.float64]:
    """Compute daily returns from a price series.

    Parameters
    ----------
    prices:
        Array of close prices (length N).
    return_type:
        ``"normal"`` -- ``(p[t] - p[t-1]) / p[t-1]``.
        ``"log"``    -- ``ln(p[t] / p[t-1])``.

    Returns
    -------
    NDArray
        Array of length N; first element is ``np.nan`` (no prior price).

    Raises
    ------
    ValueError
        If *return_type* is not ``"normal"`` or ``"log"``.
    """
    if return_type not in ("normal", "log"):
        raise ValueError(f"return_type must be 'normal' or 'log', got {return_type!r}")

    prices = np.asarray(prices, dtype=np.float64)
    n = len(prices)

    result = np.full(n, np.nan, dtype=np.float64)
    if n < 2:
        return result

    if return_type == "normal":
        result[1:] = (prices[1:] - prices[:-1]) / prices[:-1]
    else:  # log
        result[1:] = np.log(prices[1:] / prices[:-1])

    return result


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


def compute_equity_curve(
    returns: npt.NDArray[np.float64],
    return_type: str,
    initial_value: float = 100.0,
) -> npt.NDArray[np.float64]:
    """Build an equity curve from a daily return series.

    Parameters
    ----------
    returns:
        Array of daily returns (length N). ``returns[0]`` is ignored
        (typically NaN from :func:`compute_daily_returns`). Accumulation
        starts at index 1.
    return_type:
        ``"normal"`` -- ``value[t] = value[t-1] * (1 + r[t])``.
        ``"log"``    -- ``value[t] = initial_value * exp(cumsum(r[1:t]))``.
    initial_value:
        Starting value of the curve (default 100.0).

    Returns
    -------
    NDArray
        Array of length N. ``curve[0] = initial_value``.
        Empty input returns an empty array.
    """
    if return_type not in ("normal", "log"):
        raise ValueError(f"return_type must be 'normal' or 'log', got {return_type!r}")

    returns = np.asarray(returns, dtype=np.float64)
    n = len(returns)

    if n == 0:
        return np.array([], dtype=np.float64)

    curve = np.empty(n, dtype=np.float64)
    curve[0] = initial_value

    if n == 1:
        return curve

    if return_type == "normal":
        curve[1:] = initial_value * np.cumprod(1.0 + returns[1:])
    else:  # log
        curve[1:] = initial_value * np.exp(np.cumsum(returns[1:]))

    return curve


# ---------------------------------------------------------------------------
# Rebalance boundary detection
# ---------------------------------------------------------------------------


def _detect_rebalance_boundaries(
    dates: npt.NDArray[np.int64],
    freq: str,
) -> npt.NDArray[np.bool_]:
    """Return a boolean mask where True marks the start of a new rebalance period.

    Index 0 is always True (the initial allocation counts as a "rebalance").

    Parameters
    ----------
    dates:
        YYYYMMDD integer dates, sorted ascending.
    freq:
        One of ``"weekly"``, ``"monthly"``, ``"quarterly"``, ``"annually"``.
    """
    n = len(dates)
    boundaries = np.zeros(n, dtype=np.bool_)
    boundaries[0] = True

    if n < 2:
        return boundaries

    # Decompose dates into components
    years = dates // 10000
    months = (dates // 100) % 100

    if freq == "weekly":
        # Detect when ISO week number changes.
        # Convert YYYYMMDD integers to day-of-year offsets and compute
        # approximate week boundaries using actual weekday via numpy datetime64.
        # We need the Monday-based ISO week: boundary = when week number changes.
        dt_dates = _yyyymmdd_to_datetime64(dates)
        # numpy datetime64 weekday: Monday=0 ... Sunday=6
        weekdays = (dt_dates - np.datetime64("1970-01-05", "D")).astype(np.int64) % 7
        # ISO week number: compute Monday of each date's week, boundary when it changes
        mondays = dt_dates - weekdays.astype("timedelta64[D]")
        for i in range(1, n):
            if mondays[i] != mondays[i - 1]:
                boundaries[i] = True

    elif freq == "monthly":
        for i in range(1, n):
            if months[i] != months[i - 1] or years[i] != years[i - 1]:
                boundaries[i] = True

    elif freq == "quarterly":
        quarters = (months - 1) // 3  # 0-based quarter: 0,1,2,3
        for i in range(1, n):
            if quarters[i] != quarters[i - 1] or years[i] != years[i - 1]:
                boundaries[i] = True

    elif freq == "annually":
        for i in range(1, n):
            if years[i] != years[i - 1]:
                boundaries[i] = True

    else:
        raise ValueError(f"Unknown rebalance freq: {freq!r}")

    return boundaries


def _yyyymmdd_to_datetime64(
    dates: npt.NDArray[np.int64],
) -> npt.NDArray:
    """Convert YYYYMMDD integers to numpy datetime64[D] array."""
    years = dates // 10000
    months = (dates // 100) % 100
    days = dates % 100
    # Build ISO date strings and parse -- robust for any valid date
    strs = np.array(
        [f"{y:04d}-{m:02d}-{d:02d}" for y, m, d in zip(years, months, days)],
        dtype="datetime64[D]",
    )
    return strs


def _derive_returns_from_equity(
    equity: npt.NDArray[np.float64],
    return_type: str,
) -> npt.NDArray[np.float64]:
    """Derive daily returns from an equity curve."""
    n = len(equity)
    returns = np.full(n, np.nan, dtype=np.float64)
    if n > 1:
        if return_type == "normal":
            returns[1:] = (equity[1:] - equity[:-1]) / equity[:-1]
        else:  # log
            returns[1:] = np.log(equity[1:] / equity[:-1])
    return returns


# ---------------------------------------------------------------------------
# Weighted portfolio with rebalancing
# ---------------------------------------------------------------------------


def compute_weighted_portfolio(
    aligned_closes: dict[str, npt.NDArray[np.float64]],
    weights: dict[str, float],
    rebalance_freq: str,
    return_type: str,
    dates: npt.NDArray[np.int64],
) -> tuple[
    npt.NDArray[np.float64],
    dict[str, npt.NDArray[np.float64]],
    npt.NDArray[np.float64],
    dict[str, npt.NDArray[np.float64]],
    dict[str, npt.NDArray[np.float64]],
    list[int],
]:
    """Compute a weighted portfolio with rebalancing.

    Parameters
    ----------
    aligned_closes:
        ``{label: close_prices}`` -- all arrays must have the same length,
        aligned to the same date grid.
    weights:
        ``{label: weight}`` -- normalized by sum of absolute values internally.
        Negative weights represent short positions.
    rebalance_freq:
        One of ``"none"``, ``"daily"``, ``"weekly"``, ``"monthly"``,
        ``"quarterly"``, ``"annually"``.
    return_type:
        ``"normal"`` or ``"log"``.
    dates:
        YYYYMMDD integer dates (same length as price arrays).

    Returns
    -------
    tuple of:
        - portfolio_returns: daily returns of the combined portfolio (length N, [0]=NaN)
        - per_leg_returns: ``{label: daily_returns}`` for each leg
        - portfolio_equity: equity curve of the combined portfolio (length N)
        - per_leg_equities: ``{label: equity_curve}`` for each leg
        - raw_leg_equities: ``{label: equity_curve}`` buy-and-hold leg equities
          (same as per_leg_equities when rebalance_freq is ``"none"``)
        - rebalance_dates: YYYYMMDD integers where rebalancing occurred (empty
          when rebalance_freq is ``"none"`` or ``"daily"``)

    Raises
    ------
    ValueError
        If inputs are invalid (empty, mismatched lengths, all-zero weights, etc.).
    """
    # ── Validation ──
    if not aligned_closes:
        raise ValueError("aligned_closes must not be empty")

    missing = set(aligned_closes.keys()) - set(weights.keys())
    if missing:
        raise ValueError(f"Weights missing for legs: {missing}")

    labels = list(aligned_closes.keys())
    n = len(next(iter(aligned_closes.values())))
    if n == 0:
        raise ValueError("Price arrays must not be empty")

    for lbl in labels:
        if len(aligned_closes[lbl]) != n:
            raise ValueError(
                f"Length mismatch: expected {n}, got {len(aligned_closes[lbl])} for '{lbl}'"
            )

    if len(dates) != n:
        raise ValueError(f"dates length {len(dates)} != prices length {n}")

    abs_total = sum(abs(weights[lbl]) for lbl in labels)
    if abs_total == 0.0:
        raise ValueError("All weights are zero -- cannot compute portfolio")

    # Normalize weights by sum of |w|
    norm_weights = {lbl: weights[lbl] / abs_total for lbl in labels}

    if return_type not in ("normal", "log"):
        raise ValueError(f"return_type must be 'normal' or 'log', got {return_type!r}")

    # ── Per-leg daily returns ──
    per_leg_returns: dict[str, npt.NDArray[np.float64]] = {
        lbl: compute_daily_returns(aligned_closes[lbl], return_type) for lbl in labels
    }

    # ── Dispatch by rebalance frequency ──
    if rebalance_freq == "daily":
        portfolio_returns, portfolio_equity, per_leg_equities, rebalance_dates = (
            _compute_daily_rebalance(
                per_leg_returns, norm_weights, return_type, n, labels,
            )
        )
    elif rebalance_freq == "none":
        portfolio_returns, portfolio_equity, per_leg_equities, rebalance_dates = (
            _compute_buy_and_hold(
                per_leg_returns, norm_weights, return_type, n, labels,
            )
        )
    else:
        # Periodic rebalancing: weekly, monthly, quarterly, annually
        portfolio_returns, portfolio_equity, per_leg_equities, rebalance_dates = (
            _compute_periodic_rebalance(
                per_leg_returns, norm_weights, return_type, n, labels, dates, rebalance_freq,
            )
        )

    # ── Raw (buy-and-hold) leg equities for normalized comparison ──
    if rebalance_freq == "none":
        raw_leg_equities = per_leg_equities
    else:
        _, _, raw_leg_equities, _ = _compute_buy_and_hold(
            per_leg_returns, norm_weights, return_type, n, labels,
        )

    return (
        portfolio_returns,
        per_leg_returns,
        portfolio_equity,
        per_leg_equities,
        raw_leg_equities,
        rebalance_dates,
    )


def _compute_daily_rebalance(
    per_leg_returns: dict[str, npt.NDArray[np.float64]],
    norm_weights: dict[str, float],
    return_type: str,
    n: int,
    labels: list[str],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], dict[str, npt.NDArray[np.float64]], list[int]]:
    """Daily rebalancing = fixed-weight returns each day.

    Portfolio return is the weighted sum of individual leg returns.
    Each leg's equity reflects its fixed weight of the total portfolio.
    """
    portfolio_returns = np.full(n, np.nan, dtype=np.float64)

    for lbl in labels:
        w = norm_weights[lbl]
        leg_ret = per_leg_returns[lbl]
        # Skip index 0 (NaN)
        if return_type == "normal":
            portfolio_returns[1:] = np.where(
                np.isnan(portfolio_returns[1:]),
                w * leg_ret[1:],
                portfolio_returns[1:] + w * leg_ret[1:],
            )
        else:  # log -- weighted sum of log returns is NOT strictly correct for
               # portfolio-level log returns, but is the standard approximation
            portfolio_returns[1:] = np.where(
                np.isnan(portfolio_returns[1:]),
                w * leg_ret[1:],
                portfolio_returns[1:] + w * leg_ret[1:],
            )

    portfolio_equity = compute_equity_curve(portfolio_returns, return_type, initial_value=100.0)

    # Per-leg equities: each leg gets its weight fraction of the portfolio at all times
    per_leg_equities: dict[str, npt.NDArray[np.float64]] = {}
    for lbl in labels:
        per_leg_equities[lbl] = portfolio_equity * abs(norm_weights[lbl])

    return portfolio_returns, portfolio_equity, per_leg_equities, []


def _compute_buy_and_hold(
    per_leg_returns: dict[str, npt.NDArray[np.float64]],
    norm_weights: dict[str, float],
    return_type: str,
    n: int,
    labels: list[str],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], dict[str, npt.NDArray[np.float64]], list[int]]:
    """Buy-and-hold: legs drift independently from initial allocation.

    Each leg starts at (weight * 100) and grows with its own returns.
    Portfolio equity = sum of all leg equities.
    Portfolio returns are derived from the portfolio equity curve.
    """
    initial_total = 100.0

    per_leg_equities: dict[str, npt.NDArray[np.float64]] = {}
    for lbl in labels:
        w = norm_weights[lbl]
        leg_initial = abs(w) * initial_total
        leg_ret = per_leg_returns[lbl]
        leg_equity = compute_equity_curve(leg_ret, return_type, initial_value=leg_initial)

        # For short legs (negative weight), returns are inverted:
        # When the underlying goes up, the short position loses value.
        if w < 0:
            # Short leg: value = initial - (equity_if_long - initial) = 2*initial - equity_long
            leg_equity_long = leg_equity.copy()
            leg_equity = 2.0 * leg_initial - leg_equity_long

        per_leg_equities[lbl] = leg_equity

    # Portfolio equity = sum of leg equities
    portfolio_equity = np.zeros(n, dtype=np.float64)
    for lbl in labels:
        portfolio_equity += per_leg_equities[lbl]

    # Derive portfolio returns from equity curve
    portfolio_returns = _derive_returns_from_equity(portfolio_equity, return_type)

    return portfolio_returns, portfolio_equity, per_leg_equities, []


def _compute_periodic_rebalance(
    per_leg_returns: dict[str, npt.NDArray[np.float64]],
    norm_weights: dict[str, float],
    return_type: str,
    n: int,
    labels: list[str],
    dates: npt.NDArray[np.int64],
    rebalance_freq: str,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], dict[str, npt.NDArray[np.float64]], list[int]]:
    """Periodic rebalancing: within each period, legs drift independently.

    At each rebalance boundary, total portfolio value is redistributed
    according to target weights.
    """
    boundaries = _detect_rebalance_boundaries(dates, rebalance_freq)

    # Collect rebalance dates (exclude index 0 — that's the initial allocation)
    rebalance_dates: list[int] = [
        int(dates[i]) for i in range(1, n) if boundaries[i]
    ]

    initial_total = 100.0
    portfolio_equity = np.empty(n, dtype=np.float64)
    per_leg_equities: dict[str, npt.NDArray[np.float64]] = {
        lbl: np.empty(n, dtype=np.float64) for lbl in labels
    }

    # Track current allocation for each leg
    leg_values: dict[str, float] = {}
    for lbl in labels:
        w = norm_weights[lbl]
        leg_values[lbl] = abs(w) * initial_total

    # Set initial values
    portfolio_equity[0] = initial_total
    for lbl in labels:
        per_leg_equities[lbl][0] = leg_values[lbl]

    for i in range(1, n):
        # At a rebalance boundary, redistribute according to target weights
        if boundaries[i]:
            total_value = sum(leg_values.values())
            for lbl in labels:
                w = norm_weights[lbl]
                leg_values[lbl] = abs(w) * total_value

        # Each leg grows by its own return for this day
        for lbl in labels:
            r = per_leg_returns[lbl][i]
            if np.isnan(r):
                # No return data -- hold value flat
                pass
            else:
                w = norm_weights[lbl]
                if w >= 0:
                    if return_type == "normal":
                        leg_values[lbl] *= (1.0 + r)
                    else:  # log
                        leg_values[lbl] *= np.exp(r)
                else:
                    # Short position: inverted returns
                    if return_type == "normal":
                        leg_values[lbl] *= (1.0 - r)
                    else:  # log
                        leg_values[lbl] *= np.exp(-r)

            per_leg_equities[lbl][i] = leg_values[lbl]

        portfolio_equity[i] = sum(leg_values.values())

    # Derive portfolio returns from equity curve
    portfolio_returns = _derive_returns_from_equity(portfolio_equity, return_type)

    return portfolio_returns, portfolio_equity, per_leg_equities, rebalance_dates


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_metrics(
    equity_values: npt.NDArray[np.float64],
    risk_free_rate: float = 0.0,
) -> MetricsSuite:
    """Compute performance metrics from an equity curve.

    Parameters
    ----------
    equity_values:
        Portfolio value at each bar (length N).
    risk_free_rate:
        Annualized risk-free rate (e.g. 0.05 for 5%).

    Returns
    -------
    MetricsSuite
        Frozen dataclass with all computed metrics.
        ``num_trades=0`` and ``win_rate=None`` (portfolios have no trades).
    """
    equity = np.asarray(equity_values, dtype=np.float64)
    n = len(equity)

    if n < 2:
        return _empty_metrics()

    # ── Daily returns ──
    daily_returns = np.diff(equity) / equity[:-1]
    daily_returns = np.nan_to_num(daily_returns, nan=0.0, posinf=0.0, neginf=0.0)

    # ── Total return ──
    if equity[0] != 0:
        total_return = float((equity[-1] - equity[0]) / equity[0])
    else:
        total_return = 0.0

    # ── Annualized return (CAGR) ──
    n_days = n - 1
    if n_days > 0 and equity[0] > 0 and equity[-1] > 0:
        annualized_return = float((equity[-1] / equity[0]) ** (252.0 / n_days) - 1.0)
    else:
        annualized_return = 0.0

    # ── Daily excess returns over risk-free ──
    daily_rf = (1.0 + risk_free_rate) ** (1.0 / 252.0) - 1.0
    excess = daily_returns - daily_rf

    # ── Annualized volatility ──
    if len(daily_returns) > 1:
        annualized_volatility = float(np.std(daily_returns, ddof=1) * np.sqrt(252.0))
    else:
        annualized_volatility = 0.0

    # ── Sharpe ratio (annualized) ──
    std_excess = float(np.std(excess, ddof=1)) if len(excess) > 1 else 0.0
    if std_excess > 0:
        sharpe_ratio = float(np.mean(excess) / std_excess * np.sqrt(252.0))
    else:
        sharpe_ratio = 0.0

    # ── Sortino ratio ──
    # Downside deviation: std of negative excess returns only
    downside = excess[excess < 0]
    if len(downside) > 0:
        downside_std = float(np.sqrt(np.mean(downside ** 2)))
        annualized_downside = downside_std * np.sqrt(252.0)
        if annualized_downside > 0:
            sortino_ratio = float(
                (np.mean(excess) * 252.0) / annualized_downside
            )
        else:
            sortino_ratio = 0.0
    else:
        sortino_ratio = 0.0

    # ── Max drawdown (negative by convention) ──
    cummax = np.maximum.accumulate(equity)
    drawdown = (equity - cummax) / np.where(cummax > 0, cummax, 1.0)
    max_drawdown = float(np.min(drawdown))

    # ── Calmar ratio ──
    abs_dd = abs(max_drawdown)
    calmar_ratio = annualized_return / abs_dd if abs_dd > 0 else 0.0

    # ── CVaR 5% (expected shortfall) ──
    sorted_returns = np.sort(daily_returns)
    cutoff = max(1, int(np.ceil(len(sorted_returns) * 0.05)))
    cvar_5 = float(np.mean(sorted_returns[:cutoff]))

    # ── Time underwater (days in drawdown) ──
    time_underwater_days = int(np.sum(drawdown < 0))

    return MetricsSuite(
        total_return=total_return,
        annualized_return=annualized_return,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        calmar_ratio=calmar_ratio,
        cvar_5=cvar_5,
        time_underwater_days=time_underwater_days,
        annualized_volatility=annualized_volatility,
        sortino_ratio=sortino_ratio,
        num_trades=0,
        win_rate=None,
    )


def _empty_metrics() -> MetricsSuite:
    """Return zeroed metrics when data is insufficient."""
    return MetricsSuite(
        total_return=0.0,
        annualized_return=0.0,
        sharpe_ratio=0.0,
        max_drawdown=0.0,
        calmar_ratio=0.0,
        cvar_5=0.0,
        time_underwater_days=0,
        annualized_volatility=0.0,
        sortino_ratio=0.0,
        num_trades=0,
        win_rate=None,
    )


# ---------------------------------------------------------------------------
# Return aggregation
# ---------------------------------------------------------------------------


def aggregate_returns(
    dates: npt.NDArray[np.int64],
    returns: npt.NDArray[np.float64],
    per_leg_returns: dict[str, npt.NDArray[np.float64]],
    return_type: str,
    granularity: str,
) -> list[dict]:
    """Aggregate daily returns into monthly or yearly buckets.

    Parameters
    ----------
    dates:
        YYYYMMDD integer dates (same length as *returns*).
    returns:
        Portfolio daily returns (first element may be NaN).
    per_leg_returns:
        ``{label: daily_returns}`` for each leg.
    return_type:
        ``"normal"`` -- compound ``(1+r1)*(1+r2)*...-1``.
        ``"log"``    -- sum of daily log returns.
    granularity:
        ``"monthly"`` or ``"yearly"``.

    Returns
    -------
    list[dict]
        Each dict: ``{period: str, portfolio: float, <leg_label>: float, ...}``.
        Sorted chronologically. Periods with all-NaN data are omitted.
    """
    if granularity not in ("monthly", "yearly"):
        raise ValueError(f"granularity must be 'monthly' or 'yearly', got {granularity!r}")

    dates_arr = np.asarray(dates, dtype=np.int64)
    ret_arr = np.asarray(returns, dtype=np.float64)

    agg_fn = _compound if return_type == "normal" else _sum_returns

    # Build period -> indices mapping (preserve insertion order = chronological)
    period_indices: dict[str, list[int]] = {}
    for i, d in enumerate(dates_arr):
        key = _period_key(int(d), granularity)
        period_indices.setdefault(key, []).append(i)

    result: list[dict] = []
    for period, indices in period_indices.items():
        idx = np.array(indices, dtype=np.int64)
        portfolio_val = agg_fn(ret_arr[idx])
        if np.isnan(portfolio_val):
            continue
        row: dict = {"period": period, "portfolio": portfolio_val}
        for lbl, leg_ret in per_leg_returns.items():
            leg_arr = np.asarray(leg_ret, dtype=np.float64)
            row[lbl] = agg_fn(leg_arr[idx])
        result.append(row)

    result.sort(key=lambda r: str(r["period"]))
    return result


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _period_key(date_int: int, granularity: str) -> str:
    """Convert YYYYMMDD integer to period string."""
    s = str(date_int)
    year = s[:4]
    if granularity == "monthly":
        return f"{year}-{s[4:6]}"
    return year  # yearly


def _compound(values: npt.NDArray[np.float64]) -> float:
    """Compound a series of returns: (1+r1)*(1+r2)*...-1, skipping NaN."""
    v = values[~np.isnan(values)]
    if len(v) == 0:
        return float("nan")
    return float(np.prod(1.0 + v) - 1.0)


def _sum_returns(values: npt.NDArray[np.float64]) -> float:
    """Sum a series of returns, skipping NaN (for log returns)."""
    v = values[~np.isnan(values)]
    if len(v) == 0:
        return float("nan")
    return float(np.sum(v))
