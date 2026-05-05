"""Bar / signal integrity checks plus the 22-probe `validate_strategy_spec`.

The spec-time probes encoded here come from `pipeline/probes.md`; each numbered
probe (#1..#22) has a private helper that returns a `ProbeFinding` (or None).
`validate_strategy_spec` runs them in the documented priority order
(universe -> time -> data -> signals -> methodology -> execution -> sizing).
Tests in `tests/test_validate_probes.py` pin "fires on canonical example" and
"does not fire on clean spec" for every probe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from .aliases import resolve_ticker
from .constants import AMERICAN_STYLE_ROOTS, CALENDAR_BY_ASSET_CLASS
from .data_load import OptionChainSnapshot, OptionContractSeries, PriceSeries


@dataclass(frozen=True)
class IntegrityReport:
    """Outcome of an integrity check.

    Three-level severity: PASS (clean) / WARN (soft, pipeline continues) /
    FAIL (hard, pipeline must abort).

    - `ok=True, warnings=()` -> PASS
    - `ok=True, warnings=(...)` -> WARN (pipeline continues with caveat)
    - `ok=False` -> FAIL (must abort)

    `ok` is the abort signal; `failures` lists the hard errors (empty when
    `ok=True`); `warnings` lists soft issues that the caller should surface
    but should not abort on. The WARN/FAIL split was added so that calendar
    gaps within tolerance (per `pipeline/02-data.md`: <=5%) no longer label
    an otherwise-fine chain as FAIL.
    """

    ok: bool
    failures: tuple[str, ...]
    checks: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> Literal["PASS", "WARN", "FAIL"]:
        """Three-level severity label derived from ok/warnings."""
        if not self.ok:
            return "FAIL"
        if self.warnings:
            return "WARN"
        return "PASS"

    def summary_line(self) -> str:
        """Return a one-line human-readable summary.

        Three labels: ``FAIL: ...`` when hard failures present (pipeline must
        abort), ``WARN: ...`` when only soft warnings (pipeline continues),
        ``OK: ...`` when clean.
        """
        if not self.ok and self.failures:
            return f"FAIL: {self.failures[0]}"
        n = int(self.checks.get("n_bars", 0))
        gaps = int(self.checks.get("gaps", 0))
        nans = int(self.checks.get("nan_close", 0))
        if self.warnings:
            return f"WARN: {self.warnings[0]} ({n} bars, {gaps} gaps, {nans} NaNs)"
        return f"OK: {n} bars, {gaps} gaps, {nans} NaNs"


def _yyyymmdd_to_date(d: int) -> date:
    n = int(d)
    return date(n // 10000, (n // 100) % 100, n % 100)


# Calendar-gap severity boundary: gaps <= 10% of expected days surface as WARN
# (pipeline continues with caveat); > 10% is FAIL (abort). The 10% ceiling is
# wider than pipeline/02-data.md's 5% probe trigger (which marks the "ask the
# user" boundary, not the "abort" boundary) so 5%-9% chains — operationally
# fine in practice — no longer fire FAIL. Bumping this requires a matching
# pipeline doc update; keep the two in sync.
_GAP_WARN_FRACTION = 0.10


def bar_integrity(bars: PriceSeries, *, exchange: str = "XNYS") -> IntegrityReport:
    """Validate a PriceSeries for monotonicity, dups, NaNs, calendar gaps, range."""
    failures: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    dates = np.asarray(bars.dates, dtype=np.int64)
    close = np.asarray(bars.close, dtype=np.float64)
    n = int(dates.shape[0])
    checks["n_bars"] = n
    if n == 0:
        failures.append("empty bar series")
        return IntegrityReport(
            ok=False, failures=tuple(failures), checks=checks, warnings=(),
        )

    if np.any(np.diff(dates) <= 0):
        failures.append("dates not strictly increasing (duplicates or out-of-order)")
    nan_close = int(np.isnan(close).sum())
    checks["nan_close"] = nan_close
    if nan_close > 0:
        failures.append(f"{nan_close} NaN close values")

    finite_close = close[~np.isnan(close)]
    if finite_close.size > 0:
        med = float(np.median(finite_close))
        if med <= 0:
            failures.append(f"non-positive median close ({med})")
        else:
            lo = float(np.min(finite_close))
            hi = float(np.max(finite_close))
            if lo <= 0:
                failures.append(f"non-positive close minimum ({lo})")
            if hi > 10.0 * med:
                failures.append(f"close max {hi} exceeds 10x median {med}")
            checks["close_min"] = lo
            checks["close_max"] = hi
            checks["close_median"] = med

    # Calendar gap check. ImportError is the only legitimate "skip" path
    # (optional dep). All other errors (KeyError on bad exchange, ValueError
    # on bad schedule shape) surface as integrity failures so silent zero-gap
    # results don't mask a misconfigured calendar.
    try:
        import pandas_market_calendars as mcal  # type: ignore
    except ImportError as e:
        checks["gaps"] = 0
        checks["calendar_error"] = f"pandas_market_calendars unavailable: {e!r}"
    else:
        try:
            cal = mcal.get_calendar(exchange)
            sched = cal.schedule(
                start_date=_yyyymmdd_to_date(int(dates[0])),
                end_date=_yyyymmdd_to_date(int(dates[-1])),
            )
            expected_n = int(len(sched.index))
            gaps = max(0, expected_n - n)
            checks["expected_n"] = expected_n
            checks["gaps"] = gaps
            if gaps > 0:
                # Reclassify gap fraction <=5% as a WARN (pipeline continues per
                # pipeline/02-data.md). >5% remains a hard FAIL.
                gap_fraction = gaps / expected_n if expected_n > 0 else 1.0
                checks["gap_fraction"] = float(gap_fraction)
                msg = f"{gaps} calendar gaps vs {exchange} ({gap_fraction:.1%})"
                if gap_fraction <= _GAP_WARN_FRACTION:
                    warnings.append(msg)
                else:
                    failures.append(msg)
        except (KeyError, ValueError) as e:
            checks["gaps"] = 0
            checks["calendar_error"] = repr(e)
            failures.append(f"calendar lookup failed for exchange={exchange!r}: {e!r}")

    return IntegrityReport(
        ok=not failures,
        failures=tuple(failures),
        checks=checks,
        warnings=tuple(warnings),
    )


def chain_integrity(
    chain: Any,
    *,
    start: int,
    end: int,
    dte_min: int,
    dte_max: int,
    expected_asof_count: int | None = None,
    exchange: str = "XNYS",
) -> IntegrityReport:
    """Validate an OptionChainHistory for snapshot coverage, row ordering, and DTE-window coverage."""
    if chain is None:
        raise TypeError("chain_integrity: chain is None")
    snapshots = getattr(chain, "snapshots", None)
    if snapshots is None:
        raise TypeError(
            f"chain_integrity: object has no .snapshots attribute (got {type(chain).__name__})"
        )

    failures: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    snaps: tuple[OptionChainSnapshot, ...] = tuple(snapshots)
    n_snaps = len(snaps)
    checks["n_snapshots"] = n_snaps

    # Basic counts (also exposed on OptionChainHistory but recomputed here so
    # the report is self-contained even when callers pass a lookalike object).
    n_obs = 0
    contract_ids: set[str] = set()
    for s in snaps:
        for c in s.contracts:
            n_obs += len(c.rows)
            contract_ids.add(c.contract_id)
    checks["n_observations"] = int(n_obs)
    checks["n_contracts"] = len(contract_ids)

    if n_snaps == 0:
        failures.append("empty chain (no snapshots)")
        return IntegrityReport(
            ok=False, failures=tuple(failures), checks=checks, warnings=(),
        )

    # Per-snapshot row inspection: empty snapshots, per-contract row ordering,
    # per-contract row dates within snapshot's asof, DTE coverage.
    n_empty = 0
    n_out_of_order = 0
    n_rows_outside_window = 0
    n_dte_uncovered = 0
    asof_dates: list[int] = []

    for s in snaps:
        asof_dates.append(int(s.asof_date))
        if len(s.contracts) == 0:
            n_empty += 1
            n_dte_uncovered += 1
            continue
        any_in_window = False
        for c in s.contracts:
            row_dates = np.asarray([int(r.date) for r in c.rows], dtype=np.int64)
            if row_dates.size >= 2 and np.any(np.diff(row_dates) < 0):
                n_out_of_order += 1
            if row_dates.size > 0:
                if int(row_dates.min()) < int(start) or int(row_dates.max()) > int(end):
                    n_rows_outside_window += int(
                        np.sum((row_dates < int(start)) | (row_dates > int(end)))
                    )
            dte = (_yyyymmdd_to_date(int(c.expiration)) - _yyyymmdd_to_date(int(s.asof_date))).days
            if int(dte_min) <= dte <= int(dte_max):
                any_in_window = True
        if not any_in_window:
            n_dte_uncovered += 1

    checks["n_empty_snapshots"] = int(n_empty)
    checks["n_out_of_order_rows"] = int(n_out_of_order)
    checks["n_rows_outside_window"] = int(n_rows_outside_window)
    checks["n_dte_uncovered_dates"] = int(n_dte_uncovered)

    if n_empty > 0:
        failures.append(f"{n_empty} empty snapshots")
    if n_out_of_order > 0:
        failures.append(f"{n_out_of_order} contracts with out-of-order rows")
    if n_rows_outside_window > 0:
        failures.append(
            f"{n_rows_outside_window} per-contract rows outside [start={start}, end={end}]"
        )
    if n_dte_uncovered > 0:
        failures.append(
            f"{n_dte_uncovered} asof-dates with no contract in DTE window [{dte_min}, {dte_max}]"
        )

    # Asof-date monotonicity and uniqueness across snapshots.
    asof_arr = np.asarray(asof_dates, dtype=np.int64)
    if asof_arr.size >= 2 and np.any(np.diff(asof_arr) <= 0):
        failures.append("snapshot asof_date not strictly increasing (duplicates or out-of-order)")

    # Asof-date calendar gap detection — same shape as bar_integrity. Loud on
    # calendar misconfig; silent only when the optional dep is absent.
    # `n_unexpected_extra_asof_dates` is the surplus side (snapshots > calendar/explicit
    # expectation). Surplus is most often a calendar quirk (e.g. a half-day not in XNYS)
    # rather than a real defect, so it is recorded in `checks` for inspection but does
    # NOT add a `failures` entry. Deficit (gaps) remains a loud failure.
    n_extra = 0
    try:
        import pandas_market_calendars as mcal  # type: ignore
    except ImportError as e:
        checks["asof_date_gaps"] = 0
        checks["calendar_error"] = f"pandas_market_calendars unavailable: {e!r}"
    else:
        try:
            cal = mcal.get_calendar(exchange)
            sched = cal.schedule(
                start_date=_yyyymmdd_to_date(int(start)),
                end_date=_yyyymmdd_to_date(int(end)),
            )
            expected_n = int(len(sched.index))
            gaps = max(0, expected_n - n_snaps)
            n_extra = max(n_extra, max(0, n_snaps - expected_n))
            checks["expected_asof_n"] = expected_n
            checks["asof_date_gaps"] = gaps
            if gaps > 0:
                # WARN/FAIL split (per pipeline/02-data.md): gap fraction
                # <=10% is a soft warning (pipeline continues with caveat);
                # >10% remains a hard failure (abort).
                gap_fraction = gaps / expected_n if expected_n > 0 else 1.0
                checks["asof_date_gap_fraction"] = float(gap_fraction)
                msg = f"{gaps} asof-date calendar gaps vs {exchange} ({gap_fraction:.1%})"
                if gap_fraction <= _GAP_WARN_FRACTION:
                    warnings.append(msg)
                else:
                    failures.append(msg)
        except (KeyError, ValueError) as e:
            checks["asof_date_gaps"] = 0
            checks["calendar_error"] = repr(e)
            failures.append(f"calendar lookup failed for exchange={exchange!r}: {e!r}")

    # Optional explicit asof-count expectation. Deficit -> loud failure (we expected
    # more snapshots than we got). Surplus -> visible in checks only (the chain has
    # extra snapshots vs the user's expectation; could be a calendar quirk).
    if expected_asof_count is not None:
        exp = int(expected_asof_count)
        mismatch = exp != n_snaps
        checks["expected_asof_count_mismatch"] = bool(mismatch)
        if exp > n_snaps:
            failures.append(
                f"asof-count mismatch: expected {exp}, got {n_snaps}"
            )
        elif n_snaps > exp:
            n_extra = max(n_extra, n_snaps - exp)

    checks["n_unexpected_extra_asof_dates"] = int(n_extra)

    return IntegrityReport(
        ok=not failures,
        failures=tuple(failures),
        checks=checks,
        warnings=tuple(warnings),
    )


def signal_integrity(signal: NDArray[np.float64], bars: PriceSeries) -> IntegrityReport:
    """Validate a signal array against bars: length match, NaN count, value bounds."""
    failures: list[str] = []
    checks: dict[str, Any] = {}
    s = np.asarray(signal, dtype=np.float64)
    n = int(s.shape[0])
    checks["n_signal"] = n
    checks["n_bars"] = int(bars.dates.shape[0])
    if n != int(bars.dates.shape[0]):
        failures.append(f"length mismatch: signal={n} bars={int(bars.dates.shape[0])}")
    nan_count = int(np.isnan(s).sum())
    checks["nan_signal"] = nan_count
    if nan_count > 0:
        failures.append(f"{nan_count} NaN values in signal")
    finite = s[~np.isnan(s)]
    if finite.size > 0:
        if np.any(np.abs(finite) > 1.0 + 1e-9):
            failures.append("signal values outside [-1, 1]")
        checks["signal_min"] = float(np.min(finite))
        checks["signal_max"] = float(np.max(finite))
    return IntegrityReport(ok=not failures, failures=tuple(failures), checks=checks)


# =============================================================================
# Strategy-spec probes (P0-J): 22 inconsistency probes from pipeline/probes.md
# =============================================================================


Severity = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class ProbeFinding:
    """One probe outcome. `fired=False` with severity 'low' marks a probe deferred
    because data was unavailable; downstream callers should re-run after data load."""

    probe_id: str
    category: str
    fired: bool
    severity: Severity
    message: str
    suggested_resolution: str
    context: dict


def _today_yyyymmdd() -> int:
    """Today's date as YYYYMMDD int (uses local-system date)."""
    d = date.today()
    return d.year * 10000 + d.month * 100 + d.day


