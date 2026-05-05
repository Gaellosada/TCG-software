"""Bar / chain / signal integrity checks plus generic behavioural probes.

The lib is helpers, not gatekeepers. Strategy-level validation is generic and
behavioural — see :func:`run_probes`. The lib does not enforce a closed
taxonomy of strategy "kinds"; it asserts properties any sensible strategy
must satisfy (META well-formedness, finite signal past warm-up, no
look-ahead, bounded position, determinism, recorded dependencies).

Three-level severity for ``IntegrityReport``:
  PASS = no failures, no warnings.
  WARN = no failures, warnings present (proceed with caveat).
  FAIL = failures present (abort).
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from .data_load import OptionChainSnapshot, OptionContractSeries, PriceSeries


@dataclass(frozen=True)
class IntegrityReport:
    """Outcome of an integrity check.

    Three-level severity: PASS (clean) / WARN (soft, pipeline continues) /
    FAIL (hard, pipeline must abort). The ``ok`` field is the canonical abort
    signal; ``failures`` lists the hard errors, ``warnings`` lists soft
    issues. ``severity`` is derived from the two.
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
        """Return a one-line human-readable summary (FAIL / WARN / OK)."""
        if not self.ok and self.failures:
            return f"FAIL: {self.failures[0]}"
        n = int(self.checks.get("n_bars", 0))
        gaps = int(self.checks.get("gaps", 0))
        nans = int(self.checks.get("nan_close", 0))
        if self.warnings:
            return f"WARN: {self.warnings[0]} ({n} bars, {gaps} gaps, {nans} NaNs)"
        return f"OK: {n} bars, {gaps} gaps, {nans} NaNs"


def first_fired(report: IntegrityReport) -> str | None:
    """Return the first failure or warning message in the report, or ``None``.

    Mirrors the binance ``first_fired`` shape: callers check the report
    once and surface the first issue without iterating list-of-findings.
    """
    if report.failures:
        return report.failures[0]
    if report.warnings:
        return report.warnings[0]
    return None


