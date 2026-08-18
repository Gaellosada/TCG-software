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

# Tick size (minimum price increment, index points) for the 1-tick spread floor
# in ``max_spread``. The dwh v2 schema has NO min-increment column anywhere
# (checked ``information_schema`` on contract/object/serie — none exists), so
# this documented CME ES-option constant is used. See PROBLEMS.md.
#
# The real CME E-mini S&P 500 (ES) option tick is TWO-TIER (verified against the
# CME contract spec, 2026-08): trades priced at or below 5.00 index points move
# in 0.05 increments, above 5.00 in 0.25 increments. ``ES_OPTION_TICK_SIZE`` is
# the LOW tier (the value the dwh sourcing method returns); the tier rule is
# applied to the spread floor via :func:`es_option_tick` so a typical (>5.00)
# ATM leg quoted one true tick wide (0.25) is not spuriously rejected.
ES_OPTION_TICK_SIZE: float = 0.05  # low tier: premium <= ES_OPTION_TICK_TIER_PT
ES_OPTION_TICK_SIZE_HIGH: float = 0.25  # high tier: premium > ES_OPTION_TICK_TIER_PT
ES_OPTION_TICK_TIER_PT: float = 5.00  # premium (index pts) tier boundary


def es_option_tick(mark: float, low_tick: float = ES_OPTION_TICK_SIZE) -> float:
    """Tier-aware CME ES-option tick for a leg quoted at *mark* (index points).

    ``low_tick`` (the sourced low-tier increment, 0.05) applies at or below the
    5.00-point tier boundary; the coarse 0.25 tick applies strictly above it.
    """
    return low_tick if mark <= ES_OPTION_TICK_TIER_PT else ES_OPTION_TICK_SIZE_HIGH

# Tick size (minimum price increment, index points) for the ES FUTURE — the
# hedge instrument. CME ES futures trade in 0.25 index-point ticks. Used by the
# hedge-module ``max_spread`` condition's 1-tick floor on the ES-future bar. A
# documented constant (same rationale as the option tick: no dwh column exists).
ES_FUTURE_TICK_SIZE: float = 0.25


@dataclass(frozen=True)
class IntradayBar:
    """One intraday mark: a full ``timestamptz`` (UTC) and a price/value.

    Used for both the ES future price series and per-contract option marks.
    ``price`` is the effective MARK: the bbba two-sided mid when available,
    else the bar close (recon §3). ``ts`` preserves the minute-level time — it
    is NOT truncated to a date (the recon flags that trap).

    For option marks the quote fields (``bid``/``ask``/``bid_size``/
    ``ask_size``) carry the top-of-book at the event; they are ``None`` on a
    last-trade-only bar (no two-sided quote — the fields the conditional
    ``max_spread`` / ``min_quote_size`` modules read). ES-future bars leave
    them ``None`` (only the close is used there).
    """

    ts: datetime
    price: float
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None


# --------------------------------------------------------------------------- #
# Conditional entry/exit modules (v2). Engine-level, dependency-free dataclasses
# the API mirrors from its discriminated-union Pydantic condition models. A bar
# qualifies for a leg iff ALL enabled conditions pass (AND-ed).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MaxSpreadCond:
    """Per-leg. Pass if ``(ask-bid) <= max(pct/100*mid, min_ticks*tick_size)``.

    REQUIRES a two-sided quote (bid AND ask present); a last-trade-only bar
    fails. ``mid`` is the bar mark (``IntradayBar.price``).
    """

    pct: float
    min_ticks: float = 1.0


@dataclass(frozen=True)
class MinQuoteSizeCond:
    """Per-leg. Pass if ``bid_size >= size AND ask_size >= size``.

    REQUIRES both quote sizes present (last-trade-only bar fails).
    """

    size: float


@dataclass(frozen=True)
class MinPremiumCond:
    """Per-leg. Pass if the leg mark ``mid >= points``."""

    points: float


