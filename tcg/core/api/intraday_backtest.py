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
    parse_hhmm,
    resolve_et_to_utc,
    select_atm_strike,
    simulate_day,
    snap_nearest,
)
from tcg.engine.regime import realized_vol_by_date
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
class RegimeConfig(BaseModel):
    """Regime-signal emission knob. ``emit_signals=False`` (default) => no extra
    dwh fetch and NO ``regime`` key on any day (response byte-identical to the
    pre-feature baseline). ``rv_windows`` are the realized-vol lookbacks (trading
    days, each >= 2); ``sp500_symbol`` is the daily close series RV is computed
    from and ``vvix_symbol`` the passthrough VVIX series — both dwh
    ``dim_instrument`` symbols read through the P0.3 generic seam."""

    emit_signals: bool = False
    rv_windows: list[int] = Field(default_factory=lambda: [20, 30, 100])
    sp500_symbol: str = "IND_SP_500"
    vvix_symbol: str = "IND_VVIX"

    @field_validator("rv_windows")
    @classmethod
    def _valid_windows(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("rv_windows must be non-empty")
        for w in v:
            if w < 2:
                raise ValueError(f"each rv_window must be >= 2 (got {w})")
        return v


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
    """Resolved per-day trading plan (pure; no DB)."""

    day: date
    date_int: int
    entry_ts: datetime  # UTC
    exit_ts: datetime  # UTC
    entry_tol: float
    exit_tol: float
    entry_conditions: list  # engine condition dataclasses
    exit_conditions: list
    exit_triggers: list = field(default_factory=list)  # engine trigger dataclasses
    excluded: bool = False


# --------------------------------------------------------------------------- #
# Pure validation / day resolution (unit-tested without a DB)
# --------------------------------------------------------------------------- #
def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field}: invalid date {value!r}") from exc


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
        if cd.exclude:
            excluded.add(cd_date)
            continue
        if cd.entry is not None or cd.exit is not None:
            overrides[cd_date] = (cd.entry, cd.exit)

    plans: list[DayPlan] = []
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            e_ov, x_ov = overrides.get(d, (None, None))
            e_time, e_tol, e_conds, _e_trigs = _resolve_module(req.entry, e_ov)
            x_time, x_tol, x_conds, x_trigs = _resolve_module(req.exit, x_ov)
            entry_ts = resolve_et_to_utc(d, e_time, _TZ)
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
                )
            )
        d += one
    if not plans:
        raise HTTPException(status_code=400, detail="no trading days in range")
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


async def _fetch_regime_signals(
    daily_reader: DailySeriesReader,
    req: RunRequest,
    day_dates: list[int],
) -> dict[int, dict[str, float | None]]:
    """Fetch daily series through the P0.3 seam, compute RV, join with VVIX.

    Fetches IND_SP_500 with a lookback long enough to warm up the largest RV
    window (so RV is available from the FIRST backtest day), computes RV in the
    pure engine, and joins VVIX passthrough. VIX1D DROP-IN (F2.3) is exactly:
    add a ``vix1d_symbol`` to :class:`RegimeConfig`, one more ``read_series``
    call, and one ``passthrough["vix1d"] = ...`` line — the assembly is unchanged.
    """
    reg = req.regime
    windows = reg.rv_windows
    lo = min(day_dates)
    hi = max(day_dates)
    start = date.fromisoformat(_int_to_iso(lo))
    end = date.fromisoformat(_int_to_iso(hi))
    # Calendar lookback covering the largest window in trading days plus slack
    # for weekends/holidays, so the first backtest day already has full history.
    lookback = timedelta(days=max(windows) * 2 + 30)

    sp = await daily_reader.read_series(reg.sp500_symbol, start=start - lookback, end=end)
    rv_by_date = realized_vol_by_date(sp.dates, sp.values, windows)

    vvix = await daily_reader.read_series(reg.vvix_symbol, start=start, end=end)
    passthrough: dict[str, dict[int, float]] = {
        "vvix": dict(zip(vvix.dates, vvix.values))
    }
    return build_regime_signal_map(day_dates, rv_by_date, passthrough, windows)


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