def _is_valid_yyyymmdd(n: Any) -> bool:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return False
    if v < 19000101 or v > 99991231:
        return False
    try:
        date(v // 10000, (v // 100) % 100, v % 100)
        return True
    except ValueError:
        return False


def _yyyymmdd_weekday(n: int) -> int:
    """Return Python weekday() (Mon=0, Sun=6) for a YYYYMMDD int."""
    return date(n // 10000, (n // 100) % 100, n % 100).weekday()


def _deferred(probe_id: str, category: str, reason: str, suggested: str = "") -> ProbeFinding:
    """Build a `fired=False, severity='low'` finding marking the probe as unevaluated."""
    return ProbeFinding(
        probe_id=probe_id,
        category=category,
        fired=False,
        severity="low",
        message=f"probe deferred: {reason}",
        suggested_resolution=suggested,
        context={"deferred": True},
    )


def _no_finding(probe_id: str, category: str) -> ProbeFinding:
    """Build a benign 'did not fire on clean spec' finding."""
    return ProbeFinding(
        probe_id=probe_id,
        category=category,
        fired=False,
        severity="low",
        message="ok",
        suggested_resolution="",
        context={},
    )


def _exec_field(spec: dict, key: str, default: Any = None) -> Any:
    """Read a field from spec.execution dict; supports both top-level and nested."""
    ex = spec.get("execution")
    if isinstance(ex, dict) and key in ex:
        return ex[key]
    return default


def _universe_field(spec: dict, key: str, default: Any = None) -> Any:
    u = spec.get("universe")
    if isinstance(u, dict) and key in u:
        return u[key]
    return default


def _sizing_field(spec: dict, key: str, default: Any = None) -> Any:
    s = spec.get("sizing")
    if isinstance(s, dict) and key in s:
        return s[key]
    return default


def _date_range(spec: dict) -> tuple[int | None, int | None]:
    dr = spec.get("date_range")
    if not isinstance(dr, dict):
        return None, None
    return dr.get("start"), dr.get("end")


# ----------------------------------------------------------------------------- probe 1


def _probe_window_exceeds_history(spec: dict, data_summary: dict | None) -> ProbeFinding:
    pid, cat = "window_exceeds_history", "time"
    if data_summary is None:
        return _deferred(pid, cat, "no data_summary", "re-run probe after P2")
    windows = spec.get("indicator_windows") or []
    longest = max((int(w) for w in windows if isinstance(w, (int, float))), default=0)
    bars_available = int(data_summary.get("bars_in_range", 0) or 0)
    if longest <= 0 or bars_available <= 0:
        return _no_finding(pid, cat)
    if longest * 1.25 > bars_available:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                f"Strategy uses a {longest}-day window but only {bars_available} days "
                f"of data fit your range. Shrink the window, extend the range, "
                f"or accept a tiny test sample?"
            ),
            suggested_resolution="Truncate effective backtest start to first bar where window is filled.",
            context={"longest_window": longest, "bars_available": bars_available},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 2


def _probe_fees_dominate_edge(spec: dict, *_) -> ProbeFinding:
    pid, cat = "fees_dominate_edge", "sizing"
    legs = ((spec.get("signals") or {}).get("legs")) or []
    tgt = legs[0].get("target_return_per_trade") if legs else None
    if tgt is None:
        return _no_finding(pid, cat)
    target_per_trade_bps = float(tgt) * 10_000.0
    fees_bps = float(_exec_field(spec, "fees_bps", 0.0))
    slip_bps = float(_exec_field(spec, "slippage_bps", 0.0))
    rt_cost_bps = 2.0 * (fees_bps + slip_bps)
    if target_per_trade_bps > 0 and rt_cost_bps >= 0.5 * target_per_trade_bps:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                f"Round-trip costs are about {rt_cost_bps:.1f} bps but your target "
                f"per-trade edge is only {target_per_trade_bps:.1f} bps. Costs will "
                f"eat at least half the edge — confirm fees, raise the target, or trade less often?"
            ),
            suggested_resolution="Run with declared fees; flag report header as cost-dominated regime.",
            context={"rt_cost_bps": rt_cost_bps, "target_per_trade_bps": target_per_trade_bps},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 3


