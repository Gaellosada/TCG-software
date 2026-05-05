"""Actionable-insight detection for P4 -> P6 auto-suggest.

Implements the five diagnostics described in `pipeline/04-analyze.md`:

  - `regime_concentration`   — share of all-time-low-equity bars in worst calendar period
  - `trade_skew`             — top-5%-by-pnl share of total pnl (top-1% as escalation)
  - `sharpe_below_benchmark` — gap between benchmark Sharpe and portfolio Sharpe
  - `max_drawdown_exceeds`   — `metrics.max_drawdown` <= -0.3 / -0.4
  - `turnover_excessive`     — annualised turnover from trades

Each diagnostic emits a record `{id, value, threshold_medium, threshold_high,
fired, severity, context?}`; `compute_diagnostics(...)` returns the full
exhaustive list plus the `should_suggest` boolean that P6 reads. The combiner
fires when (>=2 medium fire) OR (>=1 high fires) — a single medium signal is
not enough.

P4 calls this; P6 reads `results/diagnostics.json`. The lib does NOT propose a
variant — P6 owns that decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from .engine import BacktestResult, Trade
from .metrics import MetricsSuite


Severity = Literal["medium", "high"]


@dataclass(frozen=True)
class Diagnostic:
    """One named diagnostic record (frozen; serialised as a dict)."""

    id: str
    value: Any
    threshold_medium: float
    threshold_high: float
    fired: bool
    severity: Severity | None
    context: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "value": self.value,
            "threshold_medium": self.threshold_medium,
            "threshold_high": self.threshold_high,
            "fired": self.fired,
            "severity": self.severity,
        }
        if self.context is not None:
            out["context"] = self.context
        return out


# ----------------------------------------------------------------------------- detectors


def regime_concentration(result: BacktestResult) -> Diagnostic:
    """Share of all-time-low-equity bars concentrated in the worst single calendar year.

    Flags whether the worst drawdown is dominated by one regime (e.g. 2022).
    Threshold rationale: 0.6 medium (60% of bad bars cluster in one period) is
    tighter than a 50% cut so isolated 2022-style years don't fire on every
    backtest. 0.8 escalates to high.
    """
    equity = np.asarray(result.equity_curve, dtype=np.float64)
    dates = np.asarray(result.dates, dtype=np.int64)
    n = int(equity.shape[0])
    if n < 2:
        return Diagnostic(
            id="regime_concentration",
            value=0.0,
            threshold_medium=0.6,
            threshold_high=0.8,
            fired=False,
            severity=None,
            context={"reason": "insufficient bars"},
        )
    running_max = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(running_max > 0, equity / running_max - 1.0, 0.0)
    in_dd = dd < 0
    total_dd_bars = int(np.sum(in_dd))
    if total_dd_bars == 0:
        return Diagnostic(
            id="regime_concentration",
            value=0.0,
            threshold_medium=0.6,
            threshold_high=0.8,
            fired=False,
            severity=None,
            context={"total_dd_bars": 0},
        )
    years = (dates // 10000).astype(np.int64)
    per_year: dict[int, int] = {}
    for y, flag in zip(years, in_dd):
        if flag:
            per_year[int(y)] = per_year.get(int(y), 0) + 1
    worst_year, worst_count = max(per_year.items(), key=lambda kv: kv[1])
    share = float(worst_count / total_dd_bars)
    severity: Severity | None = None
    if share >= 0.8:
        severity = "high"
    elif share >= 0.6:
        severity = "medium"
    return Diagnostic(
        id="regime_concentration",
        value=share,
        threshold_medium=0.6,
        threshold_high=0.8,
        fired=severity is not None,
        severity=severity,
        context={
            "worst_period": str(worst_year),
            "max_dd_bars": int(worst_count),
            "total_bars": int(total_dd_bars),
        },
    )


def trade_skew(trades: list[Trade]) -> Diagnostic:
    """Top-5%-by-pnl share of total pnl. High tier on top-1% share >= 0.5.

    A 1-in-20 trade carrying half the edge is real fragility; 1-in-100 is
    severe. Trades with zero pnl (open legs) contribute nothing.
    """
    pnls = np.asarray([float(t.pnl) for t in (trades or [])], dtype=np.float64)
    pnls = pnls[pnls != 0.0]
    n_trades = int(pnls.size)
    if n_trades < 20:
        # Below 20 trades, the top-5% bucket is too small to be meaningful.
        return Diagnostic(
            id="trade_skew",
            value={"top_5pct_share": 0.0, "top_1pct_share": 0.0},
            threshold_medium=0.5,
            threshold_high=0.5,
            fired=False,
            severity=None,
            context={"n_closed_trades": n_trades, "reason": "n<20"},
        )
    total = float(np.sum(pnls))
    # Degenerate-denominator guard. The skew ratio's denominator is
    # `sum(pnl)`; when that is near zero relative to per-trade scale, the
    # ratio is mathematically defined but operationally meaningless (a few
    # winners offset a few losers — share-of-total can read 4.34 or -8.0
    # without indicating fragility). Fire `degenerate=True` and skip the
    # `should_suggest` HIGH path. The 1% threshold is per-trade-relative
    # rather than absolute so it scales with strategy size.
    abs_pnl_total = float(np.sum(np.abs(pnls)))
    degenerate = abs_pnl_total > 0 and abs(total) < 0.01 * abs_pnl_total
    if abs(total) < 1e-12 or degenerate:
        return Diagnostic(
            id="trade_skew",
            value={"top_5pct_share": 0.0, "top_1pct_share": 0.0},
            threshold_medium=0.5,
            threshold_high=0.5,
            fired=False,
            severity=None,
            context={
                "n_closed_trades": n_trades,
                "reason": "sum(pnl)~0" if abs(total) < 1e-12 else "degenerate-denominator",
                "degenerate": True,
                "sum_pnl": total,
                "abs_pnl_total": abs_pnl_total,
            },
        )
    sorted_pnl = np.sort(pnls)[::-1]
    k5 = max(1, int(np.ceil(0.05 * n_trades)))
    k1 = max(1, int(np.ceil(0.01 * n_trades)))
    top5_share = float(np.sum(sorted_pnl[:k5]) / total)
    top1_share = float(np.sum(sorted_pnl[:k1]) / total)
    severity: Severity | None = None
    if top1_share >= 0.5:
        severity = "high"
    elif top5_share >= 0.5:
        severity = "medium"
    return Diagnostic(
        id="trade_skew",
        value={"top_5pct_share": top5_share, "top_1pct_share": top1_share},
        threshold_medium=0.5,
        threshold_high=0.5,
        fired=severity is not None,
        severity=severity,
        context={"n_closed_trades": n_trades, "k5": int(k5), "k1": int(k1)},
    )


def sharpe_below_benchmark(
    portfolio_sharpe: float, benchmark_sharpe: float | None
) -> Diagnostic:
    """Benchmark Sharpe minus portfolio Sharpe. Medium when gap >= 0.5; no high tier."""
    gap = (
        float(benchmark_sharpe) - float(portfolio_sharpe)
        if benchmark_sharpe is not None
        else 0.0
    )
    severity: Severity | None = "medium" if gap >= 0.5 else None
    return Diagnostic(
        id="sharpe_below_benchmark",
        value=gap,
        threshold_medium=0.5,
        threshold_high=float("inf"),
        fired=severity is not None,
        severity=severity,
        context={
            "portfolio_sharpe": float(portfolio_sharpe),
            "benchmark_sharpe": (None if benchmark_sharpe is None else float(benchmark_sharpe)),
        },
    )


def max_drawdown_exceeds(max_dd: float) -> Diagnostic:
    """`metrics.max_drawdown <= -0.3` medium; `<= -0.4` high. Drawdowns > 30% are clinically severe."""
    severity: Severity | None = None
    if max_dd <= -0.4:
        severity = "high"
    elif max_dd <= -0.3:
        severity = "medium"
    return Diagnostic(
        id="max_drawdown_exceeds",
        value=float(max_dd),
        threshold_medium=-0.3,
        threshold_high=-0.4,
        fired=severity is not None,
        severity=severity,
    )


def turnover_excessive(
    trades: list[Trade], n_bars: int, *, trading_days: int | None = None
) -> Diagnostic:
    """Annualised turnover (trades per year * avg position change fraction).

    Approximation: `trades_per_year * mean(|qty_per_trade| / capital_base ~ 1)`.
    Without a clean capital_base reference here, we use trades_per_year
    directly — 12+ trades/year for a position-strategy is high. Threshold 12.0
    medium; no high tier.
    """
    from .constants import TRADING_DAYS_PER_YEAR
    td = TRADING_DAYS_PER_YEAR if trading_days is None else int(trading_days)
    n_trades = len([t for t in (trades or []) if t.pnl != 0.0 or t.qty > 0])
    if n_bars <= 0:
        annual_turnover = 0.0
    else:
        years = max(n_bars / float(td), 1.0 / float(td))
        annual_turnover = float(n_trades) / years
    severity: Severity | None = "medium" if annual_turnover >= 12.0 else None
    return Diagnostic(
        id="turnover_excessive",
        value=annual_turnover,
        threshold_medium=12.0,
        threshold_high=float("inf"),
        fired=severity is not None,
        severity=severity,
        context={"n_trades": int(n_trades), "n_bars": int(n_bars)},
    )


# ----------------------------------------------------------------------------- combiner


def should_suggest(diagnostics: list[Diagnostic]) -> bool:
    """Combiner: True when (>=2 fired) OR (>=1 high). Matches the spec gate exactly."""
    fired = [d for d in diagnostics if d.fired]
    high = [d for d in fired if d.severity == "high"]
    return len(fired) >= 2 or len(high) >= 1


# ----------------------------------------------------------------------------- top-level


def compute_diagnostics(
    result: BacktestResult,
    metrics: MetricsSuite,
    *,
    benchmark_metrics: MetricsSuite | None = None,
) -> dict[str, Any]:
    """Run all diagnostics and return the JSON-shape `results/diagnostics.json` dict.

    The output matches the schema in `pipeline/04-analyze.md`:
        {as_of: ISO, should_suggest: bool, diagnostics: [...]}
    Each diagnostic in the list is exhaustive even when not fired.
    """
    bench_sharpe = benchmark_metrics.sharpe_ratio if benchmark_metrics is not None else None
    diagnostics: list[Diagnostic] = [
        regime_concentration(result),
        trade_skew(list(result.trades)),
        sharpe_below_benchmark(metrics.sharpe_ratio, bench_sharpe),
        max_drawdown_exceeds(metrics.max_drawdown),
        turnover_excessive(list(result.trades), n_bars=int(result.dates.shape[0])),
    ]
    return {
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "should_suggest": bool(should_suggest(diagnostics)),
        "diagnostics": [d.to_dict() for d in diagnostics],
    }


__all__ = [
    "Diagnostic",
    "regime_concentration",
    "trade_skew",
    "sharpe_below_benchmark",
    "max_drawdown_exceeds",
    "turnover_excessive",
    "should_suggest",
    "compute_diagnostics",
]
