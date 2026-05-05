"""Vectorized backtest engine.

Two sizing surfaces are supported:
  * "fixed_fraction"  — exposure  = `fraction` * capital_base * |signal|        (default)
  * "equity_compound" — exposure  = `fraction` * current_equity[t] * |signal|   (PnL compounds)
  * "inverse_vol"     — scaled to a target annualised vol (capped 5x)
  * "kelly_capped"    — fraction shrunk by rolling mean/var, capped by `kelly_cap`

Two option-leg surfaces are supported on `BacktestSpec.option_legs`:
  * legacy `OptionLeg`     — pre-priced leg (NDArray of prices), kept for back-compat
  * new      `OptionLegSpec` — declarative leg (selectors + exit rules), the chain is
                              queried per-bar via `option_chain_provider`

NaN policy on the underlying price is forward-fill for MTM. See PROGRESS.md.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Literal

import numpy as np
from numpy.typing import NDArray

from .data_load import OptionChainSnapshot, PriceSeries


# ----------------------------------------------------------------------------- specs


@dataclass(frozen=True)
class ExecutionConfig:
    """Fees, slippage, fill timing, look-ahead shift, and the annualised risk-free rate.

    `risk_free_rate` is the canonical annualised rate consumed by
    `metrics.compute_metrics(...)` for Sharpe / Sortino. Default 0.0 keeps
    pre-2022 backtests unchanged; W6+ probe 13 fires when the run overlaps the
    post-ZIRP window without an explicit value.
    """

    fees_bps: float = 5.0
    slippage_bps: float = 5.0
    fill_timing: Literal["next_open", "close"] = "next_open"
    look_ahead_shift: int = 1
    risk_free_rate: float = 0.0


@dataclass(frozen=True)
class SizingConfig:
    """Position-sizing configuration; method-specific knobs read by the engine.

    method:
      - "fixed_fraction"  — notional = capital_base * fraction * |delta_w|
      - "equity_compound" — notional = current_equity[t] * fraction * |delta_w|
      - "inverse_vol"     — notional = capital_base * (vol_target/realized_vol_252) * |delta_w|
                            capped at 5x leverage
      - "kelly_capped"    — fraction shrunk by rolling mean/var (60-bar), capped by `kelly_cap`
    """

    method: Literal["fixed_fraction", "equity_compound", "inverse_vol", "kelly_capped"] = (
        "fixed_fraction"
    )
    fraction: float = 1.0
    vol_target_annual: float | None = None
    kelly_cap: float | None = None


@dataclass(frozen=True)
class OptionLeg:
    """Legacy single-leg, pre-priced option exposure; kept for back-compat.

    Use `OptionLegSpec` (below) for new code: selectors + exit rules + chain provider.
    """

    contract_label: str
    sign: int
    qty_per_unit_signal: float
    prices: NDArray[np.float64]
    multiplier: float = 100.0


# ---------------------------------------------------------- contract selectors

@dataclass(frozen=True)
class AtmSelector:
    """Pick the at-the-money strike (or `offset_strikes` strikes away)."""

    offset_strikes: int = 0
    kind: Literal["atm"] = "atm"


@dataclass(frozen=True)
class DeltaSelector:
    """Pick the contract whose delta is closest to `target_delta`."""

    target_delta: float
    tolerance: float = 0.05
    kind: Literal["delta"] = "delta"


@dataclass(frozen=True)
class StrikeOffsetPctSelector:
    """Pick the strike nearest spot * (1 + pct_offset)."""

    pct_offset: float
    kind: Literal["pct_offset"] = "pct_offset"


@dataclass(frozen=True)
class MoneynessSelector:
    """Pick the strike nearest spot * moneyness (e.g. 0.95 = 5% OTM put)."""

    moneyness: float
    kind: Literal["moneyness"] = "moneyness"


ContractSelector = AtmSelector | DeltaSelector | StrikeOffsetPctSelector | MoneynessSelector


# ---------------------------------------------------------- expiry selectors

@dataclass(frozen=True)
class DteSelector:
    """Pick expiration with DTE closest to `target_dte`."""

    target_dte: int
    tolerance_days: int = 5
    kind: Literal["dte"] = "dte"


@dataclass(frozen=True)
class WeeklySelector:
    """Pick the nearest weekly expiration (DTE in [3, 10])."""

    kind: Literal["weekly"] = "weekly"


@dataclass(frozen=True)
class MonthlySelector:
    """Pick the nearest monthly expiration (DTE in [25, 45])."""

    kind: Literal["monthly"] = "monthly"


@dataclass(frozen=True)
class FixedExpirySelector:
    """Pick a specific YYYYMMDD expiration."""

    expiration: int
    kind: Literal["fixed"] = "fixed"


ExpirySelector = DteSelector | WeeklySelector | MonthlySelector | FixedExpirySelector


# ---------------------------------------------------------- exit rules

@dataclass(frozen=True)
class HoldToExpiration:
    """Close at expiration with intrinsic value, no fees."""

    kind: Literal["hold_to_expiration"] = "hold_to_expiration"


@dataclass(frozen=True)
class DaysToHold:
    """Close N business days after entry."""

    n: int
    kind: Literal["days_to_hold"] = "days_to_hold"


@dataclass(frozen=True)
class ExitSignal:
    """Close when named secondary signal flips (becomes 0 or sign-changes)."""

    signal_name: str
    kind: Literal["exit_signal"] = "exit_signal"


@dataclass(frozen=True)
class TrailingStop:
    """Close when leg PnL drops `stop_bps` of capital_base from its peak."""

    stop_bps: int
    kind: Literal["trailing_stop"] = "trailing_stop"


ExitRule = HoldToExpiration | DaysToHold | ExitSignal | TrailingStop


# ---------------------------------------------------------- option leg spec

@dataclass(frozen=True)
class OptionLegSpec:
    """Declarative N-leg option spec: chain query + exit rule + sizing."""

    leg_id: str
    side: Literal["long", "short"]
    qty_units: int
    option_type: Literal["C", "P"]
    contract_selector: ContractSelector
    expiry_selector: ExpirySelector
    entry_signal: str = "primary"
    exit_rule: ExitRule = field(default_factory=HoldToExpiration)
    multiplier: int = 100


@dataclass(frozen=True)
class BacktestSpec:
    """Unified inputs to `run_backtest`: bars + signal + (optional) benchmark + configs."""

    bars: PriceSeries
    signal: NDArray[np.float64]
    benchmark: PriceSeries | None = None
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    capital_base: float = 100_000.0
    rebalance_freq: Literal["bar", "daily", "weekly", "monthly", "quarterly", "annually"] = "bar"
    return_type: Literal["normal", "log"] = "normal"
    label: str = "strategy"
    # `option_legs` accepts legacy `OptionLeg` (pre-priced) OR new `OptionLegSpec`
    # (declarative: selectors + exit rules). The two paths run independently.
    option_legs: tuple[OptionLeg | OptionLegSpec, ...] = ()
    # Chain provider for `OptionLegSpec`-style legs.
    option_chain_provider: Callable[[int], OptionChainSnapshot | None] | None = None
    # Named secondary signals (used by `ExitSignal` exit rule).
    secondary_signals: dict[str, NDArray[np.float64]] = field(default_factory=dict)


@dataclass(frozen=True)
class Trade:
    """One executed underlying or leg trade at bar `date`."""

    date: int
    side: Literal["BUY", "SELL"]
    qty: float
    price: float
    cost: float
    pnl: float
    leg: str


@dataclass(frozen=True)
class BacktestResult:
    """Curves + trade log + meta produced by `run_backtest`."""

    dates: NDArray[np.int64]
    equity_curve: NDArray[np.float64]
    benchmark_curve: NDArray[np.float64] | None
    drawdown_curve: NDArray[np.float64]
    trades: list[Trade]
    positions: NDArray[np.float64]
    cash: NDArray[np.float64]
    gross_exposure: NDArray[np.float64]
    meta: dict

    @property
    def equity(self) -> NDArray[np.float64]:
        """Primary equity-curve accessor (alias of equity_curve)."""
        return self.equity_curve

    def to_json_dict(self) -> dict:
        """Return a JSON-serialisable view of the result (lists, ISO dates, plain dicts)."""

        def _iso(d: int) -> str:
            n = int(d)
            return f"{n // 10000:04d}-{(n // 100) % 100:02d}-{n % 100:02d}"

        return {
            "dates": [_iso(int(d)) for d in self.dates],
            "equity": [float(v) for v in self.equity_curve],
            "benchmark_equity": (
                [float(v) for v in self.benchmark_curve]
                if self.benchmark_curve is not None
                else None
            ),
            "drawdown": [float(v) for v in self.drawdown_curve],
            "positions": [float(v) for v in self.positions],
            "cash": [float(v) for v in self.cash],
            "gross_exposure": [float(v) for v in self.gross_exposure],
            "trades": [
                {
                    "date": _iso(int(t.date)),
                    "side": t.side,
                    "qty": float(t.qty),
                    "price": float(t.price),
                    "cost": float(t.cost),
                    "pnl": float(t.pnl),
                    "leg": t.leg,
                }
                for t in self.trades
            ],
            "meta": dict(self.meta),
        }


# ----------------------------------------------------------------------------- runner


def _validate(spec: BacktestSpec) -> None:
    if spec.capital_base <= 0:
        raise ValueError(f"capital_base must be > 0; got {spec.capital_base!r}")
    if spec.execution.look_ahead_shift < 0:
        raise ValueError(
            f"look_ahead_shift must be >= 0; got {spec.execution.look_ahead_shift!r}"
        )
    bars = spec.bars
    n = int(bars.dates.shape[0])
    if not (
        bars.open.shape[0] == n
        and bars.close.shape[0] == n
        and spec.signal.shape[0] == n
    ):
        raise ValueError(
            f"bars / signal length mismatch: dates={n}, open={int(bars.open.shape[0])}, "
            f"close={int(bars.close.shape[0])}, signal={int(spec.signal.shape[0])}"
        )
    if spec.benchmark is not None and int(spec.benchmark.close.shape[0]) != n:
        raise ValueError(
            f"benchmark length mismatch: bars={n}, benchmark={int(spec.benchmark.close.shape[0])}"
        )
    if np.any(np.diff(bars.dates) <= 0):
        bad_idx = int(np.argmax(np.diff(bars.dates) <= 0))
        raise ValueError(
            f"dates must be strictly increasing; "
            f"first non-increasing pair at index {bad_idx}: "
            f"dates[{bad_idx}]={int(bars.dates[bad_idx])}, "
            f"dates[{bad_idx + 1}]={int(bars.dates[bad_idx + 1])}"
        )
    if np.any(np.isnan(spec.signal)):
        nan_idx = np.where(np.isnan(spec.signal))[0][:5].tolist()
        raise ValueError(
            f"signal contains NaN at indices (first 5): {nan_idx}"
        )
    if np.any(np.abs(spec.signal) > 1.0 + 1e-9):
        raise ValueError(
            f"signal must lie in [-1,1]; "
            f"min={float(np.min(spec.signal))!r}, max={float(np.max(spec.signal))!r}"
        )
    for leg in spec.option_legs:
        if isinstance(leg, OptionLeg):
            if int(leg.prices.shape[0]) != n:
                raise ValueError(
                    f"option leg {leg.contract_label} prices length mismatch: "
                    f"bars={n}, prices={int(leg.prices.shape[0])}"
                )
        elif isinstance(leg, OptionLegSpec):
            if leg.qty_units < 0 or leg.multiplier <= 0:
                raise ValueError(
                    f"OptionLegSpec {leg.leg_id}: qty_units<0 or multiplier<=0; "
                    f"got qty_units={leg.qty_units!r}, multiplier={leg.multiplier!r}"
                )
            if leg.side not in ("long", "short"):
                raise ValueError(f"OptionLegSpec {leg.leg_id}: bad side {leg.side!r}")
        else:
            raise ValueError(f"unknown option leg type: {type(leg).__name__}")
    for name, arr in spec.secondary_signals.items():
        arr_n = int(np.asarray(arr).shape[0])
        if arr_n != n:
            raise ValueError(
                f"secondary signal {name!r} length mismatch: bars={n}, signal={arr_n}"
            )


def _ffill(a: NDArray[np.float64]) -> NDArray[np.float64]:
    """Forward-fill NaN values along a 1-D array, leaving leading NaN intact."""
    out = a.astype(np.float64).copy()
    last = np.nan
    for i in range(out.shape[0]):
        if np.isnan(out[i]):
            out[i] = last
        else:
            last = out[i]
    return out


def _rolling_realised_returns(close: NDArray[np.float64]) -> NDArray[np.float64]:
    """Bar-over-bar simple returns; zero on the first bar / where prev close == 0."""
    rets = np.zeros_like(close)
    rets[1:] = np.diff(close) / np.where(close[:-1] == 0, np.nan, close[:-1])
    return np.nan_to_num(rets)


def _apply_sizing(
    signal: NDArray[np.float64], sizing: SizingConfig, close: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Convert raw signal in [-1,1] to a target weight per bar in [-1,1].

    `equity_compound` is treated like `fixed_fraction` here — the equity-scaling
    multiplier is applied per-bar inside `run_backtest` because it depends on
    the live equity curve.
    """
    s = signal.astype(np.float64)
    if sizing.method in ("fixed_fraction", "equity_compound"):
        return np.clip(s * float(sizing.fraction), -1.0, 1.0)
    if sizing.method == "inverse_vol":
        from .constants import TRADING_DAYS_PER_YEAR
        target = float(sizing.vol_target_annual or 0.10)
        # Realised TRADING_DAYS_PER_YEAR-bar daily vol on close returns, annualised by sqrt(N).
        rets = _rolling_realised_returns(close)
        win = TRADING_DAYS_PER_YEAR
        vol = np.zeros_like(close)
        for i in range(close.shape[0]):
            lo = max(0, i - win + 1)
            seg = rets[lo : i + 1]
            vol[i] = float(np.std(seg, ddof=1)) if seg.size > 1 else 0.0
        ann_vol = vol * np.sqrt(float(TRADING_DAYS_PER_YEAR))
        with np.errstate(divide="ignore", invalid="ignore"):
            scale = np.where(ann_vol > 1e-8, target / np.where(ann_vol > 1e-8, ann_vol, 1.0), 0.0)
        # Cap at 5x leverage per spec.
        scale = np.minimum(scale, 5.0)
        return np.clip(s * scale, -5.0, 5.0)
    if sizing.method == "kelly_capped":
        cap = float(sizing.kelly_cap or 0.25)
        rets = _rolling_realised_returns(close)
        win = 60
        kelly = np.zeros_like(close)
        for i in range(close.shape[0]):
            lo = max(0, i - win + 1)
            seg = rets[lo : i + 1]
            if seg.size > 1:
                mu = float(np.mean(seg))
                var = float(np.var(seg, ddof=1))
                kelly[i] = mu / var if var > 1e-12 else 0.0
            else:
                kelly[i] = 0.0
        kelly = np.clip(kelly, -cap, cap)
        return np.clip(s * float(sizing.fraction) * kelly, -cap, cap)
    raise ValueError(f"unknown sizing method: {sizing.method!r}")