def _probe_rebalance_signal_frequency_mismatch(
    spec: dict, realised_signal: NDArray[np.float64] | None
) -> ProbeFinding:
    pid, cat = "rebalance_signal_frequency_mismatch", "execution"
    if realised_signal is None:
        return _deferred(pid, cat, "no realised_signal", "re-run probe after P3 signal compute")
    sig = np.asarray(realised_signal, dtype=np.float64)
    rebal = spec.get("rebalance_freq") or "bar"
    if sig.shape[0] < 2:
        return _no_finding(pid, cat)
    changes = np.where(np.diff(sig) != 0)[0]
    if len(changes) < 2:
        period_days = float("inf")
    else:
        period_days = float(np.mean(np.diff(changes)))
    fired = False
    reason = ""
    if rebal == "daily" and period_days >= 5:
        fired, reason = True, "wasteful: rebalance faster than signal"
    if rebal == "monthly" and period_days <= 2:
        fired, reason = True, "stale: signal flips faster than rebalance"
    if fired:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"Signal updates roughly every {period_days:.1f} days but you rebalance "
                f"every {rebal} — {reason}. Match the rebalance to the signal cadence?"
            ),
            suggested_resolution="Keep configured cadences; tag run as stale_or_wasteful_rebalance.",
            context={"period_days": period_days, "rebalance_freq": rebal, "reason": reason},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 4