@dataclass(frozen=True)
class MaxUnderlyingMoveCond:
    """ES-level. Pass if ``abs(es - es_ref)/es_ref*100 <= pct``.

    ``es_ref`` = the first ES 1m bar of that day (``ref="day_open"``).
    """

    pct: float
    ref: str = "day_open"


# --------------------------------------------------------------------------- #
# Early-exit TRIGGERS (v3). Attached to the EXIT module only. Each closes the
# straddle EARLY (before ``exit.time``); the FIRST to fire wins (OR). Engine-
# level, dependency-free dataclasses the API mirrors from its discriminated-union
# Pydantic trigger models. Move / delta / sigma thresholds are ABSOLUTE (fire in
# either direction); only ``pnl`` is directional.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UnderlyingMoveTrigger:
    """Fire if ``abs(ES_bar - ES_entry) >= amount`` (``unit="points"``) or
    ``>= amount/100 * ES_entry`` (``unit="percent"``). ``ES_entry`` = ES at
    ``straddle_on_ts``."""

    amount: float
    unit: str = "points"  # "points" | "percent"


@dataclass(frozen=True)
class SigmaMoveTrigger:
    """Fire if ``abs(ES_bar - ES_entry) >= n * sigma_bar`` where
    ``sigma_bar = ES_entry * IV_entry * sqrt(T_bar)`` (sigma shrinks intraday as
    expiry nears). ``IV_entry`` = ATM implied vol backed out of the ENTRY
    straddle (Black-76 inversion, avg call/put)."""

    n: float


@dataclass(frozen=True)
class NetDeltaTrigger:
    """Fire if ``abs(pre_hedge_net_straddle_delta(bar)) >= threshold`` (the same
    net delta the hedge loop computes, BEFORE hedging)."""

    threshold: float


@dataclass(frozen=True)
class PnlTrigger:
    """Position P&L to the bar = straddle option MTM (side-signed vs entry
    premium) + hedge MTM so far. ``unit``: points | percent (of entry premium) |
    usd (x multiplier). ``direction``: profit (``pnl >= +amount``), loss
    (``pnl <= -amount``), both (``abs(pnl) >= amount``)."""

    amount: float
    unit: str = "usd"  # "points" | "percent" | "usd"
    direction: str = "both"  # "profit" | "loss" | "both"


@dataclass(frozen=True)
class ExitTrigger:
    """The firing trigger that closed a day EARLY (``None`` on a time exit).

    ``value`` is the observed quantity that crossed the threshold, in the
    trigger's own terms: underlying_move -> the move (points or percent per its
    unit); sigma_move -> realized sigmas (``move/sigma_bar``); net_delta ->
    ``abs(net_delta)``; pnl -> the signed P&L in the trigger's unit.
    """

    type: str
    ts: datetime
    value: float


# --------------------------------------------------------------------------- #
# Hedge module (v4). The flat ``hedge:{enabled,interval_minutes,delta_band}`` is
# replaced by a configurable module: OR-ed rehedge TRIGGERS, AND-ed execution
# CONDITIONS (evaluated on the ES-FUTURE bar), and a TARGET describing how much
# delta to remove. Engine-level, dependency-free dataclasses the API mirrors from
# its Pydantic models. ``MaxSpreadCond`` / ``MinQuoteSizeCond`` (above) are reused
# for the hedge conditions — evaluated against the ES-future bar's quote here.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MinRehedgeDeltaCond:
    """Hedge condition. Pass (execute) only if ``abs(delta_to_remove) >=
    threshold`` — where ``delta_to_remove`` is the change in the ES-future
    position a rehedge WOULD make. Suppresses churny micro-rehedges."""

    threshold: float


@dataclass(frozen=True)
class SigmaMoveHedgeTrigger:
    """Rehedge trigger: fire when the ES has moved ``>= n * sigma_bar`` since the
    last hedge, where ``sigma_bar = ES_ref * IV_entry * sqrt(T_bar)`` and
    ``ES_ref`` = ES at the last hedge (same sigma machinery as the exit
    sigma_move trigger). ``enabled=False`` disables it."""

    enabled: bool = False
    n: float = 1.0