# ---------------------------------------------------------- multi-leg helpers


def _yyyymmdd_to_date(d: int) -> date | None:
    """Parse a YYYYMMDD int. Returns None for invalid dates (e.g., day=39)."""
    n = int(d)
    try:
        return date(n // 10000, (n // 100) % 100, n % 100)
    except ValueError:
        return None


def _calendar_dte(today: int, expiration: int) -> int | None:
    """Calendar-day distance to expiration. Negative when expired. None if either date is malformed."""
    e = _yyyymmdd_to_date(int(expiration))
    t = _yyyymmdd_to_date(int(today))
    if e is None or t is None:
        return None
    return (e - t).days


def _select_expiration(
    chain: OptionChainSnapshot, sel: ExpirySelector, today: int
) -> int | None:
    """Resolve an `ExpirySelector` against the chain's available expirations."""
    expiries = sorted({int(c.expiration) for c in chain.contracts})
    if not expiries:
        return None
    if isinstance(sel, FixedExpirySelector):
        return int(sel.expiration) if int(sel.expiration) in expiries else None
    if isinstance(sel, DteSelector):
        target = int(sel.target_dte)
        tol = int(sel.tolerance_days)
        valid = [(e, _calendar_dte(today, e)) for e in expiries]
        valid = [(e, dte) for e, dte in valid if dte is not None]
        if not valid:
            return None
        candidates = [e for e, dte in valid if abs(dte - target) <= tol]
        pool = candidates or [e for e, _ in valid]
        return min(pool, key=lambda e: abs(_calendar_dte(today, e) - target))
    if isinstance(sel, WeeklySelector):
        target = 7
        valid = [(e, _calendar_dte(today, e)) for e in expiries]
        valid = [(e, dte) for e, dte in valid if dte is not None]
        if not valid:
            return None
        return min((e for e, _ in valid), key=lambda e: abs(_calendar_dte(today, e) - target))
    if isinstance(sel, MonthlySelector):
        target = 30
        valid = [(e, _calendar_dte(today, e)) for e in expiries]
        valid = [(e, dte) for e, dte in valid if dte is not None]
        if not valid:
            return None
        return min((e for e, _ in valid), key=lambda e: abs(_calendar_dte(today, e) - target))
    return None


def _select_contract(
    chain: OptionChainSnapshot,
    sel: ContractSelector,
    expiration: int,
    option_type: str,
    spot: float,
):
    """Resolve a `ContractSelector` against contracts at the given expiration."""
    pool = [
        c
        for c in chain.contracts
        if int(c.expiration) == int(expiration) and c.option_type == option_type
    ]
    if not pool:
        return None
    pool_sorted = sorted(pool, key=lambda c: float(c.strike))
    if isinstance(sel, AtmSelector):
        # Pick strike nearest spot, then offset by N strikes.
        idx = min(range(len(pool_sorted)), key=lambda i: abs(pool_sorted[i].strike - spot))
        target_idx = max(0, min(len(pool_sorted) - 1, idx + int(sel.offset_strikes)))
        return pool_sorted[target_idx]
    if isinstance(sel, StrikeOffsetPctSelector):
        target = spot * (1.0 + float(sel.pct_offset))
        return min(pool_sorted, key=lambda c: abs(float(c.strike) - target))
    if isinstance(sel, MoneynessSelector):
        target = spot * float(sel.moneyness)
        return min(pool_sorted, key=lambda c: abs(float(c.strike) - target))
    if isinstance(sel, DeltaSelector):
        # Use chain-supplied deltas where present; skip contracts missing it.
        scored: list[tuple[float, Any]] = []
        for c in pool_sorted:
            row = c.rows[0] if c.rows else None
            d = (
                float(row.delta)
                if row is not None and row.delta is not None and not np.isnan(row.delta)
                else None
            )
            if d is None:
                continue
            scored.append((abs(d - float(sel.target_delta)), c))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0])
        if scored[0][0] > float(sel.tolerance) + 1e-9:
            return None
        return scored[0][1]
    return None


