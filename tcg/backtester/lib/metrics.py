"""MetricsSuite (frontend-aligned) + monthly / yearly tables + drawdown periods.

Public API (mirrored in ``__all__`` so a single ``dir(metrics)`` shows the
full exported surface — avoids the cold-start failure mode of skimming the
file and missing a function defined further down):

- :class:`MetricsSuite` — the canonical metrics dataclass
- :func:`compute_metrics` — full suite from a :class:`BacktestResult`
- :func:`monthly_returns_table`, :func:`yearly_returns_table` — period tables
- :func:`aggregate_returns` — dispatcher over period={'M', 'Y'}; accepts
  either a ``BacktestResult`` or ``(equity, dates)`` directly
- :func:`drawdown_periods` — drawdown segmentation
- :func:`buy_and_hold_curve`, :func:`risk_free_curve`, :func:`compare_stats`
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from .engine import BacktestResult, Trade

__all__ = [
    "MetricsSuite",
    "compute_metrics",
    "monthly_returns_table",
    "yearly_returns_table",
    "aggregate_returns",
    "drawdown_periods",
    "buy_and_hold_curve",
    "risk_free_curve",
    "compare_stats",
]


@dataclass(frozen=True)
class MetricsSuite:
    total_return: float
    annualized_return: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    calmar_ratio: float
    cvar_5: float
    time_underwater_days: int
    annualized_volatility: float
    num_trades: int
    win_rate: float | None

    def to_dict(self) -> dict:
        """Return the suite as a plain dict for JSON serialization."""
        return {
            "total_return": float(self.total_return),
            "annualized_return": float(self.annualized_return),
            "sharpe_ratio": float(self.sharpe_ratio),
            "sortino_ratio": float(self.sortino_ratio),
            "max_drawdown": float(self.max_drawdown),
            "calmar_ratio": float(self.calmar_ratio),
            "cvar_5": float(self.cvar_5),
            "time_underwater_days": int(self.time_underwater_days),
            "annualized_volatility": float(self.annualized_volatility),
            "num_trades": int(self.num_trades),
            "win_rate": (None if self.win_rate is None else float(self.win_rate)),
        }


def _zero_metrics(n_trades: int = 0) -> MetricsSuite:
    return MetricsSuite(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, n_trades, None)


def _bar_returns(equity: NDArray[np.float64], return_type: str) -> NDArray[np.float64]:
    if equity.shape[0] < 2:
        return np.zeros(0, dtype=np.float64)
    prev = equity[:-1]
    safe = np.where(prev == 0.0, np.nan, prev)
    if return_type == "log":
        with np.errstate(invalid="ignore", divide="ignore"):
            r = np.log(equity[1:] / safe)
    else:
        r = (equity[1:] - prev) / safe
    return np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)


def _date_to_dt(d: int) -> date:
    n = int(d)
    return date(n // 10000, (n // 100) % 100, n % 100)


def compute_metrics(
    equity_or_result,
    dates: NDArray[np.int64] | None = None,
    trades: list[Trade] | None = None,
    *,
    return_type: Literal["normal", "log"] = "normal",
    trading_days: int | None = None,
    risk_free_rate: float | None = None,
) -> MetricsSuite:
    """Compute MetricsSuite. Accepts either (equity, dates, trades=...) or (BacktestResult).

    `risk_free_rate` matches `execution.risk_free_rate` in `META["execution"]`. When
    `equity_or_result` is a `BacktestResult` and the kwarg is not passed, it's
    auto-threaded from `result.meta['spec']['risk_free_rate']`.
    """
    from .constants import TRADING_DAYS_PER_YEAR
    trading_days = TRADING_DAYS_PER_YEAR if trading_days is None else int(trading_days)
    rf = 0.0 if risk_free_rate is None else float(risk_free_rate)
    if isinstance(equity_or_result, BacktestResult):
        result = equity_or_result
        equity = np.asarray(result.equity_curve, dtype=np.float64)
        dates = np.asarray(result.dates, dtype=np.int64)
        trades = list(result.trades)
        meta_spec = result.meta.get("spec", {}) if isinstance(result.meta, dict) else {}
        return_type = meta_spec.get("return_type", return_type) or return_type
        if risk_free_rate is None and isinstance(meta_spec, dict) and "risk_free_rate" in meta_spec:
            try:
                rf = float(meta_spec["risk_free_rate"])
            except (TypeError, ValueError):
                pass
    else:
        equity = np.asarray(equity_or_result, dtype=np.float64)
        if dates is None:
            raise ValueError("dates is required when first argument is not BacktestResult")
        dates = np.asarray(dates, dtype=np.int64)
    if equity.shape[0] != dates.shape[0]:
        raise ValueError("equity / dates length mismatch")
    n_trades = len(trades) if trades else 0
    if equity.shape[0] < 2:
        return _zero_metrics(n_trades)

    rets = _bar_returns(equity, return_type)
    rf_per_bar = rf / trading_days
    excess = rets - rf_per_bar

    total_return = float(equity[-1] / equity[0] - 1.0) if equity[0] != 0 else 0.0
    n_bars = max(equity.shape[0] - 1, 1)
    if equity[0] > 0 and equity[-1] > 0:
        ann_return = float((equity[-1] / equity[0]) ** (trading_days / n_bars) - 1.0)
    else:
        ann_return = 0.0

    std = float(np.std(rets, ddof=1)) if rets.shape[0] > 1 else 0.0
    ann_vol = std * (trading_days ** 0.5)
    sharpe = (float(np.mean(excess)) / std * (trading_days ** 0.5)) if std > 0 else 0.0

    downside = excess[excess < 0]
    if downside.size > 0:
        d_std = float(np.sqrt(np.mean(downside ** 2)))
        sortino = (float(np.mean(excess)) / d_std * (trading_days ** 0.5)) if d_std > 0 else 0.0
    else:
        # No downside observations -> clip to 1e9 per spec convention.
        sortino = 1e9 if float(np.mean(excess)) > 0 else 0.0

    running_max = np.maximum.accumulate(equity)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(running_max > 0, equity / running_max - 1.0, 0.0)
    max_dd = float(np.min(dd)) if dd.size > 0 else 0.0
    calmar = (ann_return / abs(max_dd)) if max_dd < 0 else 0.0

    if rets.shape[0] >= 20:
        k = max(1, int(0.05 * rets.shape[0]))
        worst = np.sort(rets)[:k]
        cvar_5 = float(np.mean(worst))
    else:
        cvar_5 = 0.0

    # Time underwater: count consecutive bars with dd<0; multiply by avg calendar days/bar.
    underwater_streak = 0
    longest = 0
    for v in dd:
        if v < 0:
            underwater_streak += 1
            longest = max(longest, underwater_streak)
        else:
            underwater_streak = 0
    if dates.shape[0] > 1:
        span_days = (_date_to_dt(int(dates[-1])) - _date_to_dt(int(dates[0]))).days
        avg_cal = span_days / max(dates.shape[0] - 1, 1)
    else:
        avg_cal = 1.0
    time_uw = int(round(longest * avg_cal))

    win_rate: float | None = None
    if trades:
        closed = [t for t in trades if t.pnl != 0.0]
        if closed:
            wins = sum(1 for t in closed if t.pnl > 0)
            win_rate = wins / len(closed)

    return MetricsSuite(
        total_return=float(total_return),
        annualized_return=float(ann_return),
        sharpe_ratio=float(sharpe),
        sortino_ratio=float(sortino),
        max_drawdown=float(max_dd),
        calmar_ratio=float(calmar),
        cvar_5=float(cvar_5),
        time_underwater_days=int(time_uw),
        annualized_volatility=float(ann_vol),
        num_trades=int(n_trades),
        win_rate=win_rate,
    )


def _group_returns(
    equity: NDArray[np.float64], dates: NDArray[np.int64], key_fn
) -> list[dict]:
    if equity.shape[0] == 0:
        return []
    out: list[dict] = []
    cur_key = key_fn(int(dates[0]))
    cur_start = float(equity[0])
    last = float(equity[0])
    for i in range(1, equity.shape[0]):
        k = key_fn(int(dates[i]))
        if k != cur_key:
            v = float(last / cur_start - 1.0) if cur_start else 0.0
            out.append({"period": cur_key, "value": v, "portfolio": v})
            cur_key = k
            cur_start = float(equity[i - 1])
        last = float(equity[i])
    v = float(last / cur_start - 1.0) if cur_start else 0.0
    out.append({"period": cur_key, "value": v, "portfolio": v})
    return out


def monthly_returns_table(equity: NDArray[np.float64], dates: NDArray[np.int64]) -> list[dict]:
    """Period-grouped monthly returns: list of {period:'YYYY-MM', value:float}."""

    def _k(d: int) -> str:
        return f"{d // 10000:04d}-{(d // 100) % 100:02d}"

    return _group_returns(equity, dates, _k)


def yearly_returns_table(equity: NDArray[np.float64], dates: NDArray[np.int64]) -> list[dict]:
    """Period-grouped yearly returns: list of {period:'YYYY', value:float}."""

    def _k(d: int) -> str:
        return f"{d // 10000:04d}"

    return _group_returns(equity, dates, _k)


def drawdown_periods(
    equity: NDArray[np.float64], dates: NDArray[np.int64] | None = None
) -> list[dict]:
    """Return list of {start, end, depth} dicts (one per underwater stretch)."""
    if equity.shape[0] == 0:
        return []
    running_max = np.maximum.accumulate(equity)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(running_max > 0, equity / running_max - 1.0, 0.0)
    raw: list[tuple[int, int, float]] = []
    in_dd = False
    start_i = 0
    depth = 0.0
    for i, v in enumerate(dd):
        if v < 0 and not in_dd:
            in_dd = True
            start_i = i
            depth = float(v)
        elif v < 0 and in_dd:
            depth = min(depth, float(v))
        elif v >= 0 and in_dd:
            raw.append((start_i, i - 1, depth))
            in_dd = False
            depth = 0.0
    if in_dd:
        raw.append((start_i, equity.shape[0] - 1, depth))

    out: list[dict] = []
    for s_idx, e_idx, dep in raw:
        if dates is not None:
            out.append({"start": int(dates[s_idx]), "end": int(dates[e_idx]), "depth": float(dep)})
        else:
            out.append({"start": int(s_idx), "end": int(e_idx), "depth": float(dep)})
    return out


def aggregate_returns(
    result_or_equity, period: Literal["M", "Y"] = "M", dates: NDArray[np.int64] | None = None
) -> list[dict]:
    """Aggregate equity curve into period returns. period='M' (monthly) or 'Y' (yearly)."""
    if isinstance(result_or_equity, BacktestResult):
        equity = np.asarray(result_or_equity.equity_curve, dtype=np.float64)
        ds = np.asarray(result_or_equity.dates, dtype=np.int64)
    else:
        equity = np.asarray(result_or_equity, dtype=np.float64)
        if dates is None:
            raise ValueError("dates required when first arg is not BacktestResult")
        ds = np.asarray(dates, dtype=np.int64)
    if period == "M":
        return monthly_returns_table(equity, ds)
    if period == "Y":
        return yearly_returns_table(equity, ds)
    raise ValueError(f"unsupported period {period!r}; use 'M' or 'Y'")


# --------------------------------------------------------------------------- comparator helpers


def buy_and_hold_curve(
    result: BacktestResult,
) -> tuple[NDArray[np.int64], NDArray[np.float64]] | None:
    """Return (dates_yyyymmdd, equity) for the engine-tracked underlying B&H curve, or None if absent."""
    if result.benchmark_curve is None:
        return None
    return (
        np.asarray(result.dates, dtype=np.int64),
        np.asarray(result.benchmark_curve, dtype=np.float64),
    )


def risk_free_curve(
    dates: NDArray[np.int64],
    rate: float,
    capital_base: float,
    *,
    trading_days: int | None = None,
) -> NDArray[np.float64]:
    """Compounded equity curve at the risk-free rate, sampled per bar."""
    from .constants import TRADING_DAYS_PER_YEAR
    td = TRADING_DAYS_PER_YEAR if trading_days is None else int(trading_days)
    n = len(dates)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    per_bar = (1.0 + float(rate)) ** (1.0 / td) - 1.0
    return float(capital_base) * np.power(1.0 + per_bar, np.arange(n, dtype=np.float64))


def compare_stats(
    strategy_result: BacktestResult,
    bh_dates: NDArray[np.int64] | None,
    bh_equity: NDArray[np.float64] | None,
) -> dict:
    """Two-curve comparison: strategy MetricsSuite, B&H MetricsSuite (or None), and the rf rate."""
    spec = strategy_result.meta.get("spec", {}) if isinstance(strategy_result.meta, dict) else {}
    rf = float(spec.get("risk_free_rate", 0.0)) if isinstance(spec, dict) else 0.0
    strategy = compute_metrics(strategy_result)
    bh: MetricsSuite | None = None
    if bh_dates is not None and bh_equity is not None and len(bh_equity) > 1:
        bh = compute_metrics(
            np.asarray(bh_equity, dtype=np.float64),
            np.asarray(bh_dates, dtype=np.int64),
            trades=[],
            risk_free_rate=rf,
        )
    return {"strategy": strategy, "buy_and_hold": bh, "risk_free_annualized": rf}