def _yyyymmdd_to_date(d: int) -> date:
    n = int(d)
    return date(n // 10000, (n // 100) % 100, n % 100)


# Calendar-gap severity boundary (10% ceiling for FAIL; below is WARN).
_GAP_WARN_FRACTION = 0.10


# =============================================================================
# Bar / chain / signal integrity (data-quality probes; survive the pivot)
# =============================================================================


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

    asof_arr = np.asarray(asof_dates, dtype=np.int64)
    if asof_arr.size >= 2 and np.any(np.diff(asof_arr) <= 0):
        failures.append("snapshot asof_date not strictly increasing (duplicates or out-of-order)")

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
# Generic behavioural probes — run_probes(strategy_module, bars, result)
# =============================================================================
#
# Six universal checks. Adding a new "kind" of strategy must NOT require a
# probe change. Probes assert behaviour, not shape.

_REQUIRED_META_KEYS = ("slug", "dates", "universe", "benchmark")
_STDLIB_PACKAGE_NAMES: frozenset[str] = frozenset(sys.stdlib_module_names)


def _read_pyproject_deps() -> frozenset[str]:
    """Return top-level package names declared in the lib's ``pyproject.toml``.

    These are always installed alongside ``tcg_backtester`` and so do not
    need re-declaring in a workspace's ``requirements.txt``. The probe
    treats them as exempt.
    """
    try:
        import tomllib  # py311+
    except ImportError:  # pragma: no cover
        return frozenset()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return frozenset()
            deps = data.get("project", {}).get("dependencies", []) or []
            out: set[str] = set()
            for line in deps:
                m = re.match(r"^([A-Za-z0-9_\-.]+)", str(line).strip())
                if m:
                    out.add(m.group(1).lower().replace("-", "_"))
            return frozenset(out)
    return frozenset()


_BUILTIN_OK = frozenset({"numpy", "tcg_backtester", "lib"}) | _read_pyproject_deps()


def _probe_meta_schema(
    strategy_module: ModuleType,
    failures: list[str],
    warnings: list[str],
    checks: dict[str, Any],
) -> dict | None:
    """Probe: META present and required keys are well-formed."""
    meta = getattr(strategy_module, "META", None)
    if meta is None:
        failures.append("meta_schema: strategy module is missing top-level META dict")
        return None
    if not isinstance(meta, dict):
        failures.append(f"meta_schema: META must be a dict, got {type(meta).__name__}")
        return None
    for key in _REQUIRED_META_KEYS:
        if key not in meta:
            failures.append(f"meta_schema: META is missing required key {key!r}")
    slug = meta.get("slug")
    if slug is not None and (not isinstance(slug, str) or not slug.strip()):
        failures.append("meta_schema: META['slug'] must be a non-empty string")
    dates = meta.get("dates")
    if dates is not None:
        if not isinstance(dates, dict):
            failures.append("meta_schema: META['dates'] must be a dict with start/end")
        else:
            for k in ("start", "end"):
                if k not in dates:
                    failures.append(f"meta_schema: META['dates'] missing key {k!r}")
    universe = meta.get("universe")
    if universe is not None:
        if isinstance(universe, str):
            if not universe.strip():
                failures.append("meta_schema: META['universe'] string is empty")
        elif isinstance(universe, (list, tuple)):
            if not universe:
                failures.append("meta_schema: META['universe'] is an empty list")
        else:
            failures.append(
                f"meta_schema: META['universe'] must be a string or non-empty "
                f"list, got {type(universe).__name__}"
            )
    benchmark = meta.get("benchmark")
    if benchmark is not None and not isinstance(benchmark, (str, dict)):
        failures.append(
            f"meta_schema: META['benchmark'] must be a string or dict, got "
            f"{type(benchmark).__name__}"
        )
    checks["meta_schema_keys"] = sorted(meta.keys())
    return meta


def _signal_array_from_result(result: Any) -> np.ndarray | None:
    if result is None:
        return None
    spec = getattr(result, "spec", None)
    if spec is not None:
        sig = getattr(spec, "signal", None)
        if sig is not None:
            return np.asarray(sig, dtype=np.float64).reshape(-1)
    sig = getattr(result, "signal", None)
    if sig is not None:
        return np.asarray(sig, dtype=np.float64).reshape(-1)
    return None


def _probe_signal_finite_past_warmup(
    bars: PriceSeries,
    result: Any,
    failures: list[str],
    warnings: list[str],
    checks: dict[str, Any],
) -> None:
    """Probe: first N rows may be NaN; after warm-up, no NaN, no inf."""
    sig = _signal_array_from_result(result)
    if sig is None:
        return
    n = sig.shape[0]
    if n == 0:
        warnings.append("signal_finite_past_warmup: signal is empty")
        return
    finite_mask = ~np.isnan(sig)
    if not finite_mask.any():
        failures.append("signal_finite_past_warmup: signal is entirely NaN")
        return
    first_finite = int(np.argmax(finite_mask))
    tail = sig[first_finite:]
    n_nan_tail = int(np.sum(np.isnan(tail)))
    n_inf_tail = int(np.sum(np.isinf(tail)))
    checks["signal_warmup_n"] = first_finite
    checks["signal_n_nan_post_warmup"] = n_nan_tail
    checks["signal_n_inf"] = n_inf_tail
    if n_nan_tail > 0:
        failures.append(
            f"signal_finite_past_warmup: {n_nan_tail} NaN value(s) after "
            f"warmup index {first_finite}"
        )
    if n_inf_tail > 0:
        failures.append(
            f"signal_finite_past_warmup: {n_inf_tail} infinite value(s) in signal"
        )


def _slice_bars(bars: PriceSeries, upto_inclusive: int) -> PriceSeries:
    """Return ``bars[:upto_inclusive+1]`` preserving the PriceSeries shape."""
    j = upto_inclusive + 1
    return PriceSeries(
        instrument_id=bars.instrument_id,
        provider=bars.provider,
        dates=bars.dates[:j],
        open=bars.open[:j],
        high=bars.high[:j],
        low=bars.low[:j],
        close=bars.close[:j],
        volume=bars.volume[:j],
        meta=getattr(bars, "meta", {}),
    )


def _is_options_strategy(strategy_module: ModuleType) -> bool:
    """Heuristic: a ``run``-shape strategy is treated as options-aware (the
    ``no_lookahead`` probe is N/A — selectors resolve at engine time, the
    causal contract is enforced by ``execution.look_ahead_shift``)."""
    return callable(getattr(strategy_module, "run", None))


def _probe_no_lookahead(
    strategy_module: ModuleType,
    bars: PriceSeries,
    failures: list[str],
    warnings: list[str],
    checks: dict[str, Any],
    *,
    workspace_path: Path,
    max_bars_for_lookahead_check: int | None = 500,
) -> None:
    """Probe: ``compute_signal(bars[:i+1], ctx)[i] == compute_signal(bars, ctx)[i]``.

    Skipped for ``run``-shape strategies (selectors resolve at engine time;
    the causal contract is enforced by ``execution.look_ahead_shift``).
    Sample 5 indices in mid-range.

    ``max_bars_for_lookahead_check`` (default 500) caps the bar-window used for
    the probe. When ``compute_signal`` is slow per call (e.g. walk-forward HMM
    fitting), running on a 2 500-bar window × 6 calls is painful. Truncating to
    500 bars keeps the probe under ~5 s for typical strategies. Set to ``None``
    to disable the cap and run on the full bar series.
    """
    if _is_options_strategy(strategy_module):
        checks["no_lookahead_sampled"] = 0
        checks["no_lookahead_skipped"] = "run-shape strategy"
        return
    compute = getattr(strategy_module, "compute_signal", None)
    if not callable(compute):
        return
    n = int(bars.dates.shape[0])
    if n < 10:
        return

    # Cap the probe window to limit runtime for slow compute_signal implementations
    # (e.g. HMM strategies that refit on every prefix). Correctness is preserved:
    # look-ahead leakage is a local property — if bars[:500] reveals it, the
    # full-length run has it too. A warning is emitted when the cap is applied.
    if max_bars_for_lookahead_check is not None and n > max_bars_for_lookahead_check:
        bars = _slice_bars(bars, max_bars_for_lookahead_check - 1)
        n = max_bars_for_lookahead_check
        warnings.append(
            f"no_lookahead: bar series truncated to {max_bars_for_lookahead_check} bars "
            f"for probe (max_bars_for_lookahead_check={max_bars_for_lookahead_check}); "
            f"set max_bars_for_lookahead_check=None to probe on the full series"
        )

    from .strategy import _build_ctx  # noqa: WPS437

    meta = getattr(strategy_module, "META", {}) or {}
    slug = str(meta.get("slug") or workspace_path.name)
    full_ctx = _build_ctx(
        workspace_path=workspace_path, meta=meta, bars=bars, slug=slug
    )
    try:
        full_signal = np.asarray(compute(bars, full_ctx), dtype=np.float64).reshape(-1)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"no_lookahead: compute_signal raised on full bars: {exc}")
        return
    if full_signal.shape[0] != n:
        failures.append(
            f"no_lookahead: compute_signal returned length {full_signal.shape[0]} "
            f"!= bars length {n}"
        )
        return
    lo = max(2, int(0.1 * n))
    hi = min(n - 1, int(0.95 * n))
    if hi - lo < 5:
        idxs = list(range(lo, hi))
    else:
        idxs = list(np.linspace(lo, hi - 1, 5, dtype=int))
    mismatches = 0
    for i in idxs:
        sliced = _slice_bars(bars, i)
        sliced_ctx = _build_ctx(
            workspace_path=workspace_path, meta=meta, bars=sliced, slug=slug
        )
        try:
            partial = np.asarray(
                compute(sliced, sliced_ctx), dtype=np.float64
            ).reshape(-1)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"no_lookahead: compute_signal(bars[:{i + 1}]) raised: {exc}"
            )
            return
        if partial.shape[0] != i + 1:
            failures.append(
                f"no_lookahead: compute_signal on bars[:{i + 1}] returned "
                f"length {partial.shape[0]}, expected {i + 1}"
            )
            return
        a = full_signal[i]
        b = partial[i]
        if np.isnan(a) and np.isnan(b):
            continue
        if not np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True):
            mismatches += 1
    checks["no_lookahead_sampled"] = len(idxs)
    checks["no_lookahead_mismatches"] = mismatches
    if mismatches > 0:
        failures.append(
            f"no_lookahead: {mismatches}/{len(idxs)} sampled indices have "
            f"signal[i] depending on bars[t>i] (look-ahead leak)"
        )