def _serialize_day(
    r: DayResult,
    regime_by_date: dict[int, dict[str, float | None]] | None = None,
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
    # F2.1: attach per-day regime signals ONLY when emission is on. When off,
    # ``regime_by_date`` is None and NO key is added -> the day dict is
    # byte-identical to the pre-feature baseline (regression guard).
    if regime_by_date is not None:
        out["regime"] = regime_by_date.get(r.date)
    return out


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
        if r.status == "skipped" and r.skip_reason:
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


async def _process_day(
    reader: IntradayV2Reader,
    req: RunRequest,
    plan: DayPlan,
    all_exps: list[date],
    exp_to_objs: dict[date, list[int]],
    tick_size: float,
    es_tick: float,
) -> DayResult:
    """Fetch marks + simulate a single (non-excluded) trading day (async I/O)."""
    expiry = _pick_expiry(all_exps, plan.day, req.expiry_mode, req.dte)
    if expiry is None:
        return DayResult(date=plan.date_int, status="skipped", skip_reason="no_expiry")

    # Window: from the session open (09:30 ET, the max_underlying_move day-open
    # reference) or entry, whichever is earlier, out to the exit-scan horizon
    # (exit target + its snap tolerance) — plus a small buffer both sides.
    buf = timedelta(minutes=2.0)
    session_open = resolve_et_to_utc(plan.day, _SESSION_OPEN_ET, _TZ)
    win_start = min(plan.entry_ts, session_open) - buf
    win_end = plan.exit_ts + timedelta(minutes=plan.exit_tol) + buf
    es_bars = await reader.fetch_es_future_1m(win_start, win_end, on_or_after=plan.day)
    # ATM reference: ES nearest the entry target within the entry tolerance.
    es1 = snap_nearest(es_bars, plan.entry_ts, plan.entry_tol)
    if es1 is None:
        return DayResult(
            date=plan.date_int,
            status="skipped",
            skip_reason="no_quote_within_tolerance",
            expiry=expiry,
        )

    chosen: tuple[int, float, int, int] | None = None
    for oid in exp_to_objs[expiry]:
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
    es_day_open = _resolve_es_day_open(es_bars, plan.entry_ts, session_open)

    return simulate_day(
        date_int=plan.date_int,
        side=req.straddle_side,
        strike=strike,
        expiry=expiry,
        es_bars=es_bars,
        call_marks=call_marks,
        put_marks=put_marks,
        entry_ts=plan.entry_ts,
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

    ``daily_reader`` (the P0.3 generic daily-series seam) is used ONLY when
    ``req.regime.emit_signals`` is on, to fetch IND_SP_500 / VVIX and attach
    per-day regime signals. When emission is off it is never touched — no extra
    dwh fetch — and the response is byte-identical to the pre-feature baseline.
    """
    plans = resolve_day_plans(req)
    start = plans[0].day
    total_days = sum(1 for p in plans if not p.excluded)

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

        results.append(
            await _process_day(
                reader, req, plan, all_exps, exp_to_objs, tick_size, es_tick
            )
        )
        days_done += 1
        if progress_cb is not None:
            progress_cb(days_done, total_days)

    # F2.1 regime signals: computed ONLY when emission is on. Off => no fetch,
    # regime_by_date stays None, and no ``regime`` key is added downstream.
    regime_by_date: dict[int, dict[str, float | None]] | None = None
    if req.regime.emit_signals:
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
                    day_dates, req.regime.rv_windows, ("vvix",)
                )
        else:
            # emit on but no reader wired / no days: emit null-valued signals so
            # the response shape is still complete (never a silent omission).
            regime_by_date = _null_regime_signals(
                day_dates, req.regime.rv_windows, ("vvix",)
            )

    aggregate = aggregate_days(results)
    return {
        "params_echo": req.model_dump(),
        "window": {
            "min_date": WINDOW_MIN_DATE.isoformat(),
            "max_date": WINDOW_MAX_DATE.isoformat(),
        },
        "days": [_serialize_day(r, regime_by_date) for r in results],
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
INTRADAY_COMPUTE_VERSION = "0.4.0"

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

    The ``regime`` block AUTO-participates in the key when emission is ON (its
    windows/symbols then change the result), but is STRIPPED when
    ``emit_signals`` is off: an off-regime has ZERO effect on the output, so a
    default-off request must hash identically regardless of its (inert)
    ``rv_windows`` / symbol sub-config — and identically to a pre-feature body.
    """
    payload = _strip_use_cache(req.model_dump(mode="json"))
    if isinstance(payload, dict):
        reg = payload.get("regime")
        if isinstance(reg, dict) and not reg.get("emit_signals", False):
            payload.pop("regime", None)
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
