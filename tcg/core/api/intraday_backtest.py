"""Intraday options backtest router — ``/api/intraday-backtest``.

Endpoints (PINNED contract, see DESIGN.md §API):
* ``GET  /api/intraday-backtest/meta`` — window, expiry modes, roots, multiplier.
* ``POST /api/intraday-backtest/run``  — run the ATM-straddle + delta-hedge
  backtest over a date range and return per-day + aggregate P&L.

Boundaries: this core layer orchestrates async I/O (via
:class:`tcg.data._sql.intraday_v2.IntradayV2Reader`) and calls the PURE engine
(:mod:`tcg.engine.intraday_backtest`). Request/response models are Pydantic
mirrors of the engine's frozen dataclasses (types <- data/engine <- core).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from tcg.core.cache import DiskResultCache, canonical_hash
from tcg.data._sql.daily_series import DailySeriesReader
from tcg.data._sql.intraday_v2 import IntradayV2Reader
from tcg.engine.intraday_backtest import (
    aggregate_days,
    aggregate_ladder_day,
    parse_hhmm,
    resolve_et_to_utc,
    select_atm_strike,
    simulate_day,
    snap_nearest,
)
from tcg.engine.regime import (
    Decision,
    LevelGateSpec,
    realized_vol_by_date,
    resolve_regime_decisions,
)
from tcg.types.event_calendar import (
    EVENT_TYPES,
    all_event_dates,
    event_dates_for_types,
    event_days,
    tentative_days,
)
from tcg.types.intraday import (
    ES_MULTIPLIER,
    WINDOW_MAX_DATE,
    WINDOW_MIN_DATE,
    AggregateResult,
    CostModel,
    DayResult,
    HedgeSpec,
    HedgeTargetSpec,
    HedgeTimingSpec,
    HedgeTriggers,
    LadderEntry,
    MaxSpreadCond,
    MaxUnderlyingMoveCond,
    MinPremiumCond,
    MinQuoteSizeCond,
    MinRehedgeDeltaCond,
    NetDeltaTrigger,
    PnlTrigger,
    SigmaMoveHedgeTrigger,
    SigmaMoveTrigger,
    SkipNearExtremumSpec,
    UnderlyingMoveTrigger,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/intraday-backtest", tags=["intraday-backtest"])

_TZ = "America/New_York"
_EXPIRY_MODES = ["0DTE", "NDTE"]
# Session open (ET) used as the max_underlying_move "day_open" reference anchor.
_SESSION_OPEN_ET = "09:30"
# Tolerance (minutes) for snapping the day-open reference to the 09:30 session
# open. Covers the fetch buffer + a missing exact-09:30 print without letting an
# unrelated (much later) bar become the anchor.
_SESSION_OPEN_SNAP_TOL_MIN = 5.0


# --------------------------------------------------------------------------- #
# Conditional entry/exit modules (v2). Conditions are a discriminated union on
# ``type``; an unknown ``type`` (or ``ref``) is rejected by pydantic as 422.
# --------------------------------------------------------------------------- #
class MaxSpreadCondition(BaseModel):
    type: Literal["max_spread"]
    pct: float = Field(gt=0)
    min_ticks: float = Field(default=1.0, ge=0)


class MinQuoteSizeCondition(BaseModel):
    type: Literal["min_quote_size"]
    size: float = Field(gt=0)


class MinPremiumCondition(BaseModel):
    type: Literal["min_premium"]
    points: float = Field(gt=0)


class MaxUnderlyingMoveCondition(BaseModel):
    type: Literal["max_underlying_move"]
    pct: float = Field(gt=0)
    ref: Literal["day_open"] = "day_open"


Condition = Annotated[
    Union[
        MaxSpreadCondition,
        MinQuoteSizeCondition,
        MinPremiumCondition,
        MaxUnderlyingMoveCondition,
    ],
    Field(discriminator="type"),
]


def _to_engine_conditions(conds: list) -> list:
    """Mirror validated Pydantic conditions into engine dataclasses."""
    out: list = []
    for c in conds:
        if isinstance(c, MaxSpreadCondition):
            out.append(MaxSpreadCond(pct=c.pct, min_ticks=c.min_ticks))
        elif isinstance(c, MinQuoteSizeCondition):
            out.append(MinQuoteSizeCond(size=c.size))
        elif isinstance(c, MinPremiumCondition):
            out.append(MinPremiumCond(points=c.points))
        elif isinstance(c, MaxUnderlyingMoveCondition):
            out.append(MaxUnderlyingMoveCond(pct=c.pct, ref=c.ref))
    return out


# --------------------------------------------------------------------------- #
# Early-exit TRIGGERS (v3). A discriminated union on ``type``; attached to the
# EXIT module only (entry has none — enforced by a RunRequest validator). The
# FIRST trigger to fire closes the straddle early; time exit is the backstop.
# Params validated positive; unknown type/unit/direction -> 422.
# --------------------------------------------------------------------------- #
class UnderlyingMoveTriggerModel(BaseModel):
    type: Literal["underlying_move"]
    amount: float = Field(gt=0)
    unit: Literal["points", "percent"] = "points"


class SigmaMoveTriggerModel(BaseModel):
    type: Literal["sigma_move"]
    n: float = Field(gt=0)


class NetDeltaTriggerModel(BaseModel):
    type: Literal["net_delta"]
    threshold: float = Field(gt=0)


class PnlTriggerModel(BaseModel):
    type: Literal["pnl"]
    amount: float = Field(gt=0)
    unit: Literal["points", "percent", "usd"] = "usd"
    direction: Literal["profit", "loss", "both"] = "both"


Trigger = Annotated[
    Union[
        UnderlyingMoveTriggerModel,
        SigmaMoveTriggerModel,
        NetDeltaTriggerModel,
        PnlTriggerModel,
    ],
    Field(discriminator="type"),
]


def _to_engine_triggers(triggers: list) -> list:
    """Mirror validated Pydantic triggers into engine dataclasses."""
    out: list = []
    for t in triggers:
        if isinstance(t, UnderlyingMoveTriggerModel):
            out.append(UnderlyingMoveTrigger(amount=t.amount, unit=t.unit))
        elif isinstance(t, SigmaMoveTriggerModel):
            out.append(SigmaMoveTrigger(n=t.n))
        elif isinstance(t, NetDeltaTriggerModel):
            out.append(NetDeltaTrigger(threshold=t.threshold))
        elif isinstance(t, PnlTriggerModel):
            out.append(PnlTrigger(amount=t.amount, unit=t.unit, direction=t.direction))
    return out


# --------------------------------------------------------------------------- #
# Hedge module (v4). Replaces the flat ``hedge:{enabled,interval_minutes,
# delta_band}`` with a configurable module: OR-ed rehedge TRIGGERS, AND-ed
# execution CONDITIONS (on the ES-future bar), and a delta-removal TARGET.
# The ``max_spread`` / ``min_quote_size`` condition models are reused; a new
# ``min_rehedge_delta`` completes the hedge-condition union. Unknown instrument /
# condition type / target mode -> 422. ``band_edge`` requires ``delta_band`` set.
# --------------------------------------------------------------------------- #
class MinRehedgeDeltaCondition(BaseModel):
    type: Literal["min_rehedge_delta"]
    threshold: float = Field(gt=0)


HedgeCondition = Annotated[
    Union[MaxSpreadCondition, MinQuoteSizeCondition, MinRehedgeDeltaCondition],
    Field(discriminator="type"),
]


class SigmaMoveHedgeTriggerModel(BaseModel):
    enabled: bool = False
    n: float = Field(default=1.0, gt=0)


class HedgeTriggersModel(BaseModel):
    # null/0 -> off (interval); null -> off (band). |drift| >= band; interval elapsed.
    interval_minutes: float | None = Field(default=15.0, ge=0)
    delta_band: float | None = Field(default=0.10, ge=0)
    sigma_move: SigmaMoveHedgeTriggerModel = Field(
        default_factory=SigmaMoveHedgeTriggerModel
    )


class HedgeTargetModel(BaseModel):
    mode: Literal["zero", "band_edge", "ratio"] = "zero"
    ratio: float = Field(default=1.0, gt=0, le=1.0)  # ratio in (0, 1]


# --------------------------------------------------------------------------- #
# Hedge-timing gates (W2/P1). Two session-relative knobs on the hedge module,
# BOTH neutral by default so an unset hedge behaves exactly as before:
#   F1.1 only_within_minutes_before_close — restrict hedging to the final N min.
#   F1.2 skip_near_extremum — skip a buy-high / sell-low hedge in the late window.
# --------------------------------------------------------------------------- #
class SkipNearExtremumModel(BaseModel):
    """F1.2. Default OFF => inert. ``window_minutes`` (>0) is how long before the
    hedge-window close the gate is active; ``tolerance`` (>=0) is proximity to the
    running extremum, in ES points or percent per ``tolerance_unit``."""

    enabled: bool = False
    window_minutes: float = Field(default=30.0, gt=0)
    tolerance: float = Field(default=2.0, ge=0)
    tolerance_unit: Literal["points", "percent"] = "points"


class HedgeTimingModel(BaseModel):
    """Session-relative hedge-timing gates. Fully neutral by default. F1.1's
    ``only_within_minutes_before_close`` (>0, or ``null`` for OFF) restricts
    hedging to the final N minutes before the hedge-window close."""

    only_within_minutes_before_close: float | None = Field(default=None, gt=0)
    skip_near_extremum: SkipNearExtremumModel = Field(
        default_factory=SkipNearExtremumModel
    )


class HedgeConfig(BaseModel):
    """The v4 hedge module. ``instrument`` is a ``Literal`` so any value other
    than ``es_future`` is rejected 422 (the selector exists for future
    instruments). ``band_edge`` target requires a ``delta_band`` trigger set.
    ``timing`` carries the F1.1/F1.2 hedge-timing gates (neutral by default)."""

    enabled: bool = True
    instrument: Literal["es_future"] = "es_future"
    triggers: HedgeTriggersModel = Field(default_factory=HedgeTriggersModel)
    conditions: list[HedgeCondition] = Field(default_factory=list)
    target: HedgeTargetModel = Field(default_factory=HedgeTargetModel)
    timing: HedgeTimingModel = Field(default_factory=HedgeTimingModel)

    @model_validator(mode="after")
    def _band_edge_needs_band(self) -> "HedgeConfig":
        if self.target.mode == "band_edge" and self.triggers.delta_band is None:
            raise ValueError(
                "target.mode 'band_edge' requires triggers.delta_band to be set"
            )
        return self


def _to_engine_hedge(h: HedgeConfig) -> HedgeSpec:
    """Mirror the validated Pydantic hedge module into the engine dataclass."""
    conds: list = []
    for c in h.conditions:
        if isinstance(c, MaxSpreadCondition):
            conds.append(MaxSpreadCond(pct=c.pct, min_ticks=c.min_ticks))
        elif isinstance(c, MinQuoteSizeCondition):
            conds.append(MinQuoteSizeCond(size=c.size))
        elif isinstance(c, MinRehedgeDeltaCondition):
            conds.append(MinRehedgeDeltaCond(threshold=c.threshold))
    return HedgeSpec(
        enabled=h.enabled,
        instrument=h.instrument,
        triggers=HedgeTriggers(
            interval_minutes=h.triggers.interval_minutes,
            delta_band=h.triggers.delta_band,
            sigma_move=SigmaMoveHedgeTrigger(
                enabled=h.triggers.sigma_move.enabled, n=h.triggers.sigma_move.n
            ),
        ),
        conditions=tuple(conds),
        target=HedgeTargetSpec(mode=h.target.mode, ratio=h.target.ratio),
        timing=HedgeTimingSpec(
            only_within_minutes_before_close=(
                h.timing.only_within_minutes_before_close
            ),
            skip_near_extremum=SkipNearExtremumSpec(
                enabled=h.timing.skip_near_extremum.enabled,
                window_minutes=h.timing.skip_near_extremum.window_minutes,
                tolerance=h.timing.skip_near_extremum.tolerance,
                tolerance_unit=h.timing.skip_near_extremum.tolerance_unit,
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Transaction-cost model (P0.2). A configurable, DEFAULT-OFF half-spread crossing
# on the option legs + ES hedge. As a normal RunRequest field it participates in
# the DiskResultCache key (a changed cost => a new key => a fresh compute), which
# is correct: cost changes the result. Default off keeps the mid-fill baseline.
# --------------------------------------------------------------------------- #
class CostModelConfig(BaseModel):
    """Half-spread transaction-cost knob. ``enabled=False`` (default) reproduces
    the prior mid-fill P&L. ``fallback_cost_pts`` (index points, >= 0) is the
    fixed per-side cost charged when a fill bar has no two-sided quote."""

    enabled: bool = False
    fallback_cost_pts: float = Field(default=0.0, ge=0)


def _to_engine_cost(c: CostModelConfig) -> CostModel:
    """Mirror the validated Pydantic cost model into the engine dataclass."""
    return CostModel(enabled=c.enabled, fallback_cost_pts=c.fallback_cost_pts)


# --------------------------------------------------------------------------- #
# Vol-regime SIGNAL provider (F2.1). Default-OFF. When ``emit_signals`` is on the
# per-day response carries realized-vol H20/H30/H100 (COMPUTED in the pure engine
# from IND_SP_500 daily closes via the P0.3 daily-series seam) plus VVIX
# passthrough. This is ONLY the signals — NO side-decision, NO thresholds (that is
# the separate F2.2 task). Generic so VIX1D (F2.3) is a pure drop-in: add one
# symbol field + one fetch; the per-date assembly loop never changes.
# --------------------------------------------------------------------------- #
class LevelGateModel(BaseModel):
    """A generic single-signal LEVEL gate (F2.2), VIX1D-ready (F2.3).

    ``enabled=False`` (default) makes the gate inert. When on, if the named
    ``signal`` (e.g. ``vvix``, ``vix1d``) is present in the as-of bundle and
    strictly ABOVE ``above``, the gate applies ``action`` (veto to ``flat`` OR
    force ``long``/``short``). Gates evaluate in list order, later overrides
    earlier — so adding a VIX1D bucket is a new list entry, no schema change.
    """

    enabled: bool = False
    signal: str = "vvix"
    above: float = Field(default=0.0, ge=0.0)
    action: Literal["long", "short", "flat"] = "flat"


class RegimeConfig(BaseModel):
    """Regime signal + side-decision knob. Two independent, DEFAULT-OFF halves:

    * ``emit_signals`` (F2.1): when on, each day carries the raw RV/VVIX signals.
    * ``side_mode`` (F2.2): ``"off"`` (default) trades the run-level
      ``straddle_side`` every day exactly as before; ``"regime_driven"`` resolves
      each day's side from the regime cascade (backwardation ladder + low-vol
      floor + level gates), a ``flat`` decision SKIPS the day.

    With BOTH halves off there is no extra dwh fetch and NO ``regime`` key on any
    day (response days byte-identical to the pre-feature baseline; the block is
    also stripped from the cache key). ``rv_windows`` are the realized-vol
    lookbacks (trading days, each >= 2); ``sp500_symbol`` is the daily close
    series RV is computed from and ``vvix_symbol`` the passthrough VVIX series —
    both dwh ``dim_instrument`` symbols read through the P0.3 generic seam.

    Decision knobs (only consulted when ``side_mode='regime_driven'``):
    ``hvol_tolerance`` (>=0) multiplicatively relaxes the strict RV ladder;
    ``extremely_low_h20`` (>=0, 0 disables) is the absolute short-window RV floor
    that forces ``flat``; ``gates`` is the ordered level-gate list (VVIX now,
    VIX1D later).
    """

    emit_signals: bool = False
    rv_windows: list[int] = Field(default_factory=lambda: [20, 30, 100])
    sp500_symbol: str = "IND_SP_500"
    vvix_symbol: str = "IND_VVIX"
    # --- F2.2 side-decision fields (all inert unless side_mode is regime_driven) #
    side_mode: Literal["off", "regime_driven"] = "off"
    hvol_tolerance: float = Field(default=0.0, ge=0.0)
    extremely_low_h20: float = Field(default=0.0, ge=0.0)
    gates: list[LevelGateModel] = Field(default_factory=list)

    @field_validator("rv_windows")
    @classmethod
    def _valid_windows(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("rv_windows must be non-empty")
        for w in v:
            if w < 2:
                raise ValueError(f"each rv_window must be >= 2 (got {w})")
        return v

    @model_validator(mode="after")
    def _ladder_needs_three_windows(self) -> "RegimeConfig":
        """The backwardation ladder is a 3-rung short/mid/long comparison, so a
        regime-DRIVEN side requires exactly three RV windows (reject otherwise so
        a mis-sized ladder surfaces as 422, never a 500 from the pure engine)."""
        if self.side_mode == "regime_driven" and len(self.rv_windows) != 3:
            raise ValueError(
                "side_mode 'regime_driven' requires exactly 3 rv_windows "
                f"(short/mid/long ladder); got {self.rv_windows}"
            )
        return self

    @property
    def is_active(self) -> bool:
        """True when the block affects output (a fetch/emit/decision is due).

        The single predicate the fetch trigger AND the cache-key strip agree on:
        the ``regime`` block is inert (stripped, no fetch, no ``regime`` key) iff
        BOTH halves are off. Any active half makes it participate.
        """
        return self.emit_signals or self.side_mode != "off"

    @property
    def rv_keys(self) -> tuple[str, ...]:
        """The RV ladder keys ``("h<w0>", ...)`` in ``rv_windows`` order."""
        return tuple(f"h{w}" for w in self.rv_windows)


class EntryExitModule(BaseModel):
    """A full entry- or exit-rule module: time + its own snap tolerance + an
    AND-ed list of conditions (the 4 discriminated types).

    ``triggers`` (v3) are EXIT-only early-exit rules; a RunRequest validator
    rejects non-empty entry triggers so they can never be silently ignored.
    """

    time: str = "10:00"
    snap_tolerance_minutes: float = Field(default=10.0, gt=0)
    conditions: list[Condition] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        parse_hhmm(v)  # raises ValueError -> 422 on bad "HH:MM"
        return v


class EntryExitOverride(BaseModel):
    """A PARTIAL entry/exit module for a ``custom_days`` per-date override: any
    field present overrides the global module for that day; absent inherits."""

    time: str | None = None
    snap_tolerance_minutes: float | None = Field(default=None, gt=0)
    conditions: list[Condition] | None = None
    triggers: list[Trigger] | None = None

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: str | None) -> str | None:
        if v is not None:
            parse_hhmm(v)
        return v


class AllowlistConfig(BaseModel):
    """Date-allowlist entry mode (F3.2). The DISTINCT OPPOSITE of ``custom_days``
    (which EXCLUDES days): the allowlist restricts trading to ONLY a chosen set of
    dates. DEFAULT ``mode='off'`` => every eligible weekday trades exactly as
    before (baseline byte-identical; the block is stripped from the cache key).

    When ``mode='allowlist'`` only the RESOLVED dates get a ``DayPlan`` — all other
    weekdays are skipped and NOT emitted. The resolved set is the UNION of the
    explicit ``dates`` and the curated dates of the selected ``event_types``
    (F3.1: FOMC / NFP / CPI). Either half may be empty; if BOTH are empty while
    active the resolved set is empty and no day trades (surfaced as a 400 "no
    trading days" like any empty range).

    Composition (documented order):
    1. The allowlist FILTERS which days are eligible (only resolved dates).
    2. ``custom_days`` exclude still removes from the allowlisted set (an excluded
       day that IS in the allowlist is emitted as ``status='excluded'``; a day NOT
       in the allowlist is simply never emitted — exclude on it is a harmless
       no-op).
    3. F2.2 regime side then decides the SIDE (long/short/flat) on the days that
       remain. The allowlist filters WHICH days; regime decides the side.

    The allowlist is a pure DATE FILTER (no look-ahead — it never inspects any
    market signal). Explicit ``dates`` outside the run range or on a weekend/
    holiday simply never match a plan (lenient filter semantics — distinct from
    ``custom_days``, which ASSERTS its dates are in-range weekdays).
    """

    mode: Literal["off", "allowlist"] = "off"
    dates: list[str] = Field(default_factory=list)
    event_types: list[Literal["FOMC", "NFP", "CPI"]] = Field(default_factory=list)

    @field_validator("dates")
    @classmethod
    def _valid_dates(cls, v: list[str]) -> list[str]:
        for s in v:
            try:
                date.fromisoformat(s)
            except ValueError as exc:
                raise ValueError(f"allowlist.dates: invalid date {s!r}") from exc
        return v

    @property
    def is_active(self) -> bool:
        """True when the allowlist restricts the traded day set (mode on)."""
        return self.mode == "allowlist"

    def resolved_dates(self) -> frozenset[date]:
        """The concrete allowed date set: explicit ``dates`` ∪ event-type dates.

        Pure (no dwh). Event-type dates come from the curated F3.1 calendar via
        the ``tcg.types.event_calendar`` seam. Called only when ``is_active``.
        """
        explicit = {date.fromisoformat(s) for s in self.dates}
        return frozenset(explicit | event_dates_for_types(self.event_types))


# --------------------------------------------------------------------------- #
# Laddered multi-entry (F4.1). DEFAULT-OFF. When enabled, a day opens a straddle
# at every rung of a fixed-interval ladder (first_entry, +interval, ... up to the
# cutoff) and HOLDS EACH TO SETTLEMENT (the same exit as the single-entry path).
# The rungs are independent single straddles summed at the day level (see the
# per-entry loop in ``_process_day``), NOT a concurrency rewrite. A ladder is a
# normal RunRequest block: ACTIVE => participates in the cache key + echo; OFF =>
# stripped so a default-off body hashes/echoes byte-identically to pre-feature.
# --------------------------------------------------------------------------- #
class LadderSizingModel(BaseModel):
    """Per-rung sizing. ``equal_contracts`` (default) gives every rung the same
    ``contracts`` count. ``equal_notional`` sizes each rung to a common premium
    NOTIONAL using THAT rung's own entry straddle price: ``weight_i =
    target_notional / (straddle_price_i * multiplier)``. ``target_notional`` is
    ``notional_per_entry_usd`` when > 0, else AUTO = ``contracts *`` the first
    traded rung's notional (i.e. "deploy the same premium dollars each rung as the
    first"), so the mode needs no mandatory dollar input. Weights may be
    fractional (a linear, zero-market-impact model — documented)."""

    mode: Literal["equal_contracts", "equal_notional"] = "equal_contracts"
    contracts: float = Field(default=1.0, gt=0)
    notional_per_entry_usd: float = Field(default=0.0, ge=0)


class LadderConfig(BaseModel):
    """Laddered multi-entry schedule + sizing (F4.1). Default OFF => exactly one
    entry per day at the entry module's time (baseline byte-identical).

    * ``interval_minutes`` (>= 1) — spacing between rungs.
    * ``first_entry`` — "HH:MM" ET of the FIRST rung; ``null`` => the entry
      module's time (per-day, so a ``custom_days`` entry override is honored).
    * ``last_entry_cutoff`` — "HH:MM" ET of the LATEST allowed rung; ``null`` =>
      the exit time. Rungs are generated at ``first, first+interval, ...`` while
      ``<= cutoff`` AND strictly before the exit (every straddle must have a
      settlement after its entry). "Last entry X min before close" = set this to
      (exit - X).
    * ``max_concurrent`` (>= 0, 0 = unlimited) — the cap on straddles open at
      once. Because every rung is HELD TO SETTLEMENT, an open straddle never
      closes intraday, so the count only grows: this effectively caps the number
      of rungs that OPEN per day at ``max_concurrent`` (a rung that skips on a
      data gap consumes NO slot). Enforced at open time in ``_process_day``.
    * ``sizing`` — equal-contracts vs equal-notional (see LadderSizingModel).
    """

    enabled: bool = False
    interval_minutes: float = Field(default=30.0, ge=1.0)
    first_entry: str | None = None
    last_entry_cutoff: str | None = None
    max_concurrent: int = Field(default=0, ge=0)
    sizing: LadderSizingModel = Field(default_factory=LadderSizingModel)

    @field_validator("first_entry", "last_entry_cutoff")
    @classmethod
    def _valid_time(cls, v: str | None) -> str | None:
        if v is not None:
            parse_hhmm(v)  # raises ValueError -> 422 on bad "HH:MM"
        return v

    @property
    def is_active(self) -> bool:
        """True when the ladder changes output (enabled). The single predicate the
        cache-key strip and the echo strip agree on."""
        return self.enabled


class CustomDay(BaseModel):
    """A single per-date control (unified exclude + full per-day override).

    ``exclude=True`` -> the day is NOT traded; it is still emitted in the
    response ``days`` array with status ``"excluded"`` (calendar shows it as an
    intentional skip). ``exclude`` WINS over any override. Otherwise a present
    ``entry`` / ``exit`` partial module overrides the global module for that
    date, field by field; absent fields inherit the global default.
    """

    date: str
    exclude: bool = False
    entry: EntryExitOverride | None = None
    exit: EntryExitOverride | None = None


class RunRequest(BaseModel):
    start_date: str
    end_date: str
    # v2: entry/exit are rule MODULES (time + snap tolerance + conditions),
    # superseding the flat entry_time / exit_time / snap_tolerance_minutes.
    entry: EntryExitModule = Field(default_factory=EntryExitModule)
    exit: EntryExitModule = Field(
        default_factory=lambda: EntryExitModule(time="15:45")
    )
    expiry_mode: Literal["0DTE", "NDTE"] = "0DTE"
    dte: int = Field(default=0, ge=0)
    straddle_side: Literal["long", "short"] = "long"
    hedge: HedgeConfig = Field(default_factory=HedgeConfig)
    # Transaction-cost model (P0.2). Default OFF => mid fills (prior behavior).
    # A real field (NOT stripped from the cache key): cost changes the result.
    cost: CostModelConfig = Field(default_factory=CostModelConfig)
    # Vol-regime signal provider (F2.1). Default OFF => no extra fetch, no
    # ``regime`` field on any day (baseline preserved). When ON it participates
    # in the cache key; when OFF it is stripped so an off-request hashes
    # identically regardless of its (inert) sub-config (see _intraday_cache_key).
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    # Date-allowlist entry mode (F3.2). Default OFF => every eligible day trades
    # (baseline byte-identical; stripped from the cache key when inert). When on,
    # only the resolved dates (explicit ∪ event-type dates) get a DayPlan.
    allowlist: AllowlistConfig = Field(default_factory=AllowlistConfig)
    # Laddered multi-entry (F4.1). Default OFF => one entry/day (baseline
    # byte-identical; stripped from the cache key + echo when inert). When on,
    # a day opens a straddle at each rung and holds each to settlement.
    ladder: LadderConfig = Field(default_factory=LadderConfig)
    # Unified per-date control (exclude + full per-day override).
    custom_days: list[CustomDay] = Field(default_factory=list)
    # Durable-cache opt-out (Settings parity with the portfolio path). It selects
    # WHETHER to consult the on-disk result cache, never WHICH result a body maps
    # to, so it is STRIPPED from the cache key (``_intraday_cache_key``) — a
    # pre-feature payload without the field hashes identically. use_cache=False =>
    # no read, no write, always a fresh compute.
    use_cache: bool = True

    @model_validator(mode="after")
    def _no_entry_triggers(self) -> "RunRequest":
        """Triggers are EXIT-only (DESIGN v3): reject them on any entry module so
        they can never be silently ignored."""
        if self.entry.triggers:
            raise ValueError("entry module does not support triggers (exit-only)")
        for cd in self.custom_days:
            if cd.entry is not None and cd.entry.triggers:
                raise ValueError(
                    f"custom_days[{cd.date}].entry does not support triggers (exit-only)"
                )
        return self


@dataclass
class DayPlan:
    """Resolved per-day trading plan (pure; no DB).

    ``entry_ts`` is the day's FIRST (or only) entry — kept for the single-entry
    path + the window/day-open anchor. ``entry_tss`` is the full laddered entry
    schedule (F4.1): ``[entry_ts]`` when the ladder is off, else the rungs in
    ascending time order (all sharing the one ``exit_ts`` settlement, ``entry_tol``
    and entry conditions). The exit is the common settlement for every rung.
    """

    day: date
    date_int: int
    entry_ts: datetime  # UTC (first/only entry)
    exit_ts: datetime  # UTC
    entry_tol: float
    exit_tol: float
    entry_conditions: list  # engine condition dataclasses
    exit_conditions: list
    exit_triggers: list = field(default_factory=list)  # engine trigger dataclasses
    excluded: bool = False
    entry_tss: list[datetime] = field(default_factory=list)  # laddered entries (UTC)


# --------------------------------------------------------------------------- #
# Pure validation / day resolution (unit-tested without a DB)
# --------------------------------------------------------------------------- #
def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field}: invalid date {value!r}") from exc


# Defensive cap on rungs/day: a 1-minute ladder over a full session is ~390
# entries — legitimate but bounded. A schedule beyond this is a mis-config and is
# rejected 400 (loud) rather than silently expanding the compute unboundedly.
_MAX_LADDER_ENTRIES_PER_DAY = 500


def _ladder_entry_times(
    ladder: LadderConfig, day: date, e_time: str, x_time: str
) -> list[datetime]:
    """The laddered entry timestamps (UTC) for one day, ascending.

    Ladder OFF => ``[entry-time]`` (the single-entry baseline). ON => rungs at
    ``first, first+interval, ...`` while ``<= cutoff`` AND strictly before the
    exit settlement. ``first`` defaults to the (per-day) entry time and ``cutoff``
    to the exit time. The ET->UTC offset is constant across a trading session, so
    interior rungs are the first UTC time plus integer multiples of the interval
    (DST-safe; fractional-minute intervals supported). Raises ``HTTPException``
    400 if an active ladder yields no rung before the exit, or exceeds the
    per-day cap."""
    first_str = ladder.first_entry or e_time
    first_ts = resolve_et_to_utc(day, first_str, _TZ)
    if not ladder.enabled:
        return [first_ts]

    cutoff_str = ladder.last_entry_cutoff or x_time
    cutoff_ts = resolve_et_to_utc(day, cutoff_str, _TZ)
    exit_ts = resolve_et_to_utc(day, x_time, _TZ)
    step = timedelta(minutes=ladder.interval_minutes)

    rungs: list[datetime] = []
    k = 0
    while True:
        t = first_ts + step * k
        if t > cutoff_ts or t >= exit_ts:
            break
        rungs.append(t)
        k += 1
        if len(rungs) > _MAX_LADDER_ENTRIES_PER_DAY:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{day.isoformat()}: laddered schedule exceeds "
                    f"{_MAX_LADDER_ENTRIES_PER_DAY} entries "
                    f"(interval_minutes={ladder.interval_minutes} too small)"
                ),
            )
    if not rungs:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{day.isoformat()}: laddered schedule produced no entry before "
                f"the exit (first_entry {first_str} / cutoff {cutoff_str} / "
                f"exit {x_time})"
            ),
        )
    return rungs


def resolve_day_plans(req: RunRequest) -> list[DayPlan]:
    """Validate the request and expand it to a per-weekday plan (UTC times).

    Raises ``HTTPException(400)`` on: bad dates, empty/inverted range,
    out-of-window dates, or any day whose exit time is not after its entry time.
    Holidays are not filtered here — a day with no market data is SKIPPED later
    by the snap/skip rule.
    """
    start = _parse_date(req.start_date, "start_date")
    end = _parse_date(req.end_date, "end_date")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")
    if start < WINDOW_MIN_DATE or end > WINDOW_MAX_DATE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"dates out of intraday window "
                f"[{WINDOW_MIN_DATE.isoformat()}..{WINDOW_MAX_DATE.isoformat()}]"
            ),
        )

    # F3.2 date-allowlist: when active, restrict the eligible day set to the
    # resolved dates (explicit ∪ event-type dates). A pure DATE FILTER applied
    # BEFORE exclude/override; a non-allowlisted weekday gets no DayPlan at all.
    # Resolved up front so the custom_days loop can validate the exclude/allowlist
    # interaction (the W4 fold-in below).
    allow_active = req.allowlist.is_active
    allowed_dates = req.allowlist.resolved_dates() if allow_active else frozenset()

    # Fold custom_days into an excluded-set + a per-date override map.
    # exclude:true wins (override ignored); otherwise the present entry/exit
    # partial module overrides the global module for that date, field by field.
    excluded: set[date] = set()
    overrides: dict[date, tuple[EntryExitOverride | None, EntryExitOverride | None]] = {}
    for cd in req.custom_days:
        cd_date = _parse_date(cd.date, "custom_days")
        # A custom_day (exclude OR override) MUST reference a trading day inside
        # the range: the plan loop only emits weekdays in [start,end], so a
        # weekend/out-of-range custom_day would otherwise be silently dropped
        # (neither applied, nor emitted as "excluded", nor flagged). Reject it
        # 400 so a typo (e.g. excluding a Saturday) surfaces instead of the day
        # being traded normally. Contract: excluded days are always emitted.
        if not (start <= cd_date <= end):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"custom_days: {cd.date} is outside the range "
                    f"[{start.isoformat()}..{end.isoformat()}]"
                ),
            )
        if cd_date.weekday() >= 5:
            raise HTTPException(
                status_code=400,
                detail=f"custom_days: {cd.date} is not a weekday (no trading day)",
            )
        # W4 fold-in (review finding 1): when an allowlist is ACTIVE, a
        # custom_days EXCLUDE that names a NON-allowlisted day is contradictory —
        # that day gets no DayPlan at all, so the "excluded days are always
        # emitted" contract would be silently broken (the exclude is a no-op).
        # Reject it 400 so the allowlist/exclude interaction is explicit rather
        # than silently swallowed. (An exclude that IS in the allowlist still
        # works: the day is emitted with status "excluded".)
        if cd.exclude and allow_active and cd_date not in allowed_dates:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"custom_days: {cd.date} is excluded but is not in the active "
                    f"allowlist (it would never be emitted); remove the exclude or "
                    f"add the date to the allowlist"
                ),
            )
        if cd.exclude:
            excluded.add(cd_date)
            continue
        if cd.entry is not None or cd.exit is not None:
            overrides[cd_date] = (cd.entry, cd.exit)

    plans: list[DayPlan] = []
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5 and (not allow_active or d in allowed_dates):  # Mon-Fri
            e_ov, x_ov = overrides.get(d, (None, None))
            e_time, e_tol, e_conds, _e_trigs = _resolve_module(req.entry, e_ov)
            x_time, x_tol, x_conds, x_trigs = _resolve_module(req.exit, x_ov)
            # F4.1: the laddered entry schedule (``[entry-time]`` when off). The
            # FIRST rung is the day's ``entry_ts`` (window/day-open anchor); the
            # exit must be after it (existing single-entry invariant, and every
            # later rung is < exit by construction).
            entry_tss = _ladder_entry_times(req.ladder, d, e_time, x_time)
            entry_ts = entry_tss[0]
            exit_ts = resolve_et_to_utc(d, x_time, _TZ)
            if exit_ts <= entry_ts:
                raise HTTPException(
                    status_code=400,
                    detail=f"{d.isoformat()}: exit time must be after entry time",
                )
            plans.append(
                DayPlan(
                    day=d,
                    date_int=d.year * 10000 + d.month * 100 + d.day,
                    entry_ts=entry_ts,
                    exit_ts=exit_ts,
                    entry_tol=e_tol,
                    exit_tol=x_tol,
                    entry_conditions=_to_engine_conditions(e_conds),
                    exit_conditions=_to_engine_conditions(x_conds),
                    exit_triggers=_to_engine_triggers(x_trigs),
                    excluded=d in excluded,
                    entry_tss=entry_tss,
                )
            )
        d += one
    if not plans:
        detail = (
            "no trading days in range after applying the date allowlist"
            if allow_active
            else "no trading days in range"
        )
        raise HTTPException(status_code=400, detail=detail)
    return plans


def _resolve_module(
    base: EntryExitModule, ov: EntryExitOverride | None
) -> tuple[str, float, list, list]:
    """Merge a global module with an optional per-day partial override.

    Any override field that is present wins; absent fields inherit ``base``.
    Returns ``(time, snap_tol, conditions, triggers)``.
    """
    if ov is None:
        return base.time, base.snap_tolerance_minutes, base.conditions, base.triggers
    time_ = ov.time if ov.time is not None else base.time
    tol = (
        ov.snap_tolerance_minutes
        if ov.snap_tolerance_minutes is not None
        else base.snap_tolerance_minutes
    )
    conds = ov.conditions if ov.conditions is not None else base.conditions
    trigs = ov.triggers if ov.triggers is not None else base.triggers
    return time_, tol, conds, trigs


def _resolve_es_day_open(
    es_bars: list, entry_ts: datetime, session_open: datetime
) -> float | None:
    """Day-open ES reference for ``max_underlying_move``.

    Anchored to the 09:30 session open, but CLAMPED so the anchor can never sit
    in the FUTURE relative to the entry: ``anchor = min(entry_ts, session_open)``.

    * Normal case (entry >= open): ``anchor = session_open`` — the day-open
      reference is the 09:30 session open (nearest bar within
      ``_SESSION_OPEN_SNAP_TOL_MIN``), NOT the first fetched bar (~09:28 — the
      buffered window start, 2 min early).
    * Pre-open entry (e.g. 09:00, entry < open): ``anchor = entry_ts`` — the
      move is measured from the entry itself, never against a still-in-the-future
      09:30 price (no look-ahead; entry time is validated for format only, with
      no lower bound, so this degenerate case is reachable).

    Falls back to the first bar only if no bar sits near the anchor at all
    (sparse/holiday data).
    """
    anchor = min(entry_ts, session_open)
    anchor_bar = snap_nearest(es_bars, anchor, _SESSION_OPEN_SNAP_TOL_MIN)
    if anchor_bar is not None:
        return anchor_bar.price
    return es_bars[0].price if es_bars else None


def _pick_expiry(all_exps: list[date], day: date, mode: str, dte: int) -> date | None:
    """Resolve the target expiry for a day per ``expiry_mode`` (pure)."""
    if mode == "0DTE":
        return day if day in all_exps else None
    # NDTE: nearest expiry with days_to_exp >= dte.
    candidates = [e for e in all_exps if (e - day).days >= dte and e >= day]
    return min(candidates) if candidates else None


# --------------------------------------------------------------------------- #
# Vol-regime signal assembly (F2.1). PURE join here (unit-testable, no dwh); the
# async fetch that feeds it is ``_fetch_regime_signals`` below. Boundary: RV math
# is in the pure ENGINE (``tcg.engine.regime``); the FETCH via the P0.3 data
# reader + this join live in CORE — engine never imports data.
# --------------------------------------------------------------------------- #
def build_regime_signal_map(
    day_dates: list[int],
    rv_by_date: dict[int, dict[str, float | None]],
    passthrough_by_name: dict[str, dict[int, float]],
    windows: list[int],
) -> dict[int, dict[str, float | None]]:
    """Per-day regime-signal map: ``{date_int: {"h20":.., "vvix":.., ...}}``.

    A pure join of the computed RV signals (``rv_by_date`` keyed ``h<w>``) with
    each passthrough series (VVIX now, VIX1D later) onto the backtest ``day_dates``
    — a date missing from a series carries ``None`` for that signal (never a
    fabricated value). Adding VIX1D is a NEW entry in ``passthrough_by_name`` with
    NO change to this loop (the structural drop-in the F2.1 brief requires).
    """
    rv_keys = [f"h{w}" for w in windows]
    out: dict[int, dict[str, float | None]] = {}
    for d in day_dates:
        di = int(d)
        rv = rv_by_date.get(di) or {}
        sig: dict[str, float | None] = {k: rv.get(k) for k in rv_keys}
        for name, series in passthrough_by_name.items():
            sig[name] = series.get(di)
        out[di] = sig
    return out


def _null_regime_signals(
    day_dates: list[int], windows: list[int], passthrough_names: tuple[str, ...]
) -> dict[int, dict[str, float | None]]:
    """An all-``None`` regime map (same keys as a real one) for the degradation
    path — emit_signals is ON but the daily series could not be read. Keeps the
    response shape valid rather than crashing a good options backtest."""
    rv_keys = [f"h{w}" for w in windows]
    keys = rv_keys + list(passthrough_names)
    return {int(d): {k: None for k in keys} for d in day_dates}


async def _fetch_regime_series(
    daily_reader: DailySeriesReader,
    req: RunRequest,
    day_dates: list[int],
) -> tuple[dict[int, dict[str, float | None]], dict[str, dict[int, float]], list[int]]:
    """Fetch the raw daily series once; return ``(rv_by_date, passthrough, windows)``.

    Fetches IND_SP_500 AND VVIX with a lookback long enough to (a) warm up the
    largest RV window and (b) cover the daily close of the day BEFORE the first
    backtest day, so the no-look-ahead as-of resolver always has a prior signal
    for day one. Computes RV in the pure engine. VIX1D DROP-IN (F2.3) is exactly:
    add a ``vix1d_symbol`` to :class:`RegimeConfig`, one more ``read_series``
    call, and one ``passthrough["vix1d"] = ...`` line — nothing else changes.
    """
    reg = req.regime
    windows = reg.rv_windows
    start = date.fromisoformat(_int_to_iso(min(day_dates)))
    end = date.fromisoformat(_int_to_iso(max(day_dates)))
    # Calendar lookback covering the largest window in trading days plus slack
    # for weekends/holidays, so the first backtest day already has full history
    # AND a prior daily close exists for the as-of decision.
    lookback = timedelta(days=max(windows) * 2 + 30)

    sp = await daily_reader.read_series(reg.sp500_symbol, start=start - lookback, end=end)
    rv_by_date = realized_vol_by_date(sp.dates, sp.values, windows)

    vvix = await daily_reader.read_series(
        reg.vvix_symbol, start=start - lookback, end=end
    )
    passthrough: dict[str, dict[int, float]] = {
        "vvix": dict(zip(vvix.dates, vvix.values))
    }
    return rv_by_date, passthrough, windows


def _full_daily_signal_map(
    rv_by_date: dict[int, dict[str, float | None]],
    passthrough_by_name: dict[str, dict[int, float]],
    windows: list[int],
) -> dict[int, dict[str, float | None]]:
    """A per-DAILY-DATE signal map over the UNION of all fetched dates.

    Unlike :func:`build_regime_signal_map` (which restricts to the backtest days
    for DISPLAY), this covers every daily date the series carry — the domain the
    no-look-ahead as-of resolver must pick from so day D's ``asof`` is the true
    latest prior daily close, not merely the previous backtest day.
    """
    rv_keys = [f"h{w}" for w in windows]
    all_dates: set[int] = set(rv_by_date)
    for series in passthrough_by_name.values():
        all_dates |= set(series)
    out: dict[int, dict[str, float | None]] = {}
    for di in all_dates:
        rv = rv_by_date.get(di) or {}
        sig: dict[str, float | None] = {k: rv.get(k) for k in rv_keys}
        for name, series in passthrough_by_name.items():
            sig[name] = series.get(di)
        out[di] = sig
    return out


def _to_engine_gates(gates: list[LevelGateModel]) -> tuple[LevelGateSpec, ...]:
    """Mirror the validated Pydantic level gates into engine dataclasses."""
    return tuple(
        LevelGateSpec(enabled=g.enabled, signal=g.signal, above=g.above, action=g.action)
        for g in gates
    )


async def _fetch_regime_signals(
    daily_reader: DailySeriesReader,
    req: RunRequest,
    day_dates: list[int],
) -> dict[int, dict[str, float | None]]:
    """F2.1 DISPLAY map: per-backtest-day raw RV/VVIX signals (side_mode off)."""
    rv_by_date, passthrough, windows = await _fetch_regime_series(
        daily_reader, req, day_dates
    )
    return build_regime_signal_map(day_dates, rv_by_date, passthrough, windows)


async def _resolve_regime_side_decisions(
    daily_reader: DailySeriesReader | None,
    req: RunRequest,
    trading_dates: list[int],
) -> dict[int, Decision]:
    """Per-day regime SIDE decisions for the (non-excluded) trading days (F2.2).

    Fetches the daily signals through the P0.3 seam, builds the FULL daily signal
    map, and runs the NO-LOOK-AHEAD as-of resolver so each day's side is decided
    as-of the latest daily close STRICTLY BEFORE it. A fetch glitch (or no reader
    wired) DEGRADES to an all-fallback resolution — every day trades the static
    ``straddle_side`` (state ``fallback``), NEVER a silent skip and never a crash
    of a good options backtest (blocked > broken).
    """
    reg = req.regime
    rv_keys = list(reg.rv_keys)
    gates = _to_engine_gates(reg.gates)

    signals_by_date: dict[int, dict[str, float | None]] = {}
    passthrough_names: tuple[str, ...] = ("vvix",)
    if daily_reader is not None and trading_dates:
        try:
            rv_by_date, passthrough, windows = await _fetch_regime_series(
                daily_reader, req, trading_dates
            )
            signals_by_date = _full_daily_signal_map(rv_by_date, passthrough, windows)
            passthrough_names = tuple(passthrough)
        except Exception:  # noqa: BLE001 — a signal-fetch glitch must not fail a good backtest
            logger.exception(
                "regime side-decision fetch failed; falling back to the static side"
            )
            signals_by_date = {}

    signal_names = tuple(rv_keys) + passthrough_names
    return resolve_regime_decisions(
        trading_dates,
        signals_by_date,
        static_side=req.straddle_side,
        rv_keys=rv_keys,
        extremely_low_h20=reg.extremely_low_h20,
        hvol_tolerance=reg.hvol_tolerance,
        gates=gates,
        signal_names=signal_names,
    )


# --------------------------------------------------------------------------- #
# Serialization (frozen dataclass -> wire dict)
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _serialize_leg(leg: Any) -> dict[str, Any]:
    return {
        "entry_ts": _iso(leg.entry_ts),
        "entry_price": leg.entry_price,
        "exit_ts": _iso(leg.exit_ts),
        "exit_price": leg.exit_price,
        "exit_conditions_met": leg.exit_conditions_met,
        "pnl_pts": leg.pnl_pts,
    }


def _serialize_decision(dec: Decision) -> dict[str, Any]:
    """Wire form of a per-day regime DECISION (F2.2 readout — the WHY).

    Surfaces the resolved ``state`` (hvol_on/hvol_off/extremely_low/fallback),
    the chosen ``side`` (long/short/flat), the ``asof`` signal date the decision
    was taken as-of (``null`` => no prior close, static fallback), the ``gate``
    that vetoed/adjusted (``null`` if none), and the exact AS-OF ``signals`` the
    decision consumed — so the user and A2 can see why each day is long/short/flat.
    """
    return {
        "state": dec.state,
        "side": dec.side,
        "asof": dec.asof,
        "gate": dec.gate,
        "signals": dict(dec.signals),
    }


def _serialize_day(
    r: DayResult,
    regime_by_date: dict[int, dict[str, float | None]] | None = None,
    decision_by_date: dict[int, Decision] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "date": _int_to_iso(r.date),
        "status": r.status,
        "skip_reason": r.skip_reason,
        "expiry": r.expiry.isoformat() if r.expiry else None,
        "strike": r.strike,
        "entry": None,
        "exit": None,
        "hedge_trades": [],
        "pnl": None,
        "legs": None,
        "straddle_on_ts": _iso(r.straddle_on_ts) if r.straddle_on_ts else None,
        "straddle_off_ts": _iso(r.straddle_off_ts) if r.straddle_off_ts else None,
        "exit_trigger": (
            {
                "type": r.exit_trigger.type,
                "ts": _iso(r.exit_trigger.ts),
                "value": r.exit_trigger.value,
            }
            if r.exit_trigger
            else None
        ),
    }
    if r.legs:
        out["legs"] = {
            "call": _serialize_leg(r.legs.call),
            "put": _serialize_leg(r.legs.put),
        }
    if r.entry:
        out["entry"] = {
            "ts": _iso(r.entry.ts),
            "underlying": r.entry.underlying,
            "call_mid": r.entry.call_mid,
            "put_mid": r.entry.put_mid,
            "straddle_price": r.entry.straddle_price,
        }
    if r.exit:
        out["exit"] = {
            "ts": _iso(r.exit.ts),
            "underlying": r.exit.underlying,
            "call_mid": r.exit.call_mid,
            "put_mid": r.exit.put_mid,
            "straddle_price": r.exit.straddle_price,
        }
    out["hedge_trades"] = [
        {
            "ts": _iso(h.ts),
            "underlying": h.underlying,
            "net_delta": h.net_delta,
            "hedge_qty": h.hedge_qty,
        }
        for h in r.hedge_trades
    ]
    if r.pnl:
        out["pnl"] = {
            "option_pnl_pts": r.pnl.option_pnl_pts,
            "hedge_pnl_pts": r.pnl.hedge_pnl_pts,
            "total_pnl_pts": r.pnl.total_pnl_pts,
            "total_pnl_usd": r.pnl.total_pnl_usd,
            "cost_pts": r.pnl.cost_pts,
            "cost_usd": r.pnl.cost_usd,
            "n_fallback_fills": r.pnl.n_fallback_fills,
        }
    # Regime block. When side_mode is regime_driven the day carries the DECISION
    # readout (state/side/asof/gate/as-of signals — the WHY). Else, when only
    # F2.1 emit_signals is on, the raw per-day signals. When BOTH are off,
    # ``decision_by_date`` and ``regime_by_date`` are None and NO key is added ->
    # the day dict is byte-identical to the pre-feature baseline (regression guard).
    if decision_by_date is not None:
        dec = decision_by_date.get(r.date)
        out["regime"] = _serialize_decision(dec) if dec is not None else None
    elif regime_by_date is not None:
        out["regime"] = regime_by_date.get(r.date)
    # F4.1: laddered days carry the per-rung children here. The KEY is added ONLY
    # when the day is laddered (``entries`` non-empty), so a single-entry (or
    # ladder-off) day dict is byte-identical to the pre-F4.1 baseline. The
    # day-level fields above ARE the retained one-row-per-day AGGREGATE the
    # weekday/regime/event views key on — unchanged in shape.
    if r.entries:
        out["entries"] = [_serialize_ladder_entry(le) for le in r.entries]
    return out


def _serialize_ladder_entry(le: LadderEntry) -> dict[str, Any]:
    """Wire form of one laddered rung (F4.1): the full per-entry straddle detail
    (via :func:`_serialize_day` on the child) PLUS the rung's ``entry_ts``, its
    sizing ``contracts`` weight, and ``weighted_pnl_usd`` = the rung's dollar
    CONTRIBUTION to the day (``contracts * total_pnl_usd``). By construction the
    day aggregate's ``total_pnl_usd`` == the sum of the rungs' ``weighted_pnl_usd``
    (unambiguous PnL aggregation)."""
    child = _serialize_day(le.result)
    wpnl = (
        le.contracts * le.result.pnl.total_pnl_usd
        if le.result.pnl is not None
        else 0.0
    )
    return {
        **child,
        "entry_ts": _iso(le.entry_ts),
        "contracts": le.contracts,
        "weighted_pnl_usd": wpnl,
    }


def _serialize_aggregate(a: AggregateResult) -> dict[str, Any]:
    return {
        "n_days": a.n_days,
        "n_traded": a.n_traded,
        "n_skipped": a.n_skipped,
        "total_pnl_usd": a.total_pnl_usd,
        "mean_daily_pnl_usd": a.mean_daily_pnl_usd,
        "win_rate": a.win_rate,
        "sharpe": a.sharpe,
        "max_drawdown_usd": a.max_drawdown_usd,
        "total_cost_usd": a.total_cost_usd,
        "n_fallback_fills": a.n_fallback_fills,
        "equity_curve": [
            {"date": _int_to_iso(p.date), "cum_pnl_usd": p.cum_pnl_usd}
            for p in a.equity_curve
        ],
    }


def _int_to_iso(date_int: int) -> str:
    s = str(date_int)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _warnings(results: list[DayResult]) -> list[str]:
    reasons: dict[str, int] = {}
    for r in results:
        # ``regime_flat`` is an INTENTIONAL regime decision (side=flat), not a
        # data-quality skip — surfaced via the per-day regime readout, never as a
        # warning (same spirit as an exclude).
        if r.status == "skipped" and r.skip_reason and r.skip_reason != "regime_flat":
            reasons[r.skip_reason] = reasons.get(r.skip_reason, 0) + 1
    # NB: excluded days carry status "excluded" (not "skipped") and are
    # intentional — they never generate a warning here.
    labels = {
        "no_quote_within_tolerance": "no quote within tolerance",
        "entry_conditions_unmet": "entry conditions unmet",
        "no_expiry": "no matching expiry",
        "no_contract": "no ATM contract",
    }
    return [
        f"{n} day(s) skipped: {labels.get(reason, reason)}"
        for reason, n in sorted(reasons.items())
    ]


# --------------------------------------------------------------------------- #
# Orchestration (async I/O + pure engine)
# --------------------------------------------------------------------------- #
def count_trading_days(req: RunRequest) -> int:
    """Authoritative progress denominator: resolved weekdays, exceptions removed.

    Validates the request (raises ``HTTPException(400)`` on bad input, exactly
    like :func:`resolve_day_plans`) so the async endpoint can reject bad
    requests *before* creating a job, and pins ``total_days`` up front.
    """
    return sum(1 for p in resolve_day_plans(req) if not p.excluded)


async def _simulate_entry(
    reader: IntradayV2Reader,
    req: RunRequest,
    plan: DayPlan,
    *,
    entry_ts: datetime,
    expiry: date,
    exp_to_objs: dict[date, list[int]],
    es_bars: list,
    session_open: datetime,
    win_start: datetime,
    win_end: datetime,
    tick_size: float,
    es_tick: float,
    side: str,
) -> DayResult:
    """Run ONE straddle's full open->hedge->settlement lifecycle at ``entry_ts``.

    The per-entry unit reused by BOTH the single-entry path and each laddered
    rung (F4.1). The ES bars + expiry + window are the DAY's shared context; the
    ATM strike + option contracts + marks are picked PER ENTRY at this rung's own
    ES level (so a 10:00 rung and a 14:00 rung can be different strikes). Cost
    (P0.2) and hedge timing (F1.1/F1.2) apply per entry via ``req``. Returns the
    per-entry DayResult (``skipped`` on no-quote / no-contract, else the sim)."""
    # ATM reference: ES nearest THIS entry target within the entry tolerance.
    es1 = snap_nearest(es_bars, entry_ts, plan.entry_tol)
    if es1 is None:
        return DayResult(
            date=plan.date_int,
            status="skipped",
            skip_reason="no_quote_within_tolerance",
            expiry=expiry,
        )

    chosen: tuple[int, float, int, int] | None = None
    for oid in exp_to_objs.get(expiry, []):
        strikes = await reader.list_strikes(oid, expiry)
        if not strikes:
            continue
        strike = select_atm_strike(es1.price, strikes)
        call_id = await reader.get_option_contract_id(oid, expiry, strike, "call")
        put_id = await reader.get_option_contract_id(oid, expiry, strike, "put")
        if call_id is not None and put_id is not None:
            chosen = (oid, strike, call_id, put_id)
            break
    if chosen is None:
        return DayResult(
            date=plan.date_int,
            status="skipped",
            skip_reason="no_contract",
            expiry=expiry,
        )

    _oid, strike, call_id, put_id = chosen
    call_marks = await reader.fetch_option_1m(call_id, win_start, win_end)
    put_marks = await reader.fetch_option_1m(put_id, win_start, win_end)
    # Anchor the day-open reference to the 09:30 session open, not es_bars[0]
    # (~09:28, the buffered window start), and clamp to min(entry, open) so a
    # pre-open entry can never reference a future 09:30 price (no look-ahead).
    es_day_open = _resolve_es_day_open(es_bars, entry_ts, session_open)

    return simulate_day(
        date_int=plan.date_int,
        side=side,
        strike=strike,
        expiry=expiry,
        es_bars=es_bars,
        call_marks=call_marks,
        put_marks=put_marks,
        entry_ts=entry_ts,
        exit_ts=plan.exit_ts,
        entry_tol=plan.entry_tol,
        exit_tol=plan.exit_tol,
        entry_conditions=plan.entry_conditions,
        exit_conditions=plan.exit_conditions,
        exit_triggers=plan.exit_triggers,
        tick_size=tick_size,
        hedge=_to_engine_hedge(req.hedge),
        es_tick=es_tick,
        es_day_open=es_day_open,
        multiplier=ES_MULTIPLIER,
        cost=_to_engine_cost(req.cost),
    )


def _ladder_weight_fn(sizing: "LadderSizingModel", rung_results: list):
    """Build the per-rung sizing-weight function (F4.1, pure).

    ``equal_contracts`` => a constant ``contracts`` weight. ``equal_notional`` =>
    ``target_notional / (straddle_price_i * multiplier)`` so each rung deploys the
    same premium dollars; ``target_notional`` is ``notional_per_entry_usd`` when
    > 0, else AUTO = ``contracts *`` the FIRST traded rung's notional. A rung that
    did not trade (no ``entry``/``pnl``) keeps a nominal ``contracts`` weight (it
    contributes nothing to the sum anyway); a degenerate zero-price notional maps
    to weight 0 (never inf)."""
    contracts = sizing.contracts
    if sizing.mode != "equal_notional":
        return lambda res: contracts

    mult = ES_MULTIPLIER
    first_notional: float | None = None
    for res in rung_results:
        if res.pnl is not None and res.entry is not None and res.entry.straddle_price > 0:
            first_notional = res.entry.straddle_price * mult
            break
    if sizing.notional_per_entry_usd > 0:
        target = sizing.notional_per_entry_usd
    elif first_notional is not None:
        target = contracts * first_notional
    else:
        target = 0.0

    def _w(res) -> float:
        if res.pnl is None or res.entry is None:
            return contracts
        notional_i = res.entry.straddle_price * mult
        return target / notional_i if notional_i > 0 else 0.0

    return _w


async def _process_day(
    reader: IntradayV2Reader,
    req: RunRequest,
    plan: DayPlan,
    all_exps: list[date],
    exp_to_objs: dict[date, list[int]],
    tick_size: float,
    es_tick: float,
    side: str,
) -> DayResult:
    """Fetch marks + simulate a single (non-excluded) trading day (async I/O).

    ``side`` is the resolved straddle side for THIS day: the run-level
    ``straddle_side`` when regime side-mode is off, else the per-day regime
    decision (never ``flat`` here — a flat day is skipped by the caller). The
    same side applies to EVERY laddered rung (F4.1: the per-day regime decision
    is made once for the day and shared).

    Ladder OFF => exactly one straddle (byte-identical to the pre-F4.1 path).
    Ladder ON => open a straddle at each rung in ``plan.entry_tss`` and hold each
    to settlement; the returned DayResult is the day AGGREGATE (weighted sum of
    the rungs) carrying the per-rung children on ``entries``.
    """
    expiry = _pick_expiry(all_exps, plan.day, req.expiry_mode, req.dte)
    if expiry is None:
        return DayResult(date=plan.date_int, status="skipped", skip_reason="no_expiry")

    # Window: from the session open (09:30 ET, the max_underlying_move day-open
    # reference) or the FIRST entry, whichever is earlier, out to the exit-scan
    # horizon (exit target + its snap tolerance) — plus a small buffer both sides.
    # The ES bars are fetched ONCE and shared by every rung (rungs share only the
    # market bars, never position/hedge state — the key F4.1 reuse insight).
    buf = timedelta(minutes=2.0)
    session_open = resolve_et_to_utc(plan.day, _SESSION_OPEN_ET, _TZ)
    win_start = min(plan.entry_ts, session_open) - buf
    win_end = plan.exit_ts + timedelta(minutes=plan.exit_tol) + buf
    es_bars = await reader.fetch_es_future_1m(win_start, win_end, on_or_after=plan.day)

    entry_tss = plan.entry_tss or [plan.entry_ts]

    # Ladder OFF (or a degenerate single rung with the ladder disabled): the day
    # IS one straddle — return it directly so the result is bit-identical to the
    # pre-F4.1 single-entry path (no aggregate wrapper, no ``entries`` key).
    if not req.ladder.enabled:
        return await _simulate_entry(
            reader, req, plan,
            entry_ts=entry_tss[0], expiry=expiry, exp_to_objs=exp_to_objs,
            es_bars=es_bars, session_open=session_open,
            win_start=win_start, win_end=win_end,
            tick_size=tick_size, es_tick=es_tick, side=side,
        )

    # Ladder ON: run each rung independently, in time order, enforcing the
    # max-concurrent cap. Because every rung is HELD TO SETTLEMENT, an open
    # straddle never closes intraday, so ``currently_open`` only grows — the cap
    # therefore limits the number of rungs that OPEN per day (a rung that skips on
    # a data gap consumes no slot).
    max_concurrent = req.ladder.max_concurrent
    currently_open = 0
    rung_results: list[DayResult] = []
    for ts in entry_tss:
        if max_concurrent > 0 and currently_open >= max_concurrent:
            rung_results.append(
                DayResult(
                    date=plan.date_int,
                    status="skipped",
                    skip_reason="max_concurrent",
                    expiry=expiry,
                )
            )
            continue
        res = await _simulate_entry(
            reader, req, plan,
            entry_ts=ts, expiry=expiry, exp_to_objs=exp_to_objs,
            es_bars=es_bars, session_open=session_open,
            win_start=win_start, win_end=win_end,
            tick_size=tick_size, es_tick=es_tick, side=side,
        )
        rung_results.append(res)
        if res.status == "ok":
            currently_open += 1

    weight = _ladder_weight_fn(req.ladder.sizing, rung_results)
    entries = [
        LadderEntry(entry_ts=ts, contracts=weight(res), result=res)
        for ts, res in zip(entry_tss, rung_results)
    ]
    return aggregate_ladder_day(
        plan.date_int, entries, multiplier=ES_MULTIPLIER, expiry=expiry
    )


def _echo_params(req: RunRequest) -> dict[str, Any]:
    """The ``params_echo`` for the response, with INERT default-off feature blocks
    dropped so an off-run echo is not cluttered by pydantic default-fill.

    A ``regime`` block that is off (``is_active`` false) and an ``allowlist`` block
    that is off are omitted — so a run with neither feature echoes exactly like a
    pre-feature request (no ``regime`` / ``allowlist`` keys), while an ACTIVE block
    is echoed in full. This mirrors the cache-key strip (same ``is_active``
    predicate). The load-bearing ``days`` / ``pnl`` / ``aggregate`` output is
    unaffected (its byte-identity is a separate, already-held invariant).
    """
    echo = req.model_dump()
    if not req.regime.is_active:
        echo.pop("regime", None)
    if not req.allowlist.is_active:
        echo.pop("allowlist", None)
    if not req.ladder.is_active:
        echo.pop("ladder", None)
    return echo


async def run_backtest(
    reader: IntradayV2Reader,
    req: RunRequest,
    progress_cb: Callable[[int, int], None] | None = None,
    daily_reader: DailySeriesReader | None = None,
) -> dict[str, Any]:
    """Full run: resolve days, fetch marks per day, simulate, aggregate.

    ``progress_cb(days_done, total_days)`` — if given — is invoked once per
    *non-excluded* trading day as the loop advances (``days_done`` climbs from
    1 to ``total_days``). Excluded days are appended to the results but never
    tick progress: ``total_days`` is the exceptions-removed weekday count, the
    authoritative denominator shared with :func:`count_trading_days`.

    ``daily_reader`` (the P0.3 generic daily-series seam) is used ONLY when the
    ``regime`` block is active — ``emit_signals`` on (F2.1 per-day signals) OR
    ``side_mode='regime_driven'`` (F2.2 per-day side). When both are off it is
    never touched — no extra dwh fetch — and the response days are byte-identical
    to the pre-feature baseline.

    Per-day side (F2.2): with ``side_mode='regime_driven'`` each trading day's
    straddle side is resolved from the regime cascade as-of the PRIOR daily close
    (no look-ahead); a ``flat`` decision SKIPS the day (status ``skipped``, reason
    ``regime_flat``) exactly like an exclude — no fetch, no progress tick, not in
    ``total_days``. With ``side_mode='off'`` every day uses the run-level
    ``straddle_side`` exactly as before.
    """
    reg = req.regime
    plans = resolve_day_plans(req)
    start = plans[0].day

    # F2.2: resolve per-day sides BEFORE the loop (needs the as-of daily signals),
    # so ``flat`` days can be excluded from ``total_days`` and never fetched.
    decision_by_date: dict[int, Decision] | None = None
    if reg.side_mode == "regime_driven":
        trading_dates = [p.date_int for p in plans if not p.excluded]
        decision_by_date = await _resolve_regime_side_decisions(
            daily_reader, req, trading_dates
        )

    def _is_flat(plan: DayPlan) -> bool:
        if decision_by_date is None:
            return False
        dec = decision_by_date.get(plan.date_int)
        return dec is not None and dec.side == "flat"

    total_days = sum(1 for p in plans if not p.excluded and not _is_flat(p))

    roots = await reader.list_option_roots()
    option_object_ids = [int(r["object_id"]) for r in roots]
    exp_pairs = await reader.list_expirations(option_object_ids, start)
    exp_to_objs: dict[date, list[int]] = {}
    for oid, exp in exp_pairs:
        exp_to_objs.setdefault(exp, []).append(oid)
    all_exps = sorted(exp_to_objs)

    # Tick sizes for the max_spread 1-tick floor (sourced once per run):
    # option tick for the entry/exit conditions, ES-future tick for the hedge.
    tick_size = await reader.get_option_tick_size()
    es_tick = await reader.get_es_future_tick_size()

    results: list[DayResult] = []
    days_done = 0
    for plan in plans:
        if plan.excluded:
            # DISTINCT status "excluded" (not "skipped"): an intentionally
            # skipped day the calendar shows as such. Not processed (no dwh
            # fetch), not part of total_days, never ticks progress.
            results.append(
                DayResult(date=plan.date_int, status="excluded", skip_reason="excluded")
            )
            continue

        if _is_flat(plan):
            # Regime decided FLAT: skip the day like an exclude — no dwh fetch,
            # not in total_days, no progress tick. The per-day ``regime`` readout
            # (side=flat + state) explains WHY; not a data-quality warning.
            results.append(
                DayResult(
                    date=plan.date_int, status="skipped", skip_reason="regime_flat"
                )
            )
            continue

        # Per-day side: the regime decision when driven, else the static side.
        side = req.straddle_side
        if decision_by_date is not None:
            dec = decision_by_date.get(plan.date_int)
            if dec is not None:
                side = dec.side  # long/short here (flat handled above)

        results.append(
            await _process_day(
                reader, req, plan, all_exps, exp_to_objs, tick_size, es_tick, side
            )
        )
        days_done += 1
        if progress_cb is not None:
            progress_cb(days_done, total_days)

    # Regime EMISSION. When side_mode is regime_driven the per-day DECISION
    # readout is emitted (decision_by_date, already resolved above). Else, when
    # only F2.1 emit_signals is on, the raw per-day signals are fetched & emitted.
    # Both off => no fetch, both maps None, no ``regime`` key downstream.
    regime_by_date: dict[int, dict[str, float | None]] | None = None
    if decision_by_date is None and reg.emit_signals:
        day_dates = [r.date for r in results]
        if daily_reader is not None and day_dates:
            try:
                regime_by_date = await _fetch_regime_signals(
                    daily_reader, req, day_dates
                )
            except Exception:  # noqa: BLE001 — a signal-fetch glitch must not fail a good backtest
                logger.exception(
                    "regime signal fetch failed; emitting null regime signals"
                )
                regime_by_date = _null_regime_signals(
                    day_dates, reg.rv_windows, ("vvix",)
                )
        else:
            # emit on but no reader wired / no days: emit null-valued signals so
            # the response shape is still complete (never a silent omission).
            regime_by_date = _null_regime_signals(day_dates, reg.rv_windows, ("vvix",))

    aggregate = aggregate_days(results)
    return {
        "params_echo": _echo_params(req),
        "window": {
            "min_date": WINDOW_MIN_DATE.isoformat(),
            "max_date": WINDOW_MAX_DATE.isoformat(),
        },
        "days": [
            _serialize_day(r, regime_by_date, decision_by_date) for r in results
        ],
        "aggregate": _serialize_aggregate(aggregate),
        "warnings": _warnings(results),
    }


# --------------------------------------------------------------------------- #
# On-disk result cache (durable, always-on) — mirrors the portfolio pattern.
# --------------------------------------------------------------------------- #
# Compute-version salt for the durable on-disk cache. Folded into every key so a
# compute-affecting change (engine simulate/aggregate logic, the ES multiplier,
# tick-size handling, expiry/DTE resolution) namespaces the cache: bumping this
# guarantees stale entries can never be served with ``from_cache: true``. BUMP on
# ANY change to the intraday backtest compute output AND on each release.
INTRADAY_COMPUTE_VERSION = "0.7.0"

# A DISTINCT sqlite filename from the portfolio cache so the two do not share the
# 200-entry LRU domain (they would otherwise evict each other's entries).
_INTRADAY_CACHE_FILENAME = "intraday_results.sqlite"

# Generous default TTL: content-addressed keys mean a changed body is already a
# new key, so the TTL only bounds staleness from an UPSTREAM dwh bar revision
# under an unchanged body. Mirrors the portfolio 30-day default.
_DEFAULT_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

_intraday_result_cache: DiskResultCache | None = None


def _default_cache_path() -> str:
    """Resolve the on-disk intraday-cache file path.

    ``TCG_CACHE_DIR`` overrides the base dir (same convention as the portfolio
    cache); the default is a per-user cache dir outside the repo. Tests never
    reach this — the root-conftest autouse fixture swaps ``_intraday_result_cache``
    for a tmp-dir instance.
    """
    base = os.environ.get("TCG_CACHE_DIR") or str(Path.home() / ".cache" / "tcg")
    return str(Path(base) / _INTRADAY_CACHE_FILENAME)


def _default_cache_ttl() -> float | None:
    """Resolve the default result-cache TTL in seconds, or ``None`` for no expiry.

    ``TCG_CACHE_TTL_SECONDS`` overrides: a positive value sets the TTL; ``0`` (or a
    negative / non-numeric / empty value) DISABLES expiry. Unset → the 30-day
    default. Same knob the portfolio cache honors.
    """
    raw = os.environ.get("TCG_CACHE_TTL_SECONDS")
    if raw is None or not raw.strip():
        return float(_DEFAULT_CACHE_TTL_SECONDS)
    try:
        val = float(raw)
    except ValueError:
        return None  # misconfigured → fail safe to no-expiry rather than crash
    return val if val > 0 else None


def _get_intraday_result_cache() -> DiskResultCache:
    """Return the process-wide intraday result cache, lazily creating it."""
    global _intraday_result_cache
    if _intraday_result_cache is None:
        _intraday_result_cache = DiskResultCache(
            _default_cache_path(), ttl_seconds=_default_cache_ttl()
        )
    return _intraday_result_cache


def _strip_use_cache(obj: object) -> object:
    """Recursively drop every ``use_cache`` key from a JSON-able structure.

    ``use_cache`` selects WHETHER to use the cache, never WHICH result a body maps
    to, so it must not affect the key. Stripping it also makes a pre-feature
    payload (no ``use_cache`` field) hash identically to a current one.
    """
    if isinstance(obj, dict):
        return {k: _strip_use_cache(v) for k, v in obj.items() if k != "use_cache"}
    if isinstance(obj, list):
        return [_strip_use_cache(v) for v in obj]
    return obj


def _intraday_cache_key(req: RunRequest) -> str:
    """Canonical content key for an intraday backtest request.

    Pure function of the canonicalized request (``use_cache`` stripped at every
    level) plus the ``INTRADAY_COMPUTE_VERSION`` salt, so two equal requests hash
    equal, toggling ``use_cache`` never changes identity, and a version bump
    invalidates every stale entry. Used by BOTH the run path and the read-only
    cache endpoints so their keys always coincide.

    The ``regime`` block AUTO-participates in the key when it is ACTIVE — either
    ``emit_signals`` on (F2.1 signals change the response) OR
    ``side_mode='regime_driven'`` (F2.2 per-day side changes the P&L). It is
    STRIPPED only when BOTH halves are off: an inert regime has ZERO effect on the
    output, so a default-off request must hash identically regardless of its
    (inert) ``rv_windows`` / thresholds / gates sub-config — and identically to a
    pre-feature (regime-absent) body. The ``allowlist`` block (F3.2) is stripped
    the same way when ``mode='off'`` (inert => zero effect on which days trade).
    ``is_active`` is the single predicate the strip and the fetch/branch agree on.

    NOTE: ``cost`` and ``hedge.timing`` (also default-off/neutral) are NEVER
    stripped here even when inert — they always participate in the payload
    hash. Their default-off identity is preserved by a DIFFERENT mechanism:
    an ``INTRADAY_COMPUTE_VERSION`` bump at the time each was introduced, so a
    stale pre-feature cache entry is invalidated by the version salt rather
    than by hashing identically to the new (larger) payload. Two mechanisms,
    same goal (no wrong cache hit across a feature's introduction).
    """
    payload = _strip_use_cache(req.model_dump(mode="json"))
    if isinstance(payload, dict):
        if not req.regime.is_active:
            payload.pop("regime", None)
        if not req.allowlist.is_active:
            payload.pop("allowlist", None)
        # F4.1: an inert (default-off) ladder has ZERO effect on output, so a
        # default-off body must hash identically regardless of its (inert) ladder
        # sub-config AND identically to a pre-feature (ladder-absent) body.
        if not req.ladder.is_active:
            payload.pop("ladder", None)
    return canonical_hash({"_cv": INTRADAY_COMPUTE_VERSION, "body": payload})


# --------------------------------------------------------------------------- #
# Async job store (in-memory, single-process — see PROBLEMS.md)
# --------------------------------------------------------------------------- #
@dataclass
class _Job:
    """A background backtest run and its live progress."""

    status: Literal["running", "done", "error"] = "running"
    days_done: int = 0
    total_days: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


# Keyed by job_id. Bounded by ``_JOB_CAP``: on each new job we evict finished
# jobs (oldest first, insertion-ordered dict) once over the cap, so an abandoned
# ``done``/``error`` job that is never fetched can't grow the store without
# bound. A terminal job is NOT dropped on first fetch — it persists (under the
# same cap) so a lost/retried final poll can still recover the result rather
# than 404ing an expensive completed run (see ``get_progress``).
_JOBS: dict[str, _Job] = {}
_JOB_CAP = 64


def _new_job_id() -> str:
    # Collision-safe without wall-clock/uuid (keeps tests determinism-friendly).
    return secrets.token_hex(8)


def _prune_jobs() -> None:
    if len(_JOBS) < _JOB_CAP:
        return
    for jid in list(_JOBS):
        if len(_JOBS) < _JOB_CAP:
            break
        if _JOBS[jid].status in ("done", "error"):
            del _JOBS[jid]


async def _run_job(
    job: _Job,
    reader: IntradayV2Reader,
    req: RunRequest,
    cache_key: str | None = None,
    daily_reader: DailySeriesReader | None = None,
) -> None:
    """Background task body: run the backtest, streaming progress into ``job``.

    On success, when ``cache_key`` is not None (i.e. ``req.use_cache`` was True and
    the initial lookup missed), the fresh result dict is written to the durable
    on-disk cache under that key so a later identical run is served instantly. A
    failed run is NEVER cached — only a completed result is stored.
    """

    def _cb(done: int, total: int) -> None:
        job.days_done = done
        job.total_days = total

    try:
        result = await run_backtest(
            reader, req, progress_cb=_cb, daily_reader=daily_reader
        )
        job.result = result
        job.days_done = job.total_days
        if cache_key is not None:
            # Store the PURE result (no ``from_cache`` marker) so a cached serve is
            # byte-identical to this fresh compute apart from the response-only
            # ``from_cache`` flag the read paths add. Never cache an error. Persist
            # BEFORE flipping to ``done`` so that the moment a ``/progress`` poll
            # observes the terminal state the entry is already durably cached (no
            # observe-done-then-miss race for a follow-up cache read / fast-path).
            try:
                await _get_intraday_result_cache().put(cache_key, result)
            except Exception:  # noqa: BLE001 — a cache write glitch must not fail a good run
                logger.exception("intraday backtest cache write failed")
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as error
        logger.exception("intraday backtest job failed")
        job.error = str(exc) or exc.__class__.__name__
        job.status = "error"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/meta")
async def get_meta(request: Request) -> dict[str, Any]:
    """Static-ish metadata for the page controls (window, modes, roots)."""
    reader = IntradayV2Reader(request.app.state.dwh_pool)
    try:
        roots = await reader.list_option_roots()
        root_symbols = [r["symbol"] for r in roots]
    except Exception:  # noqa: BLE001 - meta must not hard-fail the page
        root_symbols = []
    return {
        "window": {
            "min_date": WINDOW_MIN_DATE.isoformat(),
            "max_date": WINDOW_MAX_DATE.isoformat(),
        },
        "expiry_modes": _EXPIRY_MODES,
        "roots": root_symbols,
        "hedge_instrument": "FUT_SP_500",
        "multiplier": ES_MULTIPLIER,
        "timezone": _TZ,
    }


@router.get("/event-calendar")
async def get_event_calendar() -> dict[str, Any]:
    """Curated macro event dates (F3.1) for the allowlist / event-day controls.

    STATIC — no dwh. Returns the dates grouped by type (each with its
    ``tentative`` flag), a flat de-duplicated union, the list of valid event
    types, and the tentative dates surfaced separately. Consumed by the frontend
    allowlist control and the A3 event-attribution view (next task).
    """
    return {
        "event_types": list(EVENT_TYPES),
        "events": {
            t: [
                {"date": e.date.isoformat(), "tentative": e.tentative}
                for e in event_days(t)
            ]
            for t in EVENT_TYPES
        },
        "all_dates": [d.isoformat() for d in all_event_dates()],
        "tentative_dates": [e.date.isoformat() for e in tentative_days()],
    }


@router.post("/run")
async def post_run(request: Request, req: RunRequest) -> dict[str, Any]:
    reader = IntradayV2Reader(request.app.state.dwh_pool)
    daily_reader = DailySeriesReader(request.app.state.dwh_pool)
    return await run_backtest(reader, req, daily_reader=daily_reader)


@router.post("/run-async")
async def post_run_async(request: Request, req: RunRequest) -> dict[str, str]:
    """Start a backtest as a background job; poll ``/progress/{job_id}``.

    Validation (out-of-window, inverted range, T2<=T1, no trading days) runs
    synchronously and 400s *before* any job is created — same contract as
    ``/run``. On success the resolved weekday count pins ``total_days`` up front.
    """
    total_days = count_trading_days(req)  # raises HTTPException(400) on bad input

    # Cache seam. With use_cache on, look up the durable result BEFORE creating a
    # compute task: a hit becomes a job that is ALREADY ``done`` (result attached,
    # progress pinned full, no task spawned), so the very next ``/progress`` poll
    # returns the stored result instantly. A miss (or use_cache off) falls through
    # to the normal background compute; the key is threaded to ``_run_job`` so it
    # writes the fresh result back on success (None => never cache).
    #
    # Concurrency: two identical requests arriving on a COLD cache both miss here
    # and both spawn a compute — the benign double-compute the DiskResultCache
    # documents (INSERT OR REPLACE writes the same content; never a wrong answer
    # or a corrupt row). No in-flight dedup is attempted (deliberate).
    cache_key: str | None = None
    if req.use_cache:
        cache_key = _intraday_cache_key(req)
        try:
            cached = await _get_intraday_result_cache().get(cache_key)
        except Exception:  # noqa: BLE001 — a cache glitch degrades to a fresh compute
            logger.exception("intraday backtest cache read failed")
            cached = None
        if cached is not None:
            job = _Job(
                status="done",
                days_done=total_days,
                total_days=total_days,
                result={**cached, "from_cache": True},
            )
            _prune_jobs()
            job_id = _new_job_id()
            _JOBS[job_id] = job
            return {"job_id": job_id}

    reader = IntradayV2Reader(request.app.state.dwh_pool)
    daily_reader = DailySeriesReader(request.app.state.dwh_pool)
    job = _Job(status="running", days_done=0, total_days=total_days)
    _prune_jobs()
    job_id = _new_job_id()
    _JOBS[job_id] = job
    job.task = asyncio.create_task(
        _run_job(job, reader, req, cache_key, daily_reader=daily_reader)
    )
    return {"job_id": job_id}


@router.get("/progress/{job_id}")
async def get_progress(job_id: str) -> dict[str, Any]:
    """Live progress for a job. Unknown id → 404.

    A terminal job (``done``/``error``) is NOT evicted here: it persists in the
    bounded store (pruned only when a later run needs the space, see
    :func:`_prune_jobs`/``_JOB_CAP``) so a lost or retried final poll can still
    recover the result instead of 404ing an expensive completed run.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id!r}")
    return {
        "status": job.status,
        "days_done": job.days_done,
        "total_days": job.total_days,
        "result": job.result,
        "error": job.error,
    }


# --------------------------------------------------------------------------- #
# Read-only cache endpoints (mirror the portfolio pattern). Body is a full
# RunRequest so the key is computed IDENTICALLY to the run path. Neither endpoint
# ever computes or takes a market-data / reader dependency — they structurally
# cannot fetch dwh or trigger a backtest.
# --------------------------------------------------------------------------- #
@router.post("/cache/status")
async def intraday_cache_status(req: RunRequest) -> dict[str, bool]:
    """Report whether a cached result already exists for ``req`` — WITHOUT
    computing anything. Uses ``peek`` (a pure, non-mutating existence check that
    honors the TTL and never bumps the LRU), so the status agrees exactly with a
    real hit. A cache glitch degrades to ``cached: false`` (never a 500)."""
    try:
        cached = await _get_intraday_result_cache().peek(_intraday_cache_key(req))
    except Exception:  # noqa: BLE001 — a cache glitch degrades to not-cached, never 500
        cached = False
    return {"cached": bool(cached)}


@router.post("/cache/get")
async def intraday_cache_get(req: RunRequest) -> dict[str, Any]:
    """Return a cached backtest result for ``req`` WITHOUT ever computing.

    Backs the reload-a-simulation UX: reopening a run whose config is already
    cached shows its results with no recompute.

    * HIT  → the full result object with ``from_cache: true`` (byte-identical to a
      fresh run's result plus that marker).
    * MISS → ``{"cached": false}`` at HTTP 200. It NEVER calls the compute path on
      a miss — the safety property behind auto-display.

    A cache error degrades to a miss (never a 500) so a glitch can't block the UI.
    """
    try:
        cached = await _get_intraday_result_cache().get(_intraday_cache_key(req))
    except Exception:  # noqa: BLE001 — a cache glitch degrades to a miss, never 500
        cached = None
    if cached is None:
        return {"cached": False}
    return {**cached, "from_cache": True}


@router.post("/cache/clear")
async def intraday_cache_clear() -> dict[str, bool]:
    """Clear the on-disk intraday result cache. Content-addressed, so this only
    forces the next run of each body to recompute-and-repopulate — never a
    correctness change."""
    await asyncio.to_thread(_get_intraday_result_cache().clear)
    return {"cleared": True}