def _probe_position_bounded(
    result: Any,
    failures: list[str],
    warnings: list[str],
    checks: dict[str, Any],
    *,
    capital_base: float,
    factor: float,
) -> None:
    """Probe: ``|position|`` <= factor * capital_base by default.

    Uses ``result.positions`` (mongoDB engine name). The mongoDB engine
    stores ``positions`` as a fractional weight (target / cap), so the
    bound check multiplies through capital_base to give a dollar bound.
    """
    if result is None:
        return
    positions = getattr(result, "positions", None)
    if positions is None:
        return
    pos = np.asarray(positions, dtype=np.float64)
    if pos.size == 0:
        return
    abs_max = float(np.max(np.abs(pos)))
    abs_max_dollar = abs_max * capital_base
    bound = factor * capital_base
    checks["position_abs_max"] = abs_max_dollar
    checks["position_bound"] = bound
    if abs_max_dollar > bound:
        failures.append(
            f"position_bounded: |position| max {abs_max_dollar:.4g} exceeds "
            f"{factor}× capital_base ({bound:.4g})"
        )


def _probe_deterministic(
    strategy_module: ModuleType,
    bars: PriceSeries,
    failures: list[str],
    warnings: list[str],
    checks: dict[str, Any],
    *,
    workspace_path: Path,
) -> None:
    """Probe: two calls with the same inputs return arrays with ``array_equal``."""
    if _is_options_strategy(strategy_module):
        checks["deterministic_skipped"] = "run-shape strategy"
        return
    compute = getattr(strategy_module, "compute_signal", None)
    if not callable(compute):
        return
    from .strategy import _build_ctx  # noqa: WPS437

    meta = getattr(strategy_module, "META", {}) or {}
    slug = str(meta.get("slug") or workspace_path.name)
    seed = meta.get("seed")
    if isinstance(seed, int):
        np.random.seed(seed)
    ctx = _build_ctx(workspace_path=workspace_path, meta=meta, bars=bars, slug=slug)
    try:
        a = np.asarray(compute(bars, ctx), dtype=np.float64)
        if isinstance(seed, int):
            np.random.seed(seed)
        b = np.asarray(compute(bars, ctx), dtype=np.float64)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"deterministic: compute_signal raised on second call: {exc}")
        return
    if a.shape != b.shape:
        failures.append(
            f"deterministic: shape changed across calls ({a.shape} vs {b.shape})"
        )
        return
    if not np.array_equal(a, b, equal_nan=True):
        n_diff = int(np.sum(a != b))
        failures.append(
            f"deterministic: compute_signal output differs across calls "
            f"({n_diff} elements differ)"
        )