@dataclass(frozen=True)
class HedgeTriggers:
    """The OR-ed rehedge triggers. A rehedge is CONSIDERED at a bar if ANY
    enabled trigger fires: ``interval_minutes`` elapsed since the last hedge, OR
    ``abs(delta drift since last hedge) >= delta_band``, OR the sigma_move
    trigger. ``interval_minutes``/``delta_band`` = ``None`` (or ``0`` for the
    interval) disables that trigger."""

    interval_minutes: float | None = 15.0
    delta_band: float | None = 0.10
    sigma_move: SigmaMoveHedgeTrigger = field(default_factory=SigmaMoveHedgeTrigger)


@dataclass(frozen=True)
class HedgeTargetSpec:
    """How much delta a rehedge removes.

    * ``zero``      — ``hedged_qty = -net_delta`` (full hedge; current behavior).
    * ``band_edge`` — hedge back to the band edge: leave ``sign(net_delta)*
      delta_band`` of delta on (residual = ±band). Requires ``delta_band`` set.
    * ``ratio``     — ``hedged_qty = -ratio*net_delta`` (partial; ratio in (0,1]).
    """

    mode: str = "zero"  # "zero" | "band_edge" | "ratio"
    ratio: float = 1.0


@dataclass(frozen=True)
class HedgeSpec:
    """The full hedge module (v4). ``enabled=False`` => no hedge (hedge_pnl=0).

    ``instrument`` is ``es_future`` for v1 (the field exists so more can be added
    later; the API rejects unknown instruments 422). ``conditions`` is a tuple of
    ``MaxSpreadCond`` / ``MinQuoteSizeCond`` / ``MinRehedgeDeltaCond`` — a
    considered rehedge EXECUTES only if ALL pass on the ES-future bar; else it is
    DEFERRED (reconsidered next bar, last-hedge state unchanged)."""

    enabled: bool = False
    instrument: str = "es_future"
    triggers: HedgeTriggers = field(default_factory=HedgeTriggers)
    conditions: tuple = ()
    target: HedgeTargetSpec = field(default_factory=HedgeTargetSpec)


@dataclass(frozen=True)
class MarkSnapshot:
    """Straddle marks at a single instant (entry or exit)."""

    ts: datetime
    underlying: float
    call_mid: float
    put_mid: float
    straddle_price: float


@dataclass(frozen=True)
class LegResult:
    """Per-leg fill/close outcome (independent-leg selection, v2).

    ``exit_conditions_met`` is ``False`` when the exit fell back to the nearest
    available bar because no bar in the exit window satisfied the exit
    conditions (the leg must still close). ``pnl_pts`` is side-signed, in index
    points (``side_sign*(exit_price-entry_price)``); dollarization is ×
    multiplier at the day level.
    """

    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_conditions_met: bool
    pnl_pts: float


@dataclass(frozen=True)
class StraddleLegs:
    """The two independent legs of one day's straddle."""

    call: LegResult
    put: LegResult


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
    """Outcome for one trading day (traded or skipped).

    ``entry``/``exit`` are the STRADDLE-level summary (entry ts =
    ``straddle_on_ts``, ``straddle_price`` = call+put). ``legs`` carries the
    per-leg independent fills; ``straddle_on_ts`` / ``straddle_off_ts`` bound
    the BOTH-ON (hedged) window.
    """

    date: int  # YYYYMMDD
    status: str  # "ok" | "skipped" | "excluded"
    skip_reason: str | None = None
    expiry: date | None = None
    strike: float | None = None
    entry: MarkSnapshot | None = None
    exit: MarkSnapshot | None = None
    hedge_trades: tuple[HedgeTrade, ...] = ()
    pnl: DayPnl | None = None
    legs: StraddleLegs | None = None
    straddle_on_ts: datetime | None = None
    straddle_off_ts: datetime | None = None
    # v3: the early-exit trigger that fired (``None`` => time exit / no trigger).
    exit_trigger: ExitTrigger | None = None


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