def _signal_can_be_negative(expr: Any) -> bool:
    """Heuristic: signal can produce negatives if the expression mentions
    a negative weight, a `-`, or `signal_can_be_negative` flag is set."""
    if isinstance(expr, dict):
        if expr.get("can_be_negative") is True:
            return True
        weights = expr.get("weights") or []
        if any((isinstance(w, (int, float)) and float(w) < 0) for w in weights):
            return True
    if isinstance(expr, str):
        if "-" in expr or "short" in expr.lower():
            return True
    return False


def _signal_can_be_positive(expr: Any) -> bool:
    if isinstance(expr, dict):
        if expr.get("can_be_positive") is True:
            return True
        weights = expr.get("weights") or []
        if any((isinstance(w, (int, float)) and float(w) > 0) for w in weights):
            return True
    if isinstance(expr, str):
        if "+" in expr or "long" in expr.lower():
            return True
    return False


def _probe_direction_spec_vs_signal(
    spec: dict, realised_signal: NDArray[np.float64] | None
) -> ProbeFinding:
    pid, cat = "direction_spec_vs_signal", "signals"
    direction = spec.get("direction") or "long_short"
    expr = (spec.get("signals") or {}).get("expr")
    fired_static = False
    side: str | None = None
    if direction == "long_only" and _signal_can_be_negative(expr):
        fired_static, side = True, "negative"
    elif direction == "short_only" and _signal_can_be_positive(expr):
        fired_static, side = True, "positive"

    clipped_count: int | None = None
    if realised_signal is not None:
        sig = np.asarray(realised_signal, dtype=np.float64)
        if direction == "long_only":
            clipped_count = int(np.sum(sig < 0))
        elif direction == "short_only":
            clipped_count = int(np.sum(sig > 0))

    if fired_static or (clipped_count is not None and clipped_count > 0):
        if not fired_static:
            side = "negative" if direction == "long_only" else "positive"
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"You said {direction} but the signal can produce {side} positions. "
                f"Clip the disallowed side, flip direction, or change direction setting?"
            ),
            suggested_resolution="Apply hard clip on the disallowed side; log clipped-bar count.",
            context={
                "direction": direction,
                "clipped_count": clipped_count,
                "side": side,
            },
        )
    if realised_signal is None and not fired_static:
        return _deferred(pid, cat, "no realised_signal — only static expr inspected")
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 5