def _intrinsic(option_type: str, spot: float, strike: float) -> float:
    if option_type == "C":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _get_chain_for_date(spec: BacktestSpec, day: int) -> OptionChainSnapshot | None:
    """Fetch the option chain for `day`. Provider exceptions propagate to the caller —
    silently swallowing them masked real bugs in W7 testing."""
    if spec.option_chain_provider is not None:
        return spec.option_chain_provider(int(day))
    return None


def _entry_signal_array(
    spec: BacktestSpec, name: str, n: int, shift: int
) -> NDArray[np.float64]:
    """Resolve the entry-signal array for a leg (after look-ahead shift)."""
    if name == "primary":
        s = np.asarray(spec.signal, dtype=np.float64)
    else:
        if name not in spec.secondary_signals:
            raise ValueError(f"OptionLegSpec references unknown signal {name!r}")
        s = np.asarray(spec.secondary_signals[name], dtype=np.float64)
    out = np.roll(s, shift)
    if shift > 0:
        out[:shift] = 0.0
    return out


def _simulate_multi_leg(
    *,
    bars: PriceSeries,
    price: NDArray[np.float64],
    spec: BacktestSpec,
    spec_legs: list[OptionLegSpec],
    shift: int,
    fees: float,
    slip: float,
) -> tuple[NDArray[np.float64], list[Trade], dict[str, Any]]:
    """Simulate `OptionLegSpec`-style legs bar-by-bar. Returns (pnl, trades, meta).

    PnL accounting per leg:
      * Entry: cash flow = -side_sign * qty * mult * mid_price (debit on long, credit on short)
        plus fees+slip on absolute notional. Entry cash flow is recorded as a `pnl`
        on the entry trade.
      * Mark-to-market daily: leg_pnl[t] = qty_signed * (price[t] - price[t-1]).
        We instead realise the *cash* trajectory by booking entry/exit + bar moves
        directly: `pnl_t += -entry_cost_t  ;  on close: pnl_t += +exit_credit_t`.

    The accounting identity holds: sum(trade.pnl) == final_equity_contribution.
    """
    n = int(bars.dates.shape[0])
    dates = bars.dates.astype(np.int64)
    pnl = np.zeros(n, dtype=np.float64)
    trades: list[Trade] = []
    unfilled: list[dict[str, Any]] = []

    # State per leg
    @dataclass
    class _Open:
        leg_id: str
        side_sign: int  # +1 long, -1 short
        qty: int
        multiplier: int
        option_type: str
        strike: float
        expiration: int
        contract_id: str
        entry_date: int
        entry_idx: int
        entry_price: float
        last_mtm: float
        peak_pnl: float
        exit_rule: ExitRule

    open_by_leg: dict[str, _Open] = {}
    # Pre-resolve entry signal per leg.
    leg_entry_sig = {leg.leg_id: _entry_signal_array(spec, leg.entry_signal, n, shift) for leg in spec_legs}
    leg_exit_sig: dict[str, NDArray[np.float64]] = {}
    for leg in spec_legs:
        if isinstance(leg.exit_rule, ExitSignal):
            leg_exit_sig[leg.leg_id] = _entry_signal_array(
                spec, leg.exit_rule.signal_name, n, shift
            )

    open_legs_at_end: list[str] = []

    for t in range(n):
        today = int(dates[t])
        spot = float(price[t])
        chain_today = _get_chain_for_date(spec, today)

        for leg in spec_legs:
            sig_t = float(leg_entry_sig[leg.leg_id][t])
            sig_prev = float(leg_entry_sig[leg.leg_id][t - 1]) if t > 0 else 0.0
            is_open = leg.leg_id in open_by_leg

            # ---- entry trigger: signal flips from 0 to nonzero (or sign change) and not currently open
            entry_now = (sig_prev == 0.0 and sig_t != 0.0) or (
                np.sign(sig_prev) != np.sign(sig_t) and sig_t != 0.0
            )

            # ---- exit trigger
            exit_now = False
            exit_reason: str | None = None
            if is_open:
                op = open_by_leg[leg.leg_id]
                rule = op.exit_rule
                if isinstance(rule, HoldToExpiration):
                    if today >= int(op.expiration):
                        exit_now, exit_reason = True, "expiration"
                elif isinstance(rule, DaysToHold):
                    # business-day approximation: bar count since entry
                    if (t - op.entry_idx) >= int(rule.n):
                        exit_now, exit_reason = True, "days_to_hold"
                elif isinstance(rule, ExitSignal):
                    s_arr = leg_exit_sig.get(leg.leg_id)
                    if s_arr is not None:
                        cur = float(s_arr[t])
                        prev = float(s_arr[t - 1]) if t > 0 else 0.0
                        if cur == 0.0 and prev != 0.0:
                            exit_now, exit_reason = True, "exit_signal"
                        elif np.sign(cur) != np.sign(prev) and prev != 0.0:
                            exit_now, exit_reason = True, "exit_signal_flip"
                elif isinstance(rule, TrailingStop):
                    # peak vs current
                    drop = op.peak_pnl - op.last_mtm
                    if drop >= float(rule.stop_bps) * 1e-4 * float(spec.capital_base):
                        exit_now, exit_reason = True, "trailing_stop"

            # ---- exit before entry to free the slot
            if is_open and exit_now:
                op = open_by_leg.pop(leg.leg_id)
                # Closing price: intrinsic at expiration; otherwise last MTM-style mid via chain or carry-forward.
                exit_price: float
                exit_fee = 0.0
                if exit_reason == "expiration":
                    exit_price = _intrinsic(op.option_type, spot, op.strike)
                else:
                    # try chain mid
                    cur_price = _lookup_contract_price(chain_today, op.contract_id, op.expiration, op.strike, op.option_type)
                    if cur_price is None:
                        cur_price = op.last_mtm if op.last_mtm > 0 else op.entry_price
                    exit_price = cur_price
                    exit_fee = abs(op.qty * op.multiplier * exit_price) * (fees + slip)
                # short = sold premium at entry; closing buys back (cash out = exit_price * qty * mult).
                # Cash flow on exit = +side_sign * (-1) * qty * mult * exit_price = -side_sign * qty * mult * exit_price ... wait.
                # Convention: long position gains intrinsic at expiration -> receives exit_price.
                # short position pays intrinsic to close.
                cash_flow = op.side_sign * op.qty * op.multiplier * exit_price - exit_fee
                # But if entry_cost was -side_sign * qty * mult * entry_price (debit on long),
                # then total_pnl = cash_flow_exit + cash_flow_entry. Entry was already booked at t=entry.
                pnl[t] += cash_flow
                trades.append(
                    Trade(
                        date=today,
                        side="SELL" if op.side_sign > 0 else "BUY",
                        qty=float(op.qty * op.multiplier),
                        price=float(exit_price),
                        cost=float(exit_fee),
                        pnl=float(cash_flow),
                        leg=f"{op.leg_id}::{exit_reason or 'exit'}",
                    )
                )
                is_open = False  # noqa — slot freed

            # ---- entry: pick contract & open
            if not is_open and entry_now:
                if chain_today is None or chain_today.spot is None and spot <= 0:
                    unfilled.append({"date": today, "leg_id": leg.leg_id, "reason": "no_chain"})
                    continue
                effective_spot = float(chain_today.spot) if chain_today.spot is not None else spot
                expiry = _select_expiration(chain_today, leg.expiry_selector, today)
                if expiry is None:
                    unfilled.append({"date": today, "leg_id": leg.leg_id, "reason": "no_expiration"})
                    continue
                contract = _select_contract(
                    chain_today, leg.contract_selector, expiry, leg.option_type, effective_spot
                )
                if contract is None or not contract.rows:
                    unfilled.append({"date": today, "leg_id": leg.leg_id, "reason": "no_contract"})
                    continue
                # Use canonical `.mark` (close-if-traded else bid-ask mid) to avoid
                # the close=0 silent-zero bug on real OPT_* eod rows. Falls back to
                # `.close` for legacy rows lacking bid/ask (mark==close in that case).
                row0 = contract.rows[0]
                entry_price = float(getattr(row0, "mark", row0.close))
                if not np.isfinite(entry_price) or entry_price <= 0:
                    unfilled.append({"date": today, "leg_id": leg.leg_id, "reason": "bad_entry_price"})
                    continue
                side_sign = 1 if leg.side == "long" else -1
                qty = int(leg.qty_units * abs(int(np.sign(sig_t)) or 1))
                if qty <= 0:
                    continue
                fee_open = abs(qty * leg.multiplier * entry_price) * (fees + slip)
                # Cash flow at entry: long = -premium (debit), short = +premium (credit), minus fees.
                entry_cash = -side_sign * qty * leg.multiplier * entry_price - fee_open
                pnl[t] += entry_cash
                op = _Open(
                    leg_id=leg.leg_id,
                    side_sign=side_sign,
                    qty=qty,
                    multiplier=int(leg.multiplier),
                    option_type=leg.option_type,
                    strike=float(contract.strike),
                    expiration=int(contract.expiration),
                    contract_id=str(contract.contract_id),
                    entry_date=today,
                    entry_idx=t,
                    entry_price=entry_price,
                    last_mtm=entry_price,
                    peak_pnl=0.0,
                    exit_rule=leg.exit_rule,
                )
                open_by_leg[leg.leg_id] = op
                trades.append(
                    Trade(
                        date=today,
                        side="BUY" if side_sign > 0 else "SELL",
                        qty=float(qty * leg.multiplier),
                        price=float(entry_price),
                        cost=float(fee_open),
                        pnl=float(entry_cash),
                        leg=f"{leg.leg_id}::open",
                    )
                )

            # ---- daily MTM tracking (for trailing-stop & end-of-day reporting)
            if leg.leg_id in open_by_leg:
                op = open_by_leg[leg.leg_id]
                cur_price = _lookup_contract_price(
                    chain_today, op.contract_id, op.expiration, op.strike, op.option_type
                )
                if cur_price is None:
                    cur_price = op.last_mtm
                op.last_mtm = float(cur_price)
                # unrealised PnL = side_sign * qty * mult * (cur_price - entry_price)
                u_pnl = op.side_sign * op.qty * op.multiplier * (cur_price - op.entry_price)
                op.peak_pnl = max(op.peak_pnl, u_pnl)

    # End-of-run: any leg still open is closed at the final intrinsic value.
    # NOTE: this terminal close charges NO fees and NO slippage — the engine
    # treats end-of-backtest as a synthetic mark-to-intrinsic, NOT a real exit.
    # Strategies whose realistic operation is to actively roll/close before
    # expiration will misprice their final week of cost; declare the assumption
    # explicitly in `ASSUMPTIONS.json` when running such a strategy. Open leg
    # ids are surfaced in `multi_leg_meta["open_legs_at_end"]` for the agent
    # to inspect. (P2-6 from the W9 audit; documented rather than fixed.)
    final_t = n - 1
    final_today = int(dates[final_t]) if n > 0 else 0
    final_spot = float(price[final_t]) if n > 0 else 0.0
    for leg_id, op in list(open_by_leg.items()):
        exit_price = _intrinsic(op.option_type, final_spot, op.strike)
        cash_flow = op.side_sign * op.qty * op.multiplier * exit_price
        pnl[final_t] += cash_flow
        trades.append(
            Trade(
                date=final_today,
                side="SELL" if op.side_sign > 0 else "BUY",
                qty=float(op.qty * op.multiplier),
                price=float(exit_price),
                cost=0.0,
                pnl=float(cash_flow),
                leg=f"{op.leg_id}::eot_close",
            )
        )
        open_legs_at_end.append(leg_id)
        del open_by_leg[leg_id]

    meta = {"unfilled_legs": unfilled, "open_legs_at_end": open_legs_at_end}
    return pnl, trades, meta


