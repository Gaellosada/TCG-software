"""Frozen result/param types for the intraday options backtest.

Dependency-free (only stdlib + this layer) so both ``tcg.engine`` and
``tcg.core`` may import them without violating the module-boundary contract
(types <- data/engine <- core). The engine emits these dataclasses; the API
layer mirrors them into Pydantic response models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Intraday-options data window (pinned from dwh_intraday_recon.md §2/§6 — the
# intersection of ES-future and ES-option 1m coverage). Reject/clip out-of-window
# dates loudly rather than silently returning empty.
WINDOW_MIN_DATE: date = date(2025, 1, 1)
WINDOW_MAX_DATE: date = date(2026, 7, 31)

# Dollarization multiplier for the CME ES option/future complex ($50 / index pt).
ES_MULTIPLIER: float = 50.0


@dataclass(frozen=True)
class IntradayBar:
    """One intraday mark: a full ``timestamptz`` (UTC) and a price/value.

    Used for both the ES future price series and per-contract option marks
    (bbba mid, falling back to bar close). ``ts`` preserves the minute-level
    time — it is NOT truncated to a date (the recon flags that trap).
    """

    ts: datetime
    price: float


@dataclass(frozen=True)
class MarkSnapshot:
    """Straddle marks at a single instant (entry or exit)."""

    ts: datetime
    underlying: float
    call_mid: float
    put_mid: float
    straddle_price: float


@dataclass(frozen=True)
class HedgeTrade:
    """One delta-hedge rebalance event."""

    ts: datetime
    underlying: float
    net_delta: float  # straddle position net delta (side-signed)
    hedge_qty: float  # ES-future position AFTER this rehedge (= -net_delta)


@dataclass(frozen=True)
class DayPnl:
    """Per-day P&L, in index points and dollars (points x multiplier)."""

    option_pnl_pts: float
    hedge_pnl_pts: float
    total_pnl_pts: float
    total_pnl_usd: float


@dataclass(frozen=True)
class DayResult:
    """Outcome for one trading day (traded or skipped)."""

    date: int  # YYYYMMDD
    status: str  # "ok" | "skipped"
    skip_reason: str | None = None
    expiry: date | None = None
    strike: float | None = None
    entry: MarkSnapshot | None = None
    exit: MarkSnapshot | None = None
    hedge_trades: tuple[HedgeTrade, ...] = ()
    pnl: DayPnl | None = None


@dataclass(frozen=True)
class EquityPoint:
    date: int  # YYYYMMDD
    cum_pnl_usd: float


@dataclass(frozen=True)
class AggregateResult:
    """Aggregate statistics over the traded days."""

    n_days: int
    n_traded: int
    n_skipped: int
    total_pnl_usd: float
    mean_daily_pnl_usd: float
    win_rate: float | None
    sharpe: float
    max_drawdown_usd: float
    equity_curve: tuple[EquityPoint, ...] = ()


@dataclass(frozen=True)
class BacktestResult:
    """Full backtest output: per-day results + aggregate + warnings."""

    days: tuple[DayResult, ...]
    aggregate: AggregateResult
    warnings: tuple[str, ...] = field(default_factory=tuple)
