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
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from tcg.data._sql.intraday_v2 import IntradayV2Reader
from tcg.engine.intraday_backtest import (
    aggregate_days,
    parse_hhmm,
    resolve_et_to_utc,
    select_atm_strike,
    simulate_day,
    snap_nearest,
)
from tcg.types.intraday import (
    ES_MULTIPLIER,
    ES_OPTION_TICK_SIZE,
    WINDOW_MAX_DATE,
    WINDOW_MIN_DATE,
    AggregateResult,
    DayResult,
    MaxSpreadCond,
    MaxUnderlyingMoveCond,
    MinPremiumCond,
    MinQuoteSizeCond,
    NetDeltaTrigger,
    PnlTrigger,
    SigmaMoveTrigger,
    UnderlyingMoveTrigger,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/intraday-backtest", tags=["intraday-backtest"])

_TZ = "America/New_York"
_EXPIRY_MODES = ["0DTE", "NDTE"]
# Session open (ET) used as the max_underlying_move "day_open" reference anchor.
_SESSION_OPEN_ET = "09:30"


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class HedgeConfig(BaseModel):
    enabled: bool = True
    interval_minutes: float = Field(default=15.0, gt=0)
    delta_band: float = Field(default=0.10, ge=0)


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
    # Unified per-date control (exclude + full per-day override).
    custom_days: list[CustomDay] = Field(default_factory=list)

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


def _pick_expiry(all_exps: list[date], day: date, mode: str, dte: int) -> date | None:
    """Resolve the target expiry for a day per ``expiry_mode`` (pure)."""
    if mode == "0DTE":
        return day if day in all_exps else None
    # NDTE: nearest expiry with days_to_exp >= dte.
    candidates = [e for e in all_exps if (e - day).days >= dte and e >= day]
    return min(candidates) if candidates else None


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


def _serialize_day(r: DayResult) -> dict[str, Any]:
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
        }
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
    es_day_open = es_bars[0].price if es_bars else None

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
        hedge_enabled=req.hedge.enabled,
        interval_minutes=req.hedge.interval_minutes,
        delta_band=req.hedge.delta_band,
        es_day_open=es_day_open,
        multiplier=ES_MULTIPLIER,
    )


async def run_backtest(
    reader: IntradayV2Reader,
    req: RunRequest,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Full run: resolve days, fetch marks per day, simulate, aggregate.

    ``progress_cb(days_done, total_days)`` — if given — is invoked once per
    *non-excluded* trading day as the loop advances (``days_done`` climbs from
    1 to ``total_days``). Excluded days are appended to the results but never
    tick progress: ``total_days`` is the exceptions-removed weekday count, the
    authoritative denominator shared with :func:`count_trading_days`.
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

    # Tick size for the max_spread 1-tick floor (sourced once per run).
    tick_size = await reader.get_option_tick_size()

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
            await _process_day(reader, req, plan, all_exps, exp_to_objs, tick_size)
        )
        days_done += 1
        if progress_cb is not None:
            progress_cb(days_done, total_days)

    aggregate = aggregate_days(results)
    return {
        "params_echo": req.model_dump(),
        "window": {
            "min_date": WINDOW_MIN_DATE.isoformat(),
            "max_date": WINDOW_MAX_DATE.isoformat(),
        },
        "days": [_serialize_day(r) for r in results],
        "aggregate": _serialize_aggregate(aggregate),
        "warnings": _warnings(results),
    }


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
# bound. A successfully fetched ``done``/``error`` job is dropped immediately
# (see ``get_progress``).
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


async def _run_job(job: _Job, reader: IntradayV2Reader, req: RunRequest) -> None:
    """Background task body: run the backtest, streaming progress into ``job``."""

    def _cb(done: int, total: int) -> None:
        job.days_done = done
        job.total_days = total

    try:
        result = await run_backtest(reader, req, progress_cb=_cb)
        job.result = result
        job.days_done = job.total_days
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
    return await run_backtest(reader, req)


@router.post("/run-async")
async def post_run_async(request: Request, req: RunRequest) -> dict[str, str]:
    """Start a backtest as a background job; poll ``/progress/{job_id}``.

    Validation (out-of-window, inverted range, T2<=T1, no trading days) runs
    synchronously and 400s *before* any job is created — same contract as
    ``/run``. On success the resolved weekday count pins ``total_days`` up front.
    """
    total_days = count_trading_days(req)  # raises HTTPException(400) on bad input

    reader = IntradayV2Reader(request.app.state.dwh_pool)
    job = _Job(status="running", days_done=0, total_days=total_days)
    _prune_jobs()
    job_id = _new_job_id()
    _JOBS[job_id] = job
    job.task = asyncio.create_task(_run_job(job, reader, req))
    return {"job_id": job_id}


@router.get("/progress/{job_id}")
async def get_progress(job_id: str) -> dict[str, Any]:
    """Live progress for a job. Unknown id → 404.

    A finished job (``done``/``error``) is dropped from the store on this, the
    first fetch that observes its terminal state — the client polls until then
    and stops, so the single terminal snapshot returned here is authoritative.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id!r}")
    snapshot = {
        "status": job.status,
        "days_done": job.days_done,
        "total_days": job.total_days,
        "result": job.result,
        "error": job.error,
    }
    if job.status in ("done", "error"):
        _JOBS.pop(job_id, None)
    return snapshot