def _lookup_contract_price(
    chain: OptionChainSnapshot | None,
    contract_id: str,
    expiration: int,
    strike: float,
    option_type: str,
) -> float | None:
    """Return today's close for a contract, or None if absent from this snapshot."""
    if chain is None:
        return None
    for c in chain.contracts:
        if (
            str(c.contract_id) == str(contract_id)
            or (
                int(c.expiration) == int(expiration)
                and abs(float(c.strike) - float(strike)) < 1e-9
                and c.option_type == option_type
            )
        ):
            if c.rows:
                row0 = c.rows[0]
                p = float(getattr(row0, "mark", row0.close))
                if np.isfinite(p) and p > 0:
                    return p
    return None


def run_backtest(spec: BacktestSpec) -> BacktestResult:
    """Run a vectorised backtest and return a BacktestResult.

    Single-argument call: every input lives on `spec` (bars, signal, benchmark,
    execution, sizing, capital_base, optional option_legs).
    """
    _validate(spec)
    bars = spec.bars
    n = int(bars.dates.shape[0])
    cap = float(spec.capital_base)
    fees = float(spec.execution.fees_bps) * 1e-4
    slip = float(spec.execution.slippage_bps) * 1e-4
    shift = int(spec.execution.look_ahead_shift)

    raw_close = np.asarray(bars.close, dtype=np.float64)
    nan_mask = np.isnan(raw_close)
    price = _ffill(raw_close)
    if nan_mask.any() and np.isnan(price[0]):
        first = next((i for i, v in enumerate(price) if not np.isnan(v)), None)
        if first is not None:
            price[: first + 1] = price[first]
        else:
            price[:] = 0.0
    open_ = _ffill(np.asarray(bars.open, dtype=np.float64))

    sized = _apply_sizing(spec.signal, spec.sizing, price)
    target = np.roll(sized, shift)
    if shift > 0:
        target[:shift] = 0.0

    fill_close = spec.execution.fill_timing == "close"
    fill_price = price if fill_close else open_

    prev_target = np.concatenate(([0.0], target[:-1]))
    delta_w = target - prev_target

    bar_ret = np.zeros(n, dtype=np.float64)
    safe_prev = np.where(price[:-1] == 0.0, np.nan, price[:-1])
    bar_ret[1:] = (price[1:] - price[:-1]) / safe_prev
    bar_ret = np.where(np.isnan(bar_ret), 0.0, bar_ret)

    legacy_legs = [leg for leg in spec.option_legs if isinstance(leg, OptionLeg)]
    spec_legs = [leg for leg in spec.option_legs if isinstance(leg, OptionLegSpec)]

    # ----- per-bar PnL: equity_compound walks the curve; otherwise vectorised
    equity_compound = spec.sizing.method == "equity_compound"
    if equity_compound:
        equity = np.zeros(n, dtype=np.float64)
        pnl = np.zeros(n, dtype=np.float64)
        cost_per_bar = np.zeros(n, dtype=np.float64)
        prev_eq = cap
        for t in range(n):
            if nan_mask[t]:
                equity[t] = prev_eq
                continue
            # Notional & cost scale with prev equity, not capital_base.
            scale = prev_eq if prev_eq > 0 else cap
            cost_per_bar[t] = scale * abs(float(delta_w[t])) * (fees + slip)
            prev_pos_value = (
                prev_eq * float(prev_target[t]) if t > 0 else 0.0
            )
            pnl[t] = prev_pos_value * float(bar_ret[t]) - cost_per_bar[t]
            equity[t] = prev_eq + pnl[t]
            prev_eq = equity[t]
    else:
        notional = cap * np.abs(delta_w)
        cost_per_bar = notional * (fees + slip)
        cost_per_bar = np.where(nan_mask, 0.0, cost_per_bar)
        pos_value_prev = np.concatenate(([0.0], cap * target[:-1]))
        pnl = pos_value_prev * bar_ret - cost_per_bar
        equity = cap + np.cumsum(pnl)

    # ----- legacy OptionLeg path (pre-priced)
    # Cache forward-filled leg prices once; gross_exposure + per-bar trade
    # iteration both reuse the same ndarray instead of recomputing _ffill.
    legacy_leg_prices: dict[int, NDArray[np.float64]] = {}
    for idx, leg in enumerate(legacy_legs):
        leg_prices = _ffill(leg.prices.astype(np.float64))
        legacy_leg_prices[idx] = leg_prices
        leg_qty = leg.sign * leg.qty_per_unit_signal * np.abs(prev_target) * leg.multiplier
        leg_dret = np.zeros(n, dtype=np.float64)
        leg_dret[1:] = leg_prices[1:] - leg_prices[:-1]
        leg_qty_prev = np.concatenate(([0.0], leg_qty[:-1]))
        leg_pnl = leg_qty_prev * leg_dret
        leg_dq = np.abs(np.diff(leg_qty, prepend=0.0))
        leg_cost = leg_dq * leg_prices * (fees + slip)
        pnl = pnl + leg_pnl - leg_cost
    if legacy_legs:
        # `pnl` already absorbed every leg's PnL and cost in the loop above, so
        # the cumulative sum is the canonical reconstruction in both modes. The
        # earlier branch built a stale list-comp from loop captures and was
        # immediately overwritten — removed.
        equity = cap + np.cumsum(pnl)

    # ----- new OptionLegSpec multi-leg path
    multi_leg_trades: list[Trade] = []
    multi_leg_meta: dict[str, Any] = {"unfilled_legs": [], "open_legs_at_end": []}
    if spec_legs:
        leg_pnl_total, multi_leg_trades, multi_leg_meta = _simulate_multi_leg(
            bars=bars,
            price=price,
            spec=spec,
            spec_legs=spec_legs,
            shift=shift,
            fees=fees,
            slip=slip,
        )
        pnl = pnl + leg_pnl_total
        if equity_compound:
            # Multi-leg PnL is dollar-denominated; just add to equity.
            equity = equity + np.cumsum(leg_pnl_total)
        else:
            equity = cap + np.cumsum(pnl)

    running_max = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(running_max > 0, equity / running_max - 1.0, 0.0)

    position_value = cap * target
    cash = equity - position_value
    gross = np.abs(target).copy()
    for idx, leg in enumerate(legacy_legs):
        leg_prices = legacy_leg_prices[idx]
        leg_qty = leg.sign * leg.qty_per_unit_signal * np.abs(target) * leg.multiplier
        gross = gross + np.abs(leg_qty * leg_prices) / max(cap, 1.0)

    trades: list[Trade] = []
    for t in range(n):
        if nan_mask[t]:
            continue
        dwt = float(delta_w[t])
        if abs(dwt) > 1e-12:
            scale = (
                float(equity[t - 1]) if equity_compound and t > 0 else cap
            )
            qty_units = abs(dwt) * scale / max(float(fill_price[t]), 1e-12)
            side = "BUY" if dwt > 0 else "SELL"
            trades.append(
                Trade(
                    date=int(bars.dates[t]),
                    side=side,
                    qty=float(qty_units),
                    price=float(fill_price[t]),
                    cost=float(cost_per_bar[t]),
                    pnl=0.0,
                    leg="underlying",
                )
            )
        for idx, leg in enumerate(legacy_legs):
            leg_prices = legacy_leg_prices[idx]
            cur_qty = leg.sign * leg.qty_per_unit_signal * abs(float(target[t])) * leg.multiplier
            prev_qty = leg.sign * leg.qty_per_unit_signal * abs(float(prev_target[t])) * leg.multiplier
            dq = cur_qty - prev_qty
            if abs(dq) > 1e-12:
                side = "BUY" if dq > 0 else "SELL"
                trades.append(
                    Trade(
                        date=int(bars.dates[t]),
                        side=side,
                        qty=float(abs(dq)),
                        price=float(leg_prices[t]),
                        cost=float(abs(dq) * leg_prices[t] * (fees + slip)),
                        pnl=0.0,
                        leg=leg.contract_label,
                    )
                )
    trades.extend(multi_leg_trades)

    benchmark_curve: NDArray[np.float64] | None = None
    if spec.benchmark is not None:
        bm = _ffill(np.asarray(spec.benchmark.close, dtype=np.float64))
        b0 = bm[0] if not np.isnan(bm[0]) and bm[0] != 0 else 1.0
        benchmark_curve = bm / b0 * cap

    meta: dict[str, Any] = {
        "spec": {
            "capital_base": cap,
            "fees_bps": spec.execution.fees_bps,
            "slippage_bps": spec.execution.slippage_bps,
            "fill_timing": spec.execution.fill_timing,
            "look_ahead_shift": shift,
            "risk_free_rate": float(spec.execution.risk_free_rate),
            "rebalance_freq": spec.rebalance_freq,
            "return_type": spec.return_type,
            "label": spec.label,
            "sizing_method": spec.sizing.method,
            "sizing_fraction": spec.sizing.fraction,
        },
        "look_ahead_applied": shift,
        "n_bars": int(n),
        "nan_bars": int(nan_mask.sum()),
        "instrument_id": bars.instrument_id,
        "benchmark_id": (spec.benchmark.instrument_id if spec.benchmark is not None else None),
        "run_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "n_option_legs_spec": len(spec_legs),
        "n_option_legs_legacy": len(legacy_legs),
        **multi_leg_meta,
    }

    return BacktestResult(
        dates=bars.dates.astype(np.int64),
        equity_curve=equity.astype(np.float64),
        benchmark_curve=benchmark_curve,
        drawdown_curve=dd.astype(np.float64),
        trades=trades,
        positions=target.astype(np.float64),
        cash=cash.astype(np.float64),
        gross_exposure=gross.astype(np.float64),
        meta=meta,
    )


__all__ = [
    "ExecutionConfig",
    "SizingConfig",
    "OptionLeg",
    "OptionLegSpec",
    "ContractSelector",
    "AtmSelector",
    "DeltaSelector",
    "StrikeOffsetPctSelector",
    "MoneynessSelector",
    "ExpirySelector",
    "DteSelector",
    "WeeklySelector",
    "MonthlySelector",
    "FixedExpirySelector",
    "ExitRule",
    "HoldToExpiration",
    "DaysToHold",
    "ExitSignal",
    "TrailingStop",
    "BacktestSpec",
    "Trade",
    "BacktestResult",
    "run_backtest",
]