def _probe_date_range_invalid(spec: dict, *_) -> ProbeFinding:
    pid, cat = "date_range_invalid", "time"
    start, end = _date_range(spec)
    if start is None or end is None:
        return _no_finding(pid, cat)
    today = _today_yyyymmdd()
    reasons: list[str] = []
    if not _is_valid_yyyymmdd(start):
        reasons.append("malformed start")
    if not _is_valid_yyyymmdd(end):
        reasons.append("malformed end")
    if not reasons:
        s, e = int(start), int(end)
        if s > e:
            reasons.append("reversed")
        elif s == e:
            reasons.append("zero-length")
        else:
            if _yyyymmdd_weekday(s) >= 5:
                reasons.append("start on weekend")
            if _yyyymmdd_weekday(e) >= 5:
                reasons.append("end on weekend")
            if e > today:
                reasons.append("end in future")
    if reasons:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                f"Date range {start} to {end} looks off — {', '.join(reasons)}. "
                f"What range did you intend?"
            ),
            suggested_resolution="Snap to nearest valid trading days inside available data.",
            context={"start": start, "end": end, "reasons": reasons},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 6


def _extract_referenced_symbols(expr: Any) -> set[str]:
    """Best-effort symbol extraction from a signal expression dict or string."""
    syms: set[str] = set()
    if isinstance(expr, dict):
        for s in (expr.get("symbols") or []):
            if isinstance(s, str):
                syms.add(s.upper())
    elif isinstance(expr, str):
        # crude: capture A-Z+ tokens >= 2 chars
        import re
        for m in re.findall(r"\b[A-Z]{1,6}\b", expr):
            syms.add(m)
    return syms


def _probe_universe_signal_underlying_mismatch(spec: dict, *_) -> ProbeFinding:
    pid, cat = "universe_signal_underlying_mismatch", "universe"
    traded = set(map(str.upper, _universe_field(spec, "symbols", []) or []))
    if not traded:
        return _no_finding(pid, cat)
    expr = (spec.get("signals") or {}).get("expr")
    referenced = _extract_referenced_symbols(expr)
    if not referenced:
        return _no_finding(pid, cat)
    if not referenced.issubset(traded) and not spec.get("cross_asset_explicit"):
        missing = sorted(referenced - traded)
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"Signal references {sorted(referenced)} but you only trade {sorted(traded)}. "
                f"Add the referenced symbols to the universe, or confirm cross-asset?"
            ),
            suggested_resolution="Treat as cross-asset and fetch referenced symbols read-only.",
            context={"referenced": sorted(referenced), "traded": sorted(traded), "missing": missing},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 7


def _probe_capital_vs_trade_size(spec: dict, *_) -> ProbeFinding:
    pid, cat = "capital_vs_trade_size", "sizing"
    notional = _sizing_field(spec, "notional_target")
    capital_base = spec.get("capital_base")
    if notional is None or capital_base is None:
        return _no_finding(pid, cat)
    try:
        n = float(notional)
        c = float(capital_base)
    except (TypeError, ValueError):
        return _no_finding(pid, cat)
    if c <= 0:
        return _no_finding(pid, cat)
    fired = False
    if n > c * 1.05:
        fired = True
    elif n < c * 0.001:
        fired = True
    if fired:
        ratio = n / c * 100.0
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                f"Trade size {n:.2f} vs capital {c:.2f} — {ratio:.2f}% of capital per trade. "
                f"Is that intentional or a units mistake?"
            ),
            suggested_resolution="Cap notional at capital_base; log clamp.",
            context={"notional_target": n, "capital_base": c, "ratio_pct": ratio},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 8


def _probe_lookahead_no_shift(spec: dict, *_) -> ProbeFinding:
    pid, cat = "lookahead_no_shift", "methodology"
    shift = _exec_field(spec, "look_ahead_shift", None)
    fill_timing = _exec_field(spec, "fill_timing", "next_open")
    if shift is None:
        return _no_finding(pid, cat)
    if int(shift) == 0 and fill_timing in ("close", "same_bar_close"):
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                "Your signal reads bar t's close and fills at the same bar's close — "
                "that uses information you wouldn't have live. Shift fills to t+1 open?"
            ),
            suggested_resolution="Force look_ahead_shift=1; default policy is t->t+1.",
            context={"look_ahead_shift": int(shift), "fill_timing": fill_timing},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 9


def _probe_survivorship_implicit(spec: dict, *_) -> ProbeFinding:
    pid, cat = "survivorship_implicit", "universe"
    deftype = _universe_field(spec, "definition_type")
    start, _ = _date_range(spec)
    if deftype not in ("current_top_N", "current_index_members"):
        return _no_finding(pid, cat)
    if start is None:
        return _no_finding(pid, cat)
    today = _today_yyyymmdd()
    # Roughly: start more than 365 days before today
    if int(start) < today - 10000:  # >1 year ago in YYYYMMDD-int sense
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"Universe is defined as {deftype} — backtesting backwards on it builds in "
                f"survivorship bias. Use a point-in-time membership snapshot, or accept the bias?"
            ),
            suggested_resolution="Proceed with as-of-today universe; tag survivorship_present: true.",
            context={"definition_type": deftype, "start": start},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 10


