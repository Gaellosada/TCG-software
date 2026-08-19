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
class SkipNearExtremumSpec:
    """F1.2 — skip a hedge near the RUNNING session extremum in the late session.

    In the final ``window_minutes`` before the hedge-window close, SUPPRESS a
    hedge that would BUY the ES future while ES is within ``tolerance`` of the
    RUNNING session HIGH, and symmetrically SUPPRESS one that would SELL while
    within ``tolerance`` of the RUNNING session LOW. This is the general symmetric
    "don't buy the high / sell the low into the close" rule — agnostic to the
    straddle side, keyed on the hedge TRADE's buy/sell direction.

    ``enabled=False`` (the DEFAULT) => the gate is inert (baseline preserved).
    The "session high/low" is the running extremum from session open up to and
    INCLUDING the current bar only — never the full-day high (no look-ahead).

    * ``window_minutes`` — how many minutes before the hedge-window close the gate
      is active (default 30).
    * ``tolerance`` — proximity to the running extremum that triggers the skip,
      in ES index points (``tolerance_unit="points"``) or as a percent of the
      current ES price (``tolerance_unit="percent"``). ``0`` => only exactly at
      the extremum.
    """

    enabled: bool = False
    window_minutes: float = 30.0
    tolerance: float = 2.0
    tolerance_unit: str = "points"  # "points" | "percent"


@dataclass(frozen=True)
class HedgeTimingSpec:
    """Session-relative hedge-timing gates (F1.1 + F1.2). All neutral by DEFAULT
    => a default HedgeSpec hedges exactly as before (regression-preserving).

    * ``only_within_minutes_before_close`` (F1.1) — when set, a (re)hedge is only
      CONSIDERED in the final N minutes before the hedge-window close; earlier
      bars (including the entry hedge) are suppressed. ``None`` (default) => no
      time restriction (today's behavior).
    * ``skip_near_extremum`` (F1.2) — the late-session buy-high / sell-low skip.

    "Hedge-window close" is the endpoint of the delta-hedge window for the day
    (``straddle_off_ts`` in the realized hedge) — derived from the existing
    session/bar range, needing no extra input and never peeking past it.
    """

    only_within_minutes_before_close: float | None = None
    skip_near_extremum: SkipNearExtremumSpec = field(
        default_factory=SkipNearExtremumSpec
    )


@dataclass(frozen=True)
class HedgeSpec:
    """The full hedge module (v4). ``enabled=False`` => no hedge (hedge_pnl=0).

    ``instrument`` is ``es_future`` for v1 (the field exists so more can be added
    later; the API rejects unknown instruments 422). ``conditions`` is a tuple of
    ``MaxSpreadCond`` / ``MinQuoteSizeCond`` / ``MinRehedgeDeltaCond`` — a
    considered rehedge EXECUTES only if ALL pass on the ES-future bar; else it is
    DEFERRED (reconsidered next bar, last-hedge state unchanged). ``timing`` adds
    the session-relative hedge-timing gates (F1.1/F1.2); its default is fully
    neutral so an existing HedgeSpec behaves identically."""

    enabled: bool = False
    instrument: str = "es_future"
    triggers: HedgeTriggers = field(default_factory=HedgeTriggers)
    conditions: tuple = ()
    target: HedgeTargetSpec = field(default_factory=HedgeTargetSpec)
    timing: HedgeTimingSpec = field(default_factory=HedgeTimingSpec)


@dataclass(frozen=True)
class CostModel:
    """Transaction-cost model for the option legs AND the ES delta-hedge.

    ``enabled=False`` (the DEFAULT) => ZERO cost: every fill is taken at the bbba
    two-sided MID exactly as before, so a cost-off run reproduces the prior
    mid-fill P&L bit-for-bit (regression-preserving).

    When ``enabled=True`` an ADVERSE half-spread crossing is charged on every
    fill: a BUY fills at ``mid + half_spread`` and a SELL at ``mid - half_spread``
    where ``half_spread = (ask - bid)/2`` from the SAME bbba bar used for the
    mark. The magnitude of the charge is ``half_spread`` per unit crossed and it
    ALWAYS reduces P&L, independent of side (a long entry buys / a short entry
    sells — both cross the spread adversely).

    One-sided / last-trade bars have no two-sided quote (``bid`` or ``ask``
    ``None``), so the half-spread is UNDEFINED. A fixed ``fallback_cost_pts`` per
    fill side (index points, default ``0.0``) is charged instead, and every such
    fill is COUNTED (``n_fallback_fills``) so the fallback's coverage is
    measurable rather than silently mid-filled. For the ES hedge the per-unit
    cost (spread or fallback) scales by the size of the ES trade the rehedge
    makes; ``n_fallback_fills`` counts fallback TRADE events (size-independent).
    """

    enabled: bool = False
    fallback_cost_pts: float = 0.0


