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

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tcg.data._sql.intraday_v2 import IntradayV2Reader
from tcg.engine.intraday_backtest import (
    aggregate_days,
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


class DateOverride(BaseModel):
    date: str
    entry_time: str
    exit_time: str


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
    exception_dates: list[str] = Field(default_factory=list)
    date_overrides: list[DateOverride] = Field(default_factory=list)


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

    excluded = {_parse_date(d, "exception_dates") for d in req.exception_dates}
    overrides: dict[date, tuple[str, str]] = {
        _parse_date(o.date, "date_overrides"): (o.entry_time, o.exit_time)
        for o in req.date_overrides
    }

    plans: list[DayPlan] = []
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            entry_time, exit_time = overrides.get(d, (req.entry_time, req.exit_time))
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
    labels = {
        "no_quote_within_tolerance": "no quote within tolerance",
        "no_expiry": "no matching expiry",
        "no_contract": "no ATM contract",
        "excluded": "excluded by date exception",
    }
    return [
        f"{n} day(s) skipped: {labels.get(reason, reason)}"
        for reason, n in sorted(reasons.items())
    ]


# --------------------------------------------------------------------------- #
# Orchestration (async I/O + pure engine)
# --------------------------------------------------------------------------- #
async def run_backtest(reader: IntradayV2Reader, req: RunRequest) -> dict[str, Any]:
    """Full run: resolve days, fetch marks per day, simulate, aggregate."""
    plans = resolve_day_plans(req)
    start = plans[0].day

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
    for plan in plans:
        if plan.excluded:
            results.append(
                DayResult(date=plan.date_int, status="skipped", skip_reason="excluded")
            )
            continue

        expiry = _pick_expiry(all_exps, plan.day, req.expiry_mode, req.dte)
        if expiry is None:
            results.append(
                DayResult(date=plan.date_int, status="skipped", skip_reason="no_expiry")
            )
            continue

        win_start = plan.entry_ts - pad
        win_end = plan.exit_ts + pad
        es_bars = await reader.fetch_es_future_1m(win_start, win_end, on_or_after=plan.day)
        es1 = snap_nearest(es_bars, plan.entry_ts, tol)
        if es1 is None:
            results.append(
                DayResult(
                    date=plan.date_int,
                    status="skipped",
                    skip_reason="no_quote_within_tolerance",
                    expiry=expiry,
                )
            )
            continue

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
            results.append(
                DayResult(
                    date=plan.date_int,
                    status="skipped",
                    skip_reason="no_contract",
                    expiry=expiry,
                )
            )
            continue

        _oid, strike, call_id, put_id = chosen
        call_marks = await reader.fetch_option_1m(call_id, win_start, win_end)
        put_marks = await reader.fetch_option_1m(put_id, win_start, win_end)

        results.append(
            simulate_day(
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
        )

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