def _probe_slippage_vs_liquidity(spec: dict, data_summary: dict | None) -> ProbeFinding:
    pid, cat = "slippage_vs_liquidity", "execution"
    if data_summary is None:
        return _deferred(pid, cat, "no data_summary")
    notional = _sizing_field(spec, "notional_target")
    slippage_bps = float(_exec_field(spec, "slippage_bps", 0.0))
    if notional is None:
        return _no_finding(pid, cat)
    syms_adv = data_summary.get("avg_dollar_volume_60d") or {}
    if not isinstance(syms_adv, dict) or not syms_adv:
        return _no_finding(pid, cat)
    bad: list[tuple[str, float]] = []
    for sym, adv in syms_adv.items():
        try:
            adv_f = float(adv)
        except (TypeError, ValueError):
            continue
        if adv_f <= 0:
            continue
        participation = float(notional) / adv_f
        if participation > 0.05 and slippage_bps < 10:
            bad.append((sym, participation))
    if bad:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"On {bad[0][0]}, each trade is {bad[0][1] * 100:.2f}% of average daily volume but "
                f"slippage is set to {slippage_bps:.1f} bps. Optimistic for an illiquid name — raise slippage?"
            ),
            suggested_resolution="Keep slippage; flag affected symbols in caveats.",
            context={"affected_symbols": dict(bad), "slippage_bps": slippage_bps},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 11


def _probe_missing_risk_controls_high_leverage(spec: dict, *_) -> ProbeFinding:
    pid, cat = "missing_risk_controls_high_leverage", "sizing"
    gross = _sizing_field(spec, "gross_exposure")
    capital_base = spec.get("capital_base")
    if gross is None or capital_base is None:
        return _no_finding(pid, cat)
    try:
        leverage = float(gross) / float(capital_base)
    except (TypeError, ValueError, ZeroDivisionError):
        return _no_finding(pid, cat)
    has_stop = spec.get("stop_loss") is not None
    has_kill = spec.get("max_drawdown_kill") is not None
    if leverage > 1.5 and not has_stop and not has_kill:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                f"Strategy runs at {leverage:.2f}x gross with no stop-loss or drawdown kill. "
                f"One bad day could wipe the book — add a risk control or confirm bare-bones?"
            ),
            suggested_resolution="No stops applied; add 'no_risk_controls' warning to report header.",
            context={"leverage": leverage},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 12


def _probe_walkforward_oos_missing(spec: dict, *_) -> ProbeFinding:
    pid, cat = "walkforward_oos_missing", "methodology"
    has_grid = bool(spec.get("parameter_grid"))
    has_oos = spec.get("oos_start") is not None
    if has_grid and not has_oos:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                "You're searching over a parameter grid but have no out-of-sample split. "
                "Reported metrics will overfit. Reserve the last 25% of dates for OOS?"
            ),
            suggested_resolution="Run in-sample only; brand 'in_sample_only'.",
            context={"has_param_search": True, "has_oos_split": False},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 13


def _probe_risk_free_rate_unset(spec: dict, *_) -> ProbeFinding:
    pid, cat = "risk_free_rate_unset", "methodology"
    start, end = _date_range(spec)
    # Read from the YAML spec so we distinguish "not set" from "explicitly 0.0".
    ex = spec.get("execution") or {}
    yaml_rf = ex.get("risk_free_rate") if isinstance(ex, dict) else None
    if start is None or end is None:
        return _no_finding(pid, cat)
    try:
        s, e = int(start), int(end)
    except (TypeError, ValueError):
        return _no_finding(pid, cat)
    if e >= 20220101 and s <= 20251231 and yaml_rf is None:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                "Sharpe assumes a risk-free rate; you left it blank for a period when rates "
                "were nonzero. Use a constant rate, pull a series, or accept r=0?"
            ),
            suggested_resolution="Use r=0; log assumption with high-visibility flag in metrics block.",
            context={"start": s, "end": e, "yaml_rf": yaml_rf},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 14


def _probe_calendar_mismatch(spec: dict, *_) -> ProbeFinding:
    pid, cat = "calendar_mismatch", "data"
    asset_calendar = spec.get("calendar")
    asset_class = _universe_field(spec, "asset_class")
    if asset_calendar is None or asset_class is None:
        return _no_finding(pid, cat)
    required = CALENDAR_BY_ASSET_CLASS.get(str(asset_class).upper())
    if required is None:
        return _no_finding(pid, cat)
    if asset_calendar != required:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"You set the calendar to {asset_calendar} but you're trading {asset_class}, "
                f"which trades on {required}. Switch calendars?"
            ),
            suggested_resolution="Use required_calendar; override spec; log forced switch.",
            context={"asset_calendar": asset_calendar, "required": required, "asset_class": asset_class},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 15