def _strategy_top_level_imports(strategy_module: ModuleType) -> set[str]:
    """Return the top-level package names a strategy module imports."""
    src_path = getattr(strategy_module, "__file__", None)
    if not src_path or not os.path.isfile(src_path):
        return set()
    try:
        source = Path(src_path).read_text(encoding="utf-8")
    except OSError:
        return set()
    pkgs: set[str] = set()
    try:
        tree = ast.parse(source, filename=src_path)
    except SyntaxError:
        return set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                pkgs.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                pkgs.add(node.module.split(".")[0])
    return pkgs


def _probe_dependency_recorded(
    strategy_module: ModuleType,
    workspace_path: Path,
    failures: list[str],
    warnings: list[str],
    checks: dict[str, Any],
) -> None:
    """Probe: non-stdlib non-numpy imports must appear in workspace ``requirements.txt``.

    Skipped if no ``requirements.txt`` exists. The lib's pyproject deps
    (numpy / tcg_backtester / lib itself) are exempt.
    """
    req_path = workspace_path / "requirements.txt"
    if not req_path.is_file():
        return
    try:
        req_lines = req_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        warnings.append(f"dependency_recorded: could not read requirements.txt: {exc}")
        return
    declared: set[str] = set()
    for raw_line in req_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_\-.]+)", line)
        if m:
            declared.add(m.group(1).lower().replace("-", "_"))
    imports = _strategy_top_level_imports(strategy_module)
    missing: list[str] = []
    for pkg in sorted(imports):
        norm = pkg.lower().replace("-", "_")
        if pkg in _STDLIB_PACKAGE_NAMES or norm in _STDLIB_PACKAGE_NAMES:
            continue
        if norm in _BUILTIN_OK:
            continue
        if norm in declared:
            continue
        missing.append(pkg)
    checks["dependency_recorded_missing"] = missing
    if missing:
        failures.append(
            "dependency_recorded: strategy imports "
            + ", ".join(missing)
            + " but they are not in requirements.txt"
        )