# A disabled cost model: the ``simulate_day`` default (mid fills, zero cost).
COST_DISABLED = CostModel(enabled=False)


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
    """Per-day P&L, in index points and dollars (points x multiplier).

    ``option_pnl_pts`` and ``hedge_pnl_pts`` are GROSS (mid-fill) P&L. When a
    :class:`CostModel` is enabled, ``cost_pts`` (>= 0) is the total transaction
    cost charged across the option + hedge fills and ``total_pnl_pts`` is NET of
    it (``option + hedge - cost``); cost-off runs carry ``cost_pts == 0.0`` and
    ``total == option + hedge`` exactly as before. ``n_fallback_fills`` counts
    fills that hit the fixed fallback (a one-sided / last-trade bar).
    """

    option_pnl_pts: float
    hedge_pnl_pts: float
    total_pnl_pts: float
    total_pnl_usd: float
    cost_pts: float = 0.0
    cost_usd: float = 0.0
    n_fallback_fills: int = 0


@dataclass(frozen=True)
class DayResult:
    """Outcome for one trading day (traded or skipped).

    ``entry``/``exit`` are the STRADDLE-level summary (entry ts =
    ``straddle_on_ts``, ``straddle_price`` = call+put). ``legs`` carries the
    per-leg independent fills; ``straddle_on_ts`` / ``straddle_off_ts`` bound
    the BOTH-ON (hedged) window.

    ``entries`` (F4.1 laddered multi-entry) is EMPTY for the single-entry path
    (the day IS the straddle) — so a non-laddered DayResult is bit-identical to
    before. When a day is laddered it carries N :class:`LadderEntry` children
    (one per rung, each an independent straddle sim) and THIS DayResult is the
    day-level AGGREGATE: its ``pnl`` is the sizing-WEIGHTED sum of the children
    (see :func:`tcg.engine.intraday_backtest.aggregate_ladder_day`), while the
    per-rung detail lives on the children. The one-row-per-day aggregate shape is
    preserved so weekday/regime/event attribution keep working unchanged.
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
    # F4.1: per-rung children when this row is a laddered-day aggregate (else ()).
    entries: tuple["LadderEntry", ...] = ()


@dataclass(frozen=True)
class LadderEntry:
    """One rung of a laddered multi-entry day (F4.1).

    A rung is a fully INDEPENDENT straddle: it opens at ``entry_ts`` and runs the
    ordinary open -> hedge -> hold-to-settlement lifecycle, producing its own
    :class:`DayResult` (``result``) whose ``pnl`` is per ONE contract. ``contracts``
    is the sizing weight applied to this rung (the equal-contracts count, or the
    equal-notional scale computed from this rung's own entry premium) — so the
    rung's dollar contribution to the day is ``contracts * result.pnl.total_pnl_usd``
    and the day aggregate is the sum of those contributions. Rungs never interact
    (separate positions + hedge state; they share only the market bars), which is
    exactly why a laddered day is N single-straddle sims summed, not a rewrite.
    """

    entry_ts: datetime  # UTC target for this rung's entry
    contracts: float  # sizing weight (equal-contracts count | equal-notional scale)
    result: "DayResult"


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
    # Transaction-cost coverage (0 when the cost model is off): total cost
    # charged across all traded days and the count of fills that fell back to the
    # fixed per-side cost because their bar had no two-sided quote.
    total_cost_usd: float = 0.0
    n_fallback_fills: int = 0