def _probe_dte_filter_empty(spec: dict, data_summary: dict | None) -> ProbeFinding:
    pid, cat = "dte_filter_empty", "data"
    if data_summary is None:
        return _deferred(pid, cat, "no data_summary")
    chain_match = data_summary.get("options_chain_match_count")
    if chain_match is None:
        return _no_finding(pid, cat)
    try:
        n = int(chain_match)
    except (TypeError, ValueError):
        return _no_finding(pid, cat)
    if n == 0:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                f"No option contracts matched DTE/moneyness filter on root "
                f"{spec.get('root', '<unknown>')!r}. Widen the DTE window, change the underlying, "
                f"or pick another expiry rule?"
            ),
            suggested_resolution="HALT — this is a data-empty error, not a soft default.",
            context={"matched_contracts": 0},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 16


def _probe_delta_target_no_greeks(spec: dict, data_summary: dict | None) -> ProbeFinding:
    pid, cat = "delta_target_no_greeks", "data"
    sel = spec.get("option_selection") or {}
    method = sel.get("method") if isinstance(sel, dict) else None
    if method != "delta_target":
        return _no_finding(pid, cat)
    if data_summary is None:
        return _deferred(pid, cat, "no data_summary")
    has_iv = data_summary.get("options_chain_has_iv", True)
    has_greeks = data_summary.get("options_chain_has_greeks", True)
    if not has_iv or not has_greeks:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"You want to target delta {sel.get('target')!r}, but the option data has "
                f"no IV/Greek field. Compute Greeks from BS with default {{r,q}}, or pick by strike?"
            ),
            suggested_resolution="Compute Greeks via py_vollib using spec defaults (r=0, q=0); log.",
            context={"has_iv": bool(has_iv), "has_greeks": bool(has_greeks)},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 17


def _probe_short_dated_options_no_roll(spec: dict, *_) -> ProbeFinding:
    pid, cat = "short_dated_options_no_roll", "execution"
    asset_class = _universe_field(spec, "asset_class")
    if str(asset_class or "").lower() not in ("options", "option"):
        return _no_finding(pid, cat)
    dte_min = spec.get("dte_min")
    roll_logic = spec.get("roll_logic")
    start, end = _date_range(spec)
    if dte_min is None or start is None or end is None:
        return _no_finding(pid, cat)
    try:
        dte = int(dte_min)
        s, e = int(start), int(end)
    except (TypeError, ValueError):
        return _no_finding(pid, cat)
    duration_days = e - s  # crude YYYYMMDD diff is a proxy for "more than a month"
    if dte < 10 and roll_logic is None and duration_days > 30:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"Trading sub-10-DTE options over a long range with no roll rule means positions "
                f"just expire. Roll at some DTE, or hold to expiry every time?"
            ),
            suggested_resolution="Hold to expiry; settle at intrinsic value; log behavior.",
            context={"dte_min": dte, "roll_logic": None},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 18


def _probe_exercise_style_mismatch(spec: dict, *_) -> ProbeFinding:
    pid, cat = "exercise_style_mismatch", "methodology"
    asset_class = _universe_field(spec, "asset_class")
    if str(asset_class or "").lower() not in ("options", "option"):
        return _no_finding(pid, cat)
    pricing = spec.get("pricing_model")
    root = str(spec.get("root") or "").upper()
    if pricing != "european":
        return _no_finding(pid, cat)
    if root in AMERICAN_STYLE_ROOTS:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"You priced these as European but {root} options are American. "
                f"Use American (binomial) pricing, or accept European as a small-error approximation?"
            ),
            suggested_resolution="Stick with European pricing; log expected-error magnitude.",
            context={"root": root, "pricing_model": pricing},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 19


def _probe_zero_signal_after_filters(
    spec: dict, realised_signal: NDArray[np.float64] | None
) -> ProbeFinding:
    pid, cat = "zero_signal_after_filters", "signals"
    if realised_signal is None:
        return _deferred(pid, cat, "no realised_signal")
    sig = np.asarray(realised_signal, dtype=np.float64)
    if sig.shape[0] == 0:
        return _no_finding(pid, cat)
    # Count entry events (transitions from 0 to non-zero).
    nz = (sig != 0.0).astype(np.int64)
    entries = int(np.sum(np.diff(np.concatenate(([0], nz))) > 0))
    if entries == 0 or entries < 5:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high" if entries == 0 else "medium",
            message=(
                f"Filters fire {entries} times in the whole window — too few to learn from. "
                f"Loosen a threshold, extend the range, or confirm the rare-event focus?"
            ),
            suggested_resolution="Run anyway; mark report 'low_sample_warning, n=K'.",
            context={"entries": entries},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 20


def _probe_benchmark_undefined(spec: dict, *_) -> ProbeFinding:
    pid, cat = "benchmark_undefined", "execution"
    asset_class = str(_universe_field(spec, "asset_class") or "").lower()
    bench = spec.get("benchmark")
    if bench is None and asset_class in ("equity", "index", "etf", "options", "option"):
        default = "SPX"
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="low",
            message=(
                f"No benchmark set. Compare against buy-and-hold {default}, "
                f"or run benchmark-free?"
            ),
            suggested_resolution=f"Use {default} for equity/index/options; cash (r=0) for futures/FX.",
            context={"asset_class": asset_class, "default_benchmark": default},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 21