def run_probes(
    strategy_module: ModuleType,
    bars: PriceSeries | None,
    result: Any,
    *,
    workspace_path: Path | str | None = None,
    capital_base: float = 100_000.0,
    position_bound_factor: float = 10.0,
    max_bars_for_lookahead_check: int | None = 500,
) -> IntegrityReport:
    """Run generic behavioural probes against a strategy module.

    Probes:
      - ``meta_schema`` — META present + required keys well-formed.
      - ``signal_finite_past_warmup`` — no NaN/inf after warm-up.
      - ``no_lookahead`` — sampled compute_signal is causal (skip if run-shape).
      - ``position_bounded`` — |position| <= 10× capital_base by default.
      - ``deterministic`` — two calls with the same inputs match (skip if run-shape).
      - ``dependency_recorded`` — non-stdlib non-numpy imports declared in
        ``workspace/requirements.txt``. Skipped if no requirements.txt.

    ``max_bars_for_lookahead_check`` (default 500) caps the number of bars used by
    the ``no_lookahead`` probe. Walk-forward strategies (e.g. HMM) can make each
    ``compute_signal`` call slow; truncating the probe window bounds the overhead.
    Set to ``None`` to probe on the full bar series.
    """
    failures: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    ws_path = (
        Path(workspace_path).resolve()
        if workspace_path is not None
        else Path.cwd().resolve()
    )

    meta = _probe_meta_schema(strategy_module, failures, warnings, checks)

    if bars is not None:
        _probe_signal_finite_past_warmup(bars, result, failures, warnings, checks)
        if meta is not None:
            _probe_no_lookahead(
                strategy_module, bars, failures, warnings, checks,
                workspace_path=ws_path,
                max_bars_for_lookahead_check=max_bars_for_lookahead_check,
            )
            _probe_deterministic(
                strategy_module, bars, failures, warnings, checks,
                workspace_path=ws_path,
            )

    _probe_position_bounded(
        result, failures, warnings, checks,
        capital_base=capital_base, factor=position_bound_factor,
    )

    _probe_dependency_recorded(
        strategy_module, ws_path, failures, warnings, checks,
    )

    return IntegrityReport(
        ok=not failures,
        failures=tuple(failures),
        checks=checks,
        warnings=tuple(warnings),
    )


__all__ = [
    "IntegrityReport",
    "bar_integrity",
    "chain_integrity",
    "first_fired",
    "run_probes",
    "signal_integrity",
]
