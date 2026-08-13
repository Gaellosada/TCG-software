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
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

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
    WINDOW_MAX_DATE,
    WINDOW_MIN_DATE,
    AggregateResult,
    DayResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/intraday-backtest", tags=["intraday-backtest"])

_TZ = "America/New_York"
_EXPIRY_MODES = ["0DTE", "NDTE"]


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class HedgeConfig(BaseModel):
    enabled: bool = True
    interval_minutes: float = Field(default=15.0, gt=0)
    delta_band: float = Field(default=0.10, ge=0)


class CustomDay(BaseModel):
    """A single per-date control (unified exclude + time-override).

    ``exclude=True`` -> the day is NOT traded; it is still emitted in the
    response ``days`` array with status ``"excluded"`` so the calendar can show
    it as an intentionally-skipped day. When ``exclude=True`` any ``entry_time``
    / ``exit_time`` are ignored (no conflict). When ``exclude=False`` a present
    ``entry_time`` / ``exit_time`` overrides the request default for that date;
    a missing side falls back to the request default.
    """

    date: str
    exclude: bool = False
    entry_time: str | None = None
    exit_time: str | None = None

    @field_validator("entry_time", "exit_time")
    @classmethod
    def _valid_hhmm(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parse_hhmm(v)  # raises ValueError -> 422 on bad "HH:MM"
        return v


class RunRequest(BaseModel):
    start_date: str
    end_date: str
    entry_time: str = "10:00"
    exit_time: str = "15:45"
    expiry_mode: Literal["0DTE", "NDTE"] = "0DTE"
    dte: int = Field(default=0, ge=0)
    straddle_side: Literal["long", "short"] = "long"
    hedge: HedgeConfig = Field(default_factory=HedgeConfig)
    snap_tolerance_minutes: float = Field(default=10.0, gt=0)
    # Unified per-date control: SUPERSEDES the old exception_dates +
    # date_overrides pair (see DESIGN.md §API contract).
    custom_days: list[CustomDay] = Field(default_factory=list)


@dataclass
class DayPlan:
    """Resolved per-day trading plan (pure; no DB)."""

    day: date
    date_int: int
    entry_ts: datetime  # UTC
    exit_ts: datetime  # UTC
    excluded: bool


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

    # Fold custom_days into an excluded-set + a per-date time-override map.
    # exclude:true wins (times ignored); otherwise a present entry/exit_time
    # overrides the default for that date (a missing side falls back below).
    excluded: set[date] = set()
    overrides: dict[date, tuple[str | None, str | None]] = {}
    for cd in req.custom_days:
        cd_date = _parse_date(cd.date, "custom_days")
        if cd.exclude:
            excluded.add(cd_date)
            continue
        if cd.entry_time is not None or cd.exit_time is not None:
            overrides[cd_date] = (cd.entry_time, cd.exit_time)

    plans: list[DayPlan] = []
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            ov = overrides.get(d)
            if ov is None:
                entry_time, exit_time = req.entry_time, req.exit_time
            else:
                entry_time = ov[0] if ov[0] is not None else req.entry_time
                exit_time = ov[1] if ov[1] is not None else req.exit_time
            entry_ts = resolve_et_to_utc(d, entry_time, _TZ)
            exit_ts = resolve_et_to_utc(d, exit_time, _TZ)
            if exit_ts <= entry_ts:
                raise HTTPException(
                    status_code=400,
                    detail=f"{d.isoformat()}: exit_time must be after entry_time",
                )
            plans.append(
                DayPlan(
                    day=d,
                    date_int=d.year * 10000 + d.month * 100 + d.day,
                    entry_ts=entry_ts,
                    exit_ts=exit_ts,
                    excluded=d in excluded,
                )
            )
        d += one
    if not plans:
        raise HTTPException(status_code=400, detail="no trading days in range")
    return plans


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
    pad: timedelta,
    tol: float,
) -> DayResult:
    """Fetch marks + simulate a single (non-excluded) trading day (async I/O)."""
    expiry = _pick_expiry(all_exps, plan.day, req.expiry_mode, req.dte)
    if expiry is None:
        return DayResult(date=plan.date_int, status="skipped", skip_reason="no_expiry")

    win_start = plan.entry_ts - pad
    win_end = plan.exit_ts + pad
    es_bars = await reader.fetch_es_future_1m(win_start, win_end, on_or_after=plan.day)
    es1 = snap_nearest(es_bars, plan.entry_ts, tol)
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
        snap_tolerance_minutes=tol,
        hedge_enabled=req.hedge.enabled,
        interval_minutes=req.hedge.interval_minutes,
        delta_band=req.hedge.delta_band,
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

    tol = req.snap_tolerance_minutes
    pad = timedelta(minutes=tol + 2.0)

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
            await _process_day(reader, req, plan, all_exps, exp_to_objs, pad, tol)
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