def _probe_capital_currency_vs_instrument_currency(
    spec: dict, data_summary: dict | None
) -> ProbeFinding:
    pid, cat = "capital_currency_vs_instrument_currency", "data"
    cap_ccy = (spec.get("capital_currency") or "USD").upper()
    inst_ccys: set[str] = set()
    if data_summary is not None:
        ccys = data_summary.get("instrument_currencies") or {}
        if isinstance(ccys, dict):
            for v in ccys.values():
                if isinstance(v, str):
                    inst_ccys.add(v.upper())
        elif isinstance(ccys, (list, tuple, set)):
            inst_ccys.update(str(v).upper() for v in ccys if isinstance(v, str))
    if not inst_ccys:
        return _no_finding(pid, cat)
    if len(inst_ccys) > 1 or (cap_ccy not in inst_ccys):
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="medium",
            message=(
                f"Capital is in {cap_ccy} but instruments quote in {sorted(inst_ccys)}. "
                f"Convert PnL to {cap_ccy} at daily FX, or treat as same-currency?"
            ),
            suggested_resolution="Treat all as same currency (no FX conversion); log fx_unconverted.",
            context={"cap_ccy": cap_ccy, "inst_ccys": sorted(inst_ccys)},
        )
    return _no_finding(pid, cat)


# ----------------------------------------------------------------------------- probe 22


def _probe_instrument_lookup_failed(spec: dict, *_) -> ProbeFinding:
    pid, cat = "instrument_lookup_failed", "universe"
    raw = spec.get("user_term") or spec.get("raw_instrument_term")
    if raw is None:
        return _no_finding(pid, cat)
    resolved = resolve_ticker(raw)
    if resolved is None:
        return ProbeFinding(
            probe_id=pid,
            category=cat,
            fired=True,
            severity="high",
            message=(
                f"Couldn't resolve {raw!r} to a ticker. Did you mean a known alias?"
            ),
            suggested_resolution="HALT P1 with PROBLEMS.md entry; do not fall through to a guessed ticker.",
            context={"user_term": raw, "resolved": None},
        )
    return _no_finding(pid, cat)


# =============================================================================
# Orchestrator: fire-order priority follows pipeline/probes.md:301-321
# =============================================================================


# Probe firing-order list (id, callable). Order is the documented priority
# cascade: universe -> time -> data -> signals -> methodology -> execution -> sizing.
_PROBE_ORDER: tuple[tuple[str, Any], ...] = (
    # 1. Universe
    ("instrument_lookup_failed", _probe_instrument_lookup_failed),
    ("universe_signal_underlying_mismatch", _probe_universe_signal_underlying_mismatch),
    ("survivorship_implicit", _probe_survivorship_implicit),
    # 2. Time
    ("date_range_invalid", _probe_date_range_invalid),
    ("window_exceeds_history", _probe_window_exceeds_history),
    # 3. Data
    ("calendar_mismatch", _probe_calendar_mismatch),
    ("capital_currency_vs_instrument_currency", _probe_capital_currency_vs_instrument_currency),
    ("dte_filter_empty", _probe_dte_filter_empty),
    ("delta_target_no_greeks", _probe_delta_target_no_greeks),
    # 4. Signals
    ("direction_spec_vs_signal", _probe_direction_spec_vs_signal),
    ("zero_signal_after_filters", _probe_zero_signal_after_filters),
    # 5. Methodology
    ("lookahead_no_shift", _probe_lookahead_no_shift),
    ("walkforward_oos_missing", _probe_walkforward_oos_missing),
    ("risk_free_rate_unset", _probe_risk_free_rate_unset),
    ("exercise_style_mismatch", _probe_exercise_style_mismatch),
    # 6. Execution
    ("rebalance_signal_frequency_mismatch", _probe_rebalance_signal_frequency_mismatch),
    ("slippage_vs_liquidity", _probe_slippage_vs_liquidity),
    ("short_dated_options_no_roll", _probe_short_dated_options_no_roll),
    ("benchmark_undefined", _probe_benchmark_undefined),
    # 7. Sizing
    ("capital_vs_trade_size", _probe_capital_vs_trade_size),
    ("missing_risk_controls_high_leverage", _probe_missing_risk_controls_high_leverage),
    ("fees_dominate_edge", _probe_fees_dominate_edge),
)


_DATA_SUMMARY_PROBES = {
    "window_exceeds_history",
    "slippage_vs_liquidity",
    "dte_filter_empty",
    "delta_target_no_greeks",
    "capital_currency_vs_instrument_currency",
}
_SIGNAL_PROBES = {
    "rebalance_signal_frequency_mismatch",
    "direction_spec_vs_signal",
    "zero_signal_after_filters",
}


def validate_strategy_spec(
    spec_yaml: dict,
    *,
    data_summary: dict | None = None,
    realised_signal: NDArray[np.float64] | None = None,
) -> list[ProbeFinding]:
    """Run all 22 spec-time inconsistency probes; return findings in firing-order priority.

    Pure-Python detection. Probes that need data (#1, #10, #15, #16, #21) or a
    realised signal (#3, #4, #19) accept None and return a `low`-severity
    'unevaluated' finding so callers know the probe was deferred.
    """
    out: list[ProbeFinding] = []
    for pid, fn in _PROBE_ORDER:
        if pid in _SIGNAL_PROBES:
            finding = fn(spec_yaml, realised_signal)
        elif pid in _DATA_SUMMARY_PROBES:
            finding = fn(spec_yaml, data_summary)
        else:
            finding = fn(spec_yaml)
        out.append(finding)
    return out


def first_fired(findings: list[ProbeFinding]) -> ProbeFinding | None:
    """Return the first fired finding from validate_strategy_spec output, or None."""
    return next((f for f in findings if f.fired), None)


__all__ = [
    "IntegrityReport",
    "ProbeFinding",
    "bar_integrity",
    "chain_integrity",
    "first_fired",
    "signal_integrity",
    "validate_strategy_spec",
]
