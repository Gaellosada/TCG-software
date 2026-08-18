"""Reusable per-leg VALIDATION HARNESS (strategy-repro Wave 0).

Given a leg's daily equity curve (or a persisted-entity run), this module
computes a reproduced monthly %-return grid, parses a named section of the
golden ``monthly_pnl_targets.md`` into the same grid shape, and produces the
comparison metrics + a side-by-side printer the per-leg validation reports use.

W1-W4 workers reuse this by passing a different leg entity + target-section
name.  It is TEST-LAYER code, so it may import ``tcg`` freely.

Design notes / conventions (each load-bearing)
----------------------------------------------
* **Monthly bucket = calendar month, % of capital.**  A month's return is
  ``equity[last_bar_of_month] / equity[last_bar_of_prior_month] - 1`` (the very
  first month is anchored at ``equity[0]``).  This is BYTE-IDENTICAL to the
  engine's ``tcg.engine.metrics.aggregate_returns(..., "monthly")`` compound
  convention — cross-checked in ``test_val_harness.py`` — so the reproduced grid
  and the API's own ``monthly_returns`` block agree.  Values are stored in
  PERCENT (target-table units); ``1.28`` == +1.28 %/month.

* **Year column = compounding checksum**, NOT a sum: ``∏(1+m/100) - 1 ≈
  year/100``.  ``parse_target_section`` recomputes it per row and returns the
  mismatches (transcription-error tripwire), per the target-file instruction.

* **Blank target cell → None** ("month not traded / outside data range").
  Skipped in compounding (factor 1) and excluded from every correlation/diff.
  A literal ``0`` is a TRADED month with 0 % return and IS included.

* **Sharpe is intentionally absent.**  The engine's Sharpe is known-wrong
  (Gael); this harness never computes or gates on it.

* **maxDD is compared as a magnitude RATIO** ``|repro|/|given|`` (not pp) — the
  honest metric across a -13 % and a -35 % leg (see the Wave-0 band).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np
import numpy.typing as npt

from tcg.engine.statistics import compute_statistics

# --------------------------------------------------------------------------- #
# Locating the golden target file (repo-root anchored, no CWD assumption)
# --------------------------------------------------------------------------- #

# tests/integration/strategy_repro/harness.py -> parents[3] == TCG-software/
_TCG_SOFTWARE = Path(__file__).resolve().parents[3]
# The workspace target tables live OUTSIDE TCG-software (one level up, in the
# repo-parent ``workspace/`` tree).  Anchor on the parent of TCG-software.
DEFAULT_TARGETS_MD = (
    _TCG_SOFTWARE.parent
    / "workspace"
    / "tasks"
    / "strategy-repro-spec"
    / "output"
    / "validation_targets"
    / "monthly_pnl_targets.md"
)

_MONTHS = list(range(1, 13))


# --------------------------------------------------------------------------- #
# MonthlyGrid — a year x month %-return grid
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MonthlyGrid:
    """A year x month grid of %-returns (PERCENT units).

    ``cells[(year, month)]`` is the month's return in percent, or the key is
    ABSENT when there is no value (a blank target cell / a month the reproduced
    curve does not cover).  ``year_totals[year]`` is the row's annual figure in
    percent — the STATED value for a parsed target, or the compounded value for
    a reproduced grid.
    """

    cells: dict[tuple[int, int], float]
    year_totals: dict[int, float] = field(default_factory=dict)

    def value(self, year: int, month: int) -> float | None:
        return self.cells.get((year, month))

    def months(self) -> list[tuple[int, int]]:
        """All present (year, month) keys, chronologically sorted."""
        return sorted(self.cells.keys())

    def years(self) -> list[int]:
        return sorted({y for (y, _m) in self.cells})

    def compounded_year_total(self, year: int) -> float | None:
        """∏(1+m/100)-1 over the present months of ``year``, in percent."""
        ms = [self.cells[(year, m)] for m in _MONTHS if (year, m) in self.cells]
        if not ms:
            return None
        prod = 1.0
        for m in ms:
            prod *= 1.0 + m / 100.0
        return (prod - 1.0) * 100.0


# --------------------------------------------------------------------------- #
# Reproduced grid from a daily equity curve
# --------------------------------------------------------------------------- #


def rebase_to_100(equity: Sequence[float]) -> npt.NDArray[np.float64]:
    """Rescale an equity curve so its first finite value is 100.0.

    ``/api/portfolio/compute`` returns ``leg_equities[lbl]`` seeded at
    ``|normalized_weight|·100`` (e.g. a cash leg paired with one equal-weight
    companion starts at 50, not 100 — the F4 cash-only guard forces a companion
    to supply the date axis).  That per-leg curve carries the leg's OWN returns
    (the companion's vol is NOT blended in), so rebasing to 100 recovers the
    standalone base-100 leg curve — neutral to every ratio-based metric
    (monthly grid, corr, ann_ret) and correct for the absolute ruin tripwire.
    ``portfolio_equity`` for a single standalone leg is already base-100, so it
    needs no rebasing.
    """
    e = np.asarray(equity, dtype=np.float64)
    finite = e[np.isfinite(e)]
    if finite.shape[0] == 0 or finite[0] == 0.0:
        return e
    return e / finite[0] * 100.0


def _to_yyyymmdd(dates: Sequence) -> npt.NDArray[np.int64]:
    """Coerce ISO strings ('2006-01-03') OR YYYYMMDD ints to an int64 array."""
    out: list[int] = []
    for d in dates:
        if isinstance(d, str):
            out.append(int(d.replace("-", "")))
        else:
            out.append(int(d))
    return np.asarray(out, dtype=np.int64)


def equity_to_monthly_grid(
    dates: Sequence,
    equity: Sequence[float],
) -> MonthlyGrid:
    """Bucket a daily equity curve into a calendar-month %-return grid.

    ``dates`` are ISO strings or YYYYMMDD ints (ascending); ``equity`` is the
    aligned equity level (any positive base).  A month's return anchors on the
    equity just before the month begins (``equity[0]`` for the first month) —
    identical to the engine's monthly aggregation.  Returned values are PERCENT.
    """
    d = _to_yyyymmdd(dates)
    e = np.asarray(equity, dtype=np.float64)
    if d.shape[0] != e.shape[0]:
        raise ValueError(f"dates ({d.shape[0]}) and equity ({e.shape[0]}) length mismatch")
    if e.shape[0] < 2:
        raise ValueError("need >= 2 equity observations")

    ym = (d // 100).astype(np.int64)  # YYYYMM per bar
    n = e.shape[0]

    # Boundaries where the YYYYMM key changes.
    boundaries = [0]
    for i in range(1, n):
        if ym[i] != ym[i - 1]:
            boundaries.append(i)
    boundaries.append(n)  # sentinel

    cells: dict[tuple[int, int], float] = {}
    for k in range(len(boundaries) - 1):
        start = boundaries[k]
        end = boundaries[k + 1] - 1  # inclusive last bar of the bucket
        anchor_idx = start - 1 if start > 0 else 0
        anchor = e[anchor_idx]
        if anchor <= 0 or not math.isfinite(anchor):
            continue
        end_val = e[end]
        if not math.isfinite(end_val):
            continue
        ret_pct = (end_val / anchor - 1.0) * 100.0
        key_ym = int(ym[start])
        year, month = divmod(key_ym, 100)
        cells[(year, month)] = ret_pct

    grid = MonthlyGrid(cells=cells)
    year_totals = {y: grid.compounded_year_total(y) for y in grid.years()}
    return MonthlyGrid(
        cells=cells,
        year_totals={y: v for y, v in year_totals.items() if v is not None},
    )


# --------------------------------------------------------------------------- #
# Target-table parser
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChecksumResult:
    """Per-row year-checksum outcome (∏(1+m/100)-1 vs the stated year)."""

    year: int
    stated: float | None
    computed: float | None
    abs_diff_pp: float | None

    @property
    def ok(self) -> bool:
        if self.stated is None or self.computed is None:
            return True  # nothing to check (e.g. an in-progress final year)
        return self.abs_diff_pp is not None and self.abs_diff_pp <= _CHECKSUM_TOL_PP


# Compounding-checksum tolerance (pp).  Transcribed screenshots round each cell
# to 2 dp; 12 such roundings + the stated year's own rounding accumulate to a
# few tenths of a pp, so 0.6 pp flags a genuine slip without false alarms.
_CHECKSUM_TOL_PP = 0.6


def _parse_cell(tok: str) -> float | None:
    tok = tok.strip()
    if tok == "":
        return None
    # Normalise a unicode minus if one slipped in.
    tok = tok.replace("−", "-")
    return float(tok)


def parse_target_section(
    section_name: str,
    md_path: Path | str = DEFAULT_TARGETS_MD,
) -> tuple[MonthlyGrid, list[ChecksumResult]]:
    """Parse a named ``## <section_name>`` table into a MonthlyGrid + checksums.

    Matches the section whose ``##`` header STARTS WITH ``section_name`` (the
    headers carry a trailing descriptive parenthetical, e.g.
    ``## Short_SPX_50d_Put_2M  (always-on 50Δ put; ...)``).  Each data row is
    ``|year|Jan..Dec|year|``; blank cells → absent (None); the trailing ``year``
    column is stored as ``year_totals``.  The compounding checksum
    ``∏(1+m/100)-1 ≈ year/100`` is recomputed per row and returned so a caller
    can assert data fidelity BEFORE trusting the grid.
    """
    path = Path(md_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Find the header line.
    hdr_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            title = ln[3:].strip()
            if title.startswith(section_name):
                hdr_idx = i
                break
    if hdr_idx is None:
        raise KeyError(f"section {section_name!r} not found in {path}")

    cells: dict[tuple[int, int], float] = {}
    year_totals: dict[int, float] = {}
    checksums: list[ChecksumResult] = []

    # Walk data rows until the next ``##`` header or EOF.  A data row starts
    # with ``|<4-digit-year>|``.
    row_re = re.compile(r"^\|\s*(\d{4})\s*\|")
    for ln in lines[hdr_idx + 1 :]:
        if ln.startswith("## "):
            break
        m = row_re.match(ln)
        if not m:
            continue
        # Split the markdown row into cells (drop the leading/trailing empties).
        parts = [c for c in ln.split("|")]
        # parts[0] == '' (before first pipe); parts[1] == year; parts[2..13]
        # == Jan..Dec; parts[14] == year total; parts[15] == '' (after last).
        # Be tolerant of ragged trailing cells.
        toks = parts[1:]
        if toks and toks[-1].strip() == "":
            toks = toks[:-1]
        if len(toks) < 13:
            # Not a full month row — skip defensively.
            continue
        year = int(toks[0].strip())
        month_toks = toks[1:13]
        for mi, tok in zip(_MONTHS, month_toks):
            v = _parse_cell(tok)
            if v is not None:
                cells[(year, mi)] = v
        stated = _parse_cell(toks[13]) if len(toks) >= 14 else None
        if stated is not None:
            year_totals[year] = stated
        checksums.append(_row_checksum(year, month_toks, stated))

    return MonthlyGrid(cells=cells, year_totals=year_totals), checksums


def _row_checksum(
    year: int, month_toks: Iterable[str], stated: float | None
) -> ChecksumResult:
    prod = 1.0
    any_val = False
    for tok in month_toks:
        v = _parse_cell(tok)
        if v is None:
            continue
        any_val = True
        prod *= 1.0 + v / 100.0
    computed = (prod - 1.0) * 100.0 if any_val else None
    if stated is None or computed is None:
        diff = None
    else:
        diff = abs(computed - stated)
    return ChecksumResult(year=year, stated=stated, computed=computed, abs_diff_pp=diff)


# --------------------------------------------------------------------------- #
# Comparison metrics
# --------------------------------------------------------------------------- #


def _pearson(a: npt.NDArray[np.float64], b: npt.NDArray[np.float64]) -> float:
    """Pearson correlation; NaN when either series is (near-)constant."""
    if a.shape[0] < 3 or b.shape[0] < 3:
        return float("nan")
    sa = float(np.std(a))
    sb = float(np.std(b))
    if sa <= 1e-12 or sb <= 1e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


@dataclass(frozen=True)
class Comparison:
    """The full reproduced-vs-target comparison for one leg."""

    section: str
    n_overlap_months: int
    monthly_corr: float
    equity_log_corr: float
    repro_ann_ret_pct: float
    given_ann_ret_pct: float | None
    ann_ret_abs_diff_pp: float | None
    repro_maxdd_pct: float
    given_maxdd_pct: float | None
    maxdd_ratio: float | None  # |repro| / |given|
    repro_min_equity: float
    ruin_ok: bool
    diff_grid: MonthlyGrid  # per-month (repro - target), only overlapping cells
    repro_grid: MonthlyGrid
    target_grid: MonthlyGrid
    checksum_failures: list[ChecksumResult]
    # MONTHLY-vs-MONTHLY maxDD (both curves resampled to MONTH-END over the
    # OVERLAP months, then peak-to-trough).  This is the SOUND drawdown basis for
    # a leg whose target is a MONTHLY grid only: the default ``maxdd_ratio`` above
    # divides a *daily* reproduction maxDD by a *monthly-granularity* target maxDD
    # (apples-to-oranges — it over-reports drawdown and prints misleading FAILs,
    # review-d1-signal-lag.md nit 1).  ``check_band(..., maxdd_basis="monthly")``
    # gates on ``maxdd_ratio_monthly`` instead.  Defaulted so pre-existing
    # constructions stay valid; ``compare()`` always populates them.
    repro_maxdd_monthly_pct: float = 0.0
    target_maxdd_monthly_pct: float = 0.0
    maxdd_ratio_monthly: float | None = None


def _series_maxdd_pct(eq: npt.NDArray[np.float64]) -> float:
    """Worst peak-to-trough drawdown of an equity curve, in PERCENT (<= 0)."""
    if eq.shape[0] == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    return float(dd.min()) * 100.0


def _overlap_months(repro: MonthlyGrid, target: MonthlyGrid) -> list[tuple[int, int]]:
    return sorted(set(repro.cells) & set(target.cells))


def compare(
    dates: Sequence,
    equity: Sequence[float],
    target_grid: MonthlyGrid,
    *,
    section: str = "",
    given_ann_ret_pct: float | None = None,
    given_maxdd_pct: float | None = None,
    checksums: Sequence[ChecksumResult] | None = None,
    ruin_floor: float = 50.0,
    base_equity: float = 100.0,
) -> Comparison:
    """Compare a reproduced daily equity curve to a target monthly grid.

    ``given_ann_ret_pct`` / ``given_maxdd_pct`` are the leg's GIVEN headline
    stats (percent; maxDD negative).  ``ruin_floor`` is the min-equity tripwire
    on a ``base_equity``-based curve.  Correlations use only months present in
    BOTH grids; the equity-level corr is Pearson of the LOG cumulative monthly
    equity reconstructed from each grid's monthly returns over those months
    (the target has no daily curve, so monthly cumulative is the honest basis).
    """
    d = _to_yyyymmdd(dates)
    e = np.asarray(equity, dtype=np.float64)
    repro_grid = equity_to_monthly_grid(d, e)

    overlap = _overlap_months(repro_grid, target_grid)
    repro_r = np.array([repro_grid.cells[k] / 100.0 for k in overlap], dtype=np.float64)
    targ_r = np.array([target_grid.cells[k] / 100.0 for k in overlap], dtype=np.float64)

    monthly_corr = _pearson(repro_r, targ_r)

    if len(overlap) >= 2:
        repro_eq = np.cumprod(1.0 + repro_r)
        targ_eq = np.cumprod(1.0 + targ_r)
        with np.errstate(invalid="ignore", divide="ignore"):
            equity_log_corr = _pearson(np.log(repro_eq), np.log(targ_eq))
        # Monthly-vs-monthly maxDD: both month-end curves over the SAME overlap
        # months → apples-to-apples drawdown (the sound basis for monthly-only
        # targets, review nit 1).
        repro_maxdd_monthly = _series_maxdd_pct(repro_eq)
        target_maxdd_monthly = _series_maxdd_pct(targ_eq)
    else:
        equity_log_corr = float("nan")
        repro_maxdd_monthly = 0.0
        target_maxdd_monthly = 0.0
    if abs(target_maxdd_monthly) > 1e-9:
        maxdd_ratio_monthly: float | None = abs(repro_maxdd_monthly) / abs(
            target_maxdd_monthly
        )
    else:
        maxdd_ratio_monthly = None

    # Reproduced headline stats from the daily curve (Sharpe intentionally
    # ignored).  compute_statistics needs YYYYMMDD ints + positive finite equity.
    stats = compute_statistics(d, e)
    repro_ann = stats.return_.cagr * 100.0
    repro_maxdd = stats.drawdown.max_drawdown * 100.0  # <= 0
    repro_min_eq = float(np.min(e))

    ann_diff = (
        abs(repro_ann - given_ann_ret_pct) if given_ann_ret_pct is not None else None
    )
    if given_maxdd_pct is not None and abs(given_maxdd_pct) > 1e-9:
        maxdd_ratio = abs(repro_maxdd) / abs(given_maxdd_pct)
    else:
        maxdd_ratio = None

    diff_cells = {k: repro_grid.cells[k] - target_grid.cells[k] for k in overlap}
    diff_grid = MonthlyGrid(cells=diff_cells)

    checksum_failures = [c for c in (checksums or []) if not c.ok]

    return Comparison(
        section=section,
        n_overlap_months=len(overlap),
        monthly_corr=monthly_corr,
        equity_log_corr=equity_log_corr,
        repro_ann_ret_pct=repro_ann,
        given_ann_ret_pct=given_ann_ret_pct,
        ann_ret_abs_diff_pp=ann_diff,
        repro_maxdd_pct=repro_maxdd,
        given_maxdd_pct=given_maxdd_pct,
        maxdd_ratio=maxdd_ratio,
        repro_min_equity=repro_min_eq,
        ruin_ok=repro_min_eq > ruin_floor,
        diff_grid=diff_grid,
        repro_grid=repro_grid,
        target_grid=target_grid,
        checksum_failures=checksum_failures,
        repro_maxdd_monthly_pct=repro_maxdd_monthly,
        target_maxdd_monthly_pct=target_maxdd_monthly,
        maxdd_ratio_monthly=maxdd_ratio_monthly,
    )


# --------------------------------------------------------------------------- #
# Tolerance band (Wave-0, finalized vs the 50Δ actuals)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToleranceBand:
    """The per-leg golden tolerance band (Wave-0 FINALIZED, Sharpe excluded).

    **CONFIRMED at the proposed numbers vs the real 50Δ + 10Δ full-window runs
    using the LEGACY STRICT-EOM ROLL** (``maturity=end_of_month(offset=2)``,
    matching ``simpleMonthlyPutRollEom``):

    | leg | roll | monthly_corr | equity_log_corr | ann_ret |Δ| | maxDD ratio | min |
    |-----|------|-------------:|----------------:|----------:|-----:|----:|
    | 50Δ | EOM             | **0.897** | 0.987 | 1.13 pp | 0.78 | 97.4 |
    | 10Δ | EOM             | **0.873** | 0.997 | 0.31 pp | 1.02 | 92.1 |
    | 50Δ | NearestToTgt(60)| 0.780     | 0.993 | 0.58 pp | 0.81 | 90.6 |
    | 10Δ | NearestToTgt(60)| 0.764     | 0.978 | 1.30 pp | 1.06 | 89.6 |

    KEY FINDING: the earlier monthly_corr shortfall (0.76-0.78) was a ROLL-TIMING
    artifact, NOT a band problem.  The §5.1 legs originally used
    ``NearestToTarget(60)+hold`` (rolls when the nearest-60d listed expiry
    advances — days off the calendar month-end), which perturbs within-month
    returns while leaving the cumulative curve at 0.99.  Matching the legacy
    strict end-of-calendar-month roll lifts monthly_corr by +0.11-0.12 → BOTH
    legs clear the proposed 0.80 with margin.  So the band stays at the proposed
    numbers; NO relaxation needed.

    * **monthly_corr ≥ 0.80** — both EOM legs clear it (0.873 / 0.897).
    * **equity_corr ≥ 0.90** (PRIMARY shape gate; both 0.987 / 0.997).
    * ann_ret ≤ 2.0 pp (worst 1.13); maxDD RATIO [0.70, 1.40] (0.78 / 1.02 — the
      ratio swap CONFIRMED); ruin min > 50 (92 / 97).

    ⚑ NOTE for W2/W3: the intermittently-traded gated/hedged legs (HVOL-gated
    10Δ, sparse VIX-call legs) may score lower monthly_corr even with correct
    rolls (many 0-months + a single mistimed trade dominates); revisit the floor
    for THOSE legs with their own data if they miss 0.80 while equity_corr holds.
    """

    monthly_corr_min: float = 0.80   # CONFIRMED — both EOM legs clear it (0.873/0.897)
    equity_corr_min: float = 0.90    # PRIMARY shape gate (both 0.987/0.997)
    ann_ret_abs_pp_max: float = 2.0  # observed worst 1.13 pp (EOM)
    maxdd_ratio_lo: float = 0.70     # observed 0.78 / 1.02 (EOM)
    maxdd_ratio_hi: float = 1.40
    ruin_floor: float = 50.0         # observed min 92 / 97 (EOM)


DEFAULT_BAND = ToleranceBand()


@dataclass(frozen=True)
class BandVerdict:
    passed: bool
    reasons: list[str]  # one line per checked criterion, PASS/FAIL prefixed


def check_band(
    cmp: Comparison,
    band: ToleranceBand = DEFAULT_BAND,
    *,
    maxdd_basis: Literal["daily", "monthly"] = "daily",
) -> BandVerdict:
    """Evaluate a Comparison against a ToleranceBand (shape-first, Sharpe out).

    ``maxdd_basis`` selects the drawdown ratio that is GATED:
      * ``"daily"`` (default) — ``maxdd_ratio`` = |daily-repro-maxDD| /
        |given-maxDD|.  Correct for §5.1-style legs whose target carries a
        published DAILY headline maxDD (apples-to-apples).
      * ``"monthly"`` — ``maxdd_ratio_monthly`` = |month-end-repro-maxDD| /
        |month-end-target-maxDD| over the overlap months.  The sound basis for a
        leg whose target is a MONTHLY grid only (§5.2/§5.5-style); avoids the
        apples-to-oranges daily-vs-monthly FAIL (review nit 1).
    """
    reasons: list[str] = []
    ok = True

    def _chk(cond: bool, label: str) -> None:
        nonlocal ok
        reasons.append(f"{'PASS' if cond else 'FAIL'} {label}")
        ok = ok and cond

    _chk(
        not math.isnan(cmp.monthly_corr) and cmp.monthly_corr >= band.monthly_corr_min,
        f"monthly_corr {cmp.monthly_corr:.3f} >= {band.monthly_corr_min}",
    )
    _chk(
        not math.isnan(cmp.equity_log_corr)
        and cmp.equity_log_corr >= band.equity_corr_min,
        f"equity_log_corr {cmp.equity_log_corr:.3f} >= {band.equity_corr_min}",
    )
    if cmp.ann_ret_abs_diff_pp is not None:
        _chk(
            cmp.ann_ret_abs_diff_pp <= band.ann_ret_abs_pp_max,
            f"ann_ret |Δ| {cmp.ann_ret_abs_diff_pp:.2f}pp <= {band.ann_ret_abs_pp_max}pp",
        )
    gated_ratio = (
        cmp.maxdd_ratio_monthly if maxdd_basis == "monthly" else cmp.maxdd_ratio
    )
    if gated_ratio is not None:
        _chk(
            band.maxdd_ratio_lo <= gated_ratio <= band.maxdd_ratio_hi,
            f"maxDD ratio ({maxdd_basis}) {gated_ratio:.2f} in "
            f"[{band.maxdd_ratio_lo}, {band.maxdd_ratio_hi}]",
        )
    _chk(
        cmp.repro_min_equity > band.ruin_floor,
        f"min_equity {cmp.repro_min_equity:.2f} > {band.ruin_floor}",
    )
    return BandVerdict(passed=ok, reasons=reasons)


# --------------------------------------------------------------------------- #
# Side-by-side monthly grid printer
# --------------------------------------------------------------------------- #

_MONTH_LABELS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _fmt(v: float | None) -> str:
    return "   . " if v is None else f"{v:6.2f}"


def format_side_by_side(
    repro: MonthlyGrid,
    target: MonthlyGrid,
    *,
    title: str = "",
) -> str:
    """Three stacked year x month tables: reproduced / target / diff.

    One block per section: the reproduced grid, the target grid, and the
    (repro - target) diff over overlapping cells.  Percent units.
    """
    years = sorted(set(repro.years()) | set(target.years()))
    out: list[str] = []
    if title:
        out.append(f"### {title}")

    def _block(name: str, grid: MonthlyGrid, diff: bool = False) -> None:
        out.append(f"\n[{name}]  (% per month)")
        out.append("year  | " + " ".join(f"{lbl:>6}" for lbl in _MONTH_LABELS) + " |    yr")
        for y in years:
            row_vals = []
            for mo in _MONTHS:
                v = grid.value(y, mo)
                row_vals.append(_fmt(v))
            yr = grid.year_totals.get(y)
            if yr is None:
                yr = grid.compounded_year_total(y)
            yr_s = "  . " if yr is None else f"{yr:6.2f}"
            out.append(f"{y}  | " + " ".join(row_vals) + f" | {yr_s}")

    _block("REPRODUCED", repro)
    _block("TARGET", target)
    # Diff grid over overlapping cells.
    diff_cells = {
        k: repro.cells[k] - target.cells[k]
        for k in (set(repro.cells) & set(target.cells))
    }
    _block("DIFF (repro - target)", MonthlyGrid(cells=diff_cells))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Persisted-entity run (the "UI-buildable + persisted" path)
# --------------------------------------------------------------------------- #


def saved_legs_to_compute_body(
    saved_legs: Sequence[dict],
    *,
    rebalance: str = "none",
    start: str | None = None,
    end: str | None = None,
    normalize_weights: bool = True,
    use_cache: bool = False,
) -> dict:
    """Translate a persisted portfolio's ``legs`` array into a
    ``/api/portfolio/compute`` request body.

    Faithful reimplementation of the frontend ``buildPortfolioComputeBody``
    transform (frontend/src/pages/Portfolio/computeBodyBuilder.js): the stored
    flat ``legs`` array (weight inline, ``label`` per leg) becomes two dicts
    keyed by ``label`` — ``legs`` (per-type spec) and ``weights``.  This is the
    same path a non-dev's saved portfolio takes on "Compute", so running through
    it proves the leg is UI-buildable + persisted (not an inline JSON).

    Covers the leg types Wave-0..4 use (option_stream / cash_rate / continuous /
    instrument).  ``normalize_weights`` defaults to **True** — the UI-faithful
    default (the frontend never sends the flag; the backend defaults it True).
    For a SINGLE standalone leg this normalizes Σ|w| to 1.0 (a short weight -100
    becomes exactly -1.0×, applying the direction with NO rescale — the val_5_1
    recipe).  The LEVERAGED multi-leg combine (F1, V1/V2 assembly) is a later
    wave that passes ``normalize_weights=False`` explicitly.
    """
    legs: dict[str, dict] = {}
    weights: dict[str, float] = {}
    for leg in saved_legs:
        label = leg["label"]
        typ = leg.get("type", "instrument")
        spec: dict
        if typ == "option_stream":
            spec = {
                "type": "option_stream",
                "collection": leg["collection"],
                "option_type": leg["option_type"],
                "cycle": leg.get("cycle"),
                "maturity": leg["maturity"],
                "selection": leg["selection"],
                "stream": leg["stream"],
                "hold_between_rolls": bool(leg.get("hold_between_rolls", False)),
                "nav_times": float(leg.get("nav_times", 1.0)),
            }
            if leg.get("sizing_mode"):
                spec["sizing_mode"] = leg["sizing_mode"]
            if leg.get("futures_reference"):
                spec["futures_reference"] = leg["futures_reference"]
            if leg.get("roll_offset"):
                spec["roll_offset"] = leg["roll_offset"]
        elif typ == "cash_rate":
            spec = {"type": "cash_rate", "cash_rate": leg.get("cash_rate")}
        elif typ == "continuous":
            spec = {
                "type": "continuous",
                "collection": leg["collection"],
                "strategy": leg.get("strategy") or "front_month",
                "adjustment": leg.get("adjustment") or "none",
            }
            if leg.get("cycle"):
                spec["cycle"] = leg["cycle"]
            roll = leg.get("rollOffset", 0)
            if roll:
                spec["roll_offset"] = roll
            rank = leg.get("rank", 1)
            if rank and rank > 1:
                spec["rank"] = rank
        elif typ == "instrument":
            spec = {
                "type": "instrument",
                "collection": leg["collection"],
                "symbol": leg["symbol"],
            }
        else:
            raise ValueError(f"unsupported persisted leg type {typ!r} for label {label!r}")
        # Per-leaf data_source (v1/v2) must survive into the compute body for ANY
        # leg type — the fetch layer routes each leaf to its warehouse via
        # ``svc_for(data_source)``.  Previously only cash_rate propagated it, so a
        # v2 option_stream leg silently ran on v1 (the principal v1->v2 rebase gap).
        if leg.get("data_source"):
            spec["data_source"] = leg["data_source"]
        legs[label] = spec
        weights[label] = float(leg.get("weight", 0.0))

    body: dict = {
        "legs": legs,
        "weights": weights,
        "rebalance": rebalance,
        "return_type": "normal",
        "normalize_weights": normalize_weights,
        "use_cache": use_cache,
    }
    if start:
        body["start"] = start
    if end:
        body["end"] = end
    return body


async def persist_and_run_portfolio(
    client,
    saved_legs: Sequence[dict],
    *,
    portfolio_id: str,
    name: str,
    category: str = "DEV",
    rebalance: str = "none",
    kind: str = "pure",
    start: str | None = None,
    end: str | None = None,
    normalize_weights: bool = True,
    timeout: float | None = None,
) -> dict:
    """CREATE a portfolio via the real persistence API, READ it back, translate
    the stored doc to a compute body, RUN it, then ARCHIVE it (cleanup).

    Returns the ``/api/portfolio/compute`` JSON.  Raises AssertionError with the
    response text if any step returns a non-2xx status.  This is the reusable
    "the way a non-dev would from the React UI" path — it never inlines a
    compute JSON; the legs round-trip through ``tcg_app_data`` first.
    """
    # 1. CREATE (POST /api/persistence/portfolios) — write through tcg.persistence.
    create_body = {
        "id": portfolio_id,
        "name": name,
        "category": category,
        "legs": list(saved_legs),
        "rebalance": rebalance,
        "kind": kind,
    }
    r = await client.post("/api/persistence/portfolios", json=create_body)
    assert r.status_code == 201, f"persist create failed: {r.status_code} {r.text}"

    try:
        # 2. READ back (GET /api/persistence/portfolios/{id}) — prove round-trip.
        r = await client.get(f"/api/persistence/portfolios/{portfolio_id}")
        assert r.status_code == 200, f"persist get failed: {r.status_code} {r.text}"
        doc = r.json()
        stored_legs = doc["legs"]

        # 3. Translate stored doc -> compute body (frontend recipe).
        body = saved_legs_to_compute_body(
            stored_legs,
            rebalance=doc.get("rebalance", "none"),
            start=start,
            end=end,
            normalize_weights=normalize_weights,
        )

        # 4. RUN (POST /api/portfolio/compute).
        kwargs = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        r = await client.post("/api/portfolio/compute", **kwargs)
        assert r.status_code == 200, f"compute failed: {r.status_code} {r.text}"
        result = r.json()
        assert "error_type" not in result, f"compute errored: {result}"
        return result
    finally:
        # 5. CLEANUP — soft-delete the calibration portfolio.
        await client.delete(f"/api/persistence/portfolios/{portfolio_id}")


# --------------------------------------------------------------------------- #
# DURABLE persisted-entity upsert (the UI-VISIBLE reproduction entity path)
# --------------------------------------------------------------------------- #
#
# Gael's instruction: the §5.x reproduction legs must persist as DURABLE,
# UI-visible entities named ``Reproduction_<leg>`` — a non-dev opens
# ``Reproduction_Short_SPX_50d_Put_2M`` on the Portfolio page and clicks Compute.
# Unlike ``persist_and_run_portfolio`` (which soft-deletes at the end — an
# EPHEMERAL calibration run), these leave the entity in place under a VISIBLE
# category so it stays discoverable + runnable.
#
# Idempotency (re-runnable without a duplicate row or a 409 collision) rides
# ENTIRELY on the EXISTING persistence API — no new production endpoint:
#   * POST /api/persistence/portfolios       -> 201 create               (first run)
#   * on 409 (id already exists from a prior run):
#     PUT /api/persistence/portfolios/{id}   -> 200 full-replace          (re-run)
# The PUT WHERE clause is ``(id, type)`` only — it does NOT filter on category
# (tcg/persistence/repository.py::update) — so a previously soft-deleted
# (category='DELETED') entity is RESURRECTED to the visible category by the same
# PUT.  POST+PUT thus give a clean idempotent upsert that honours the
# tcg_app_data schema (``tcg.persistence`` stays the sole writer) and the
# soft-delete convention (DELETE→category='DELETED').

# Stable ids == names for the §5.x DURABLE reproduction entities (``Reproduction_
# <leg>``).  The id doubles as the name — both match the persistence id pattern
# ``^[A-Za-z0-9_\-:.]+$``.
REPRO_ENTITY_50D_PUT = "Reproduction_Short_SPX_50d_Put_2M"
REPRO_ENTITY_10D_PUT = "Reproduction_Short_SPX_10d_Put_2M"
REPRO_ENTITY_USD_1M_RATE = "Reproduction_USD_1M_rate"

# The single VISIBLE category the reproduction entities live under.  Explicitly
# NOT 'DELETED' (the hidden soft-delete sentinel) and NOT 'ARCHIVE'.  'DEV' is a
# first-class visible ``Category`` (the app's dev section) so the entity shows on
# the Portfolio/Signals DEV tab.  The v1->v2 rebase consolidates ALL reproduction
# entities here (was 'RESEARCH' in the prior wave — Gael 2026-08-07: use DEV).
REPRO_VISIBLE_CATEGORY = "DEV"


async def upsert_durable_portfolio(
    client,
    saved_legs: Sequence[dict],
    *,
    portfolio_id: str,
    name: str,
    category: str = REPRO_VISIBLE_CATEGORY,
    rebalance: str = "none",
    kind: str = "pure",
) -> dict:
    """Idempotently CREATE-or-REPLACE a DURABLE, UI-visible portfolio entity and
    return the GET-round-tripped stored doc.

    POST-create; on 409 (already exists from a prior run) PUT full-replace.  The
    entity is NOT soft-deleted afterwards — it stays in ``tcg_app_data`` under a
    visible ``category`` so a non-dev can open + run it from the Portfolio page.
    Idempotency uses ONLY the existing persistence API (POST + PUT); PUT's
    ``(id, type)`` WHERE ignores category, so a soft-deleted prior entity is
    resurrected to the visible category.  Raises AssertionError on any non-2xx.
    """
    assert category != "DELETED", (
        "durable reproduction entity must use a VISIBLE category, not the "
        "soft-delete sentinel 'DELETED'"
    )
    create_body = {
        "id": portfolio_id,
        "name": name,
        "category": category,
        "legs": list(saved_legs),
        "rebalance": rebalance,
        "kind": kind,
    }
    r = await client.post("/api/persistence/portfolios", json=create_body)
    if r.status_code == 201:
        pass  # fresh create
    elif r.status_code == 409:
        # A prior run already persisted this id — full-replace to refresh the
        # content AND (if it had been soft-deleted) resurrect it to the visible
        # category.  This is what makes the path idempotent.
        update_body = {
            "name": name,
            "category": category,
            "legs": list(saved_legs),
            "rebalance": rebalance,
            "kind": kind,
        }
        r = await client.put(
            f"/api/persistence/portfolios/{portfolio_id}", json=update_body
        )
        assert (
            r.status_code == 200
        ), f"durable upsert PUT failed: {r.status_code} {r.text}"
    else:
        raise AssertionError(
            f"durable upsert POST failed: {r.status_code} {r.text}"
        )

    # GET back — prove the round-trip + confirm the entity is VISIBLE (not the
    # hidden 'DELETED' sentinel) so the UI can list + open it.
    r = await client.get(f"/api/persistence/portfolios/{portfolio_id}")
    assert r.status_code == 200, f"durable get failed: {r.status_code} {r.text}"
    doc = r.json()
    assert doc["id"] == portfolio_id, doc
    assert doc["name"] == name, doc
    assert doc["category"] == category, doc
    assert doc["category"] != "DELETED", doc
    return doc


async def durable_persist_and_run_portfolio(
    client,
    saved_legs: Sequence[dict],
    *,
    portfolio_id: str,
    name: str,
    category: str = REPRO_VISIBLE_CATEGORY,
    rebalance: str = "none",
    kind: str = "pure",
    start: str | None = None,
    end: str | None = None,
    normalize_weights: bool = True,
    timeout: float | None = None,
) -> tuple[dict, dict]:
    """Idempotently upsert a DURABLE portfolio entity, GET it back, translate the
    stored doc to a compute body, RUN it, and return ``(stored_doc, compute_json)``.

    Identical run path to ``persist_and_run_portfolio`` (the non-dev "Compute"
    route: stored legs -> frontend recipe -> ``/api/portfolio/compute``) EXCEPT
    the entity is durable — an idempotent upsert, a VISIBLE category, and NO
    soft-delete at the end.  Returns the stored doc too so a caller can assert
    visibility.
    """
    doc = await upsert_durable_portfolio(
        client,
        saved_legs,
        portfolio_id=portfolio_id,
        name=name,
        category=category,
        rebalance=rebalance,
        kind=kind,
    )
    stored_legs = doc["legs"]
    body = saved_legs_to_compute_body(
        stored_legs,
        rebalance=doc.get("rebalance", "none"),
        start=start,
        end=end,
        normalize_weights=normalize_weights,
    )
    kwargs = {"json": body}
    if timeout is not None:
        kwargs["timeout"] = timeout
    r = await client.post("/api/portfolio/compute", **kwargs)
    assert r.status_code == 200, f"compute failed: {r.status_code} {r.text}"
    result = r.json()
    assert "error_type" not in result, f"compute errored: {result}"
    return doc, result


# --------------------------------------------------------------------------- #
# DURABLE persisted-entity SIGNAL upsert + run (the UI-VISIBLE gated-leg path)
# --------------------------------------------------------------------------- #
#
# A SIGNAL-gated leg (e.g. §5.2 ShortPutHVOLout — a short 10Δ put made flat
# during HVOL-ON episodes) is NOT a plain always-on portfolio leg: its position
# toggles, so it runs through ``/api/signals/compute`` (Signal spec + referenced
# indicators), not ``/api/portfolio/compute``.  It therefore persists as a
# DURABLE, UI-visible ``SignalDoc`` (``POST /api/persistence/signals``), keyed
# ``Reproduction_<leg>`` under a VISIBLE category — a non-dev opens it on the
# Signals page and clicks Compute.  Idempotency mirrors the portfolio helper:
# POST-create; on 409 PUT full-replace.
#
# UI-FAITHFUL RUN: the stored doc carries only ``inputs`` + ``rules`` (the
# indicator CODE is NOT stored — the frontend hydrates it from the default /
# user indicator registry at compute time, see
# frontend/src/pages/Signals/requestBuilder.js::buildComputeRequestBody).  So
# ``durable_persist_and_run_signal`` takes the stored inputs/rules back from the
# GET, pairs them with the caller-supplied ``indicators`` list (the same shape
# the UI builds: ``{id, name, code, params, seriesMap}``), and POSTs the pair to
# ``/api/signals/compute`` — exactly the non-dev "Compute" route.

REPRO_ENTITY_SHORTPUT_HVOLOUT = "Reproduction_ShortPutHVOLout"


async def upsert_durable_signal(
    client,
    *,
    signal_id: str,
    name: str,
    inputs: Sequence[dict],
    rules: dict,
    category: str = REPRO_VISIBLE_CATEGORY,
    settings: dict | None = None,
    description: str = "",
) -> dict:
    """Idempotently CREATE-or-REPLACE a DURABLE, UI-visible SIGNAL entity and
    return the GET-round-tripped stored doc.

    POST-create; on 409 (already exists from a prior run) PUT full-replace.  The
    entity is NOT soft-deleted afterwards — it stays in ``tcg_app_data`` under a
    visible ``category`` so a non-dev can open + run it from the Signals page.
    Raises AssertionError on any non-2xx.
    """
    assert category != "DELETED", (
        "durable reproduction entity must use a VISIBLE category, not the "
        "soft-delete sentinel 'DELETED'"
    )
    body = {
        "id": signal_id,
        "name": name,
        "category": category,
        "inputs": list(inputs),
        "rules": rules,
        "settings": settings or {},
        "description": description,
    }
    r = await client.post("/api/persistence/signals", json=body)
    if r.status_code == 201:
        pass
    elif r.status_code == 409:
        update_body = {k: v for k, v in body.items() if k != "id"}
        r = await client.put(
            f"/api/persistence/signals/{signal_id}", json=update_body
        )
        assert (
            r.status_code == 200
        ), f"durable signal upsert PUT failed: {r.status_code} {r.text}"
    else:
        raise AssertionError(
            f"durable signal upsert POST failed: {r.status_code} {r.text}"
        )

    r = await client.get(f"/api/persistence/signals/{signal_id}")
    assert r.status_code == 200, f"durable signal get failed: {r.status_code} {r.text}"
    doc = r.json()
    assert doc["id"] == signal_id, doc
    assert doc["name"] == name, doc
    assert doc["category"] == category, doc
    assert doc["category"] != "DELETED", doc
    return doc


async def durable_persist_and_run_signal(
    client,
    *,
    signal_id: str,
    name: str,
    inputs: Sequence[dict],
    rules: dict,
    indicators: Sequence[dict],
    category: str = REPRO_VISIBLE_CATEGORY,
    settings: dict | None = None,
    description: str = "",
    start: str | None = None,
    end: str | None = None,
    slippage_bps: float = 0.0,
    fees_bps: float = 0.0,
    timeout: float | None = None,
) -> tuple[dict, dict]:
    """Idempotently upsert a DURABLE SIGNAL entity, GET it back, build the
    ``/api/signals/compute`` body from the STORED inputs/rules + the supplied
    indicator specs (the UI-faithful recipe), RUN it, and return
    ``(stored_doc, compute_json)``.

    Mirrors ``durable_persist_and_run_portfolio`` for signal-gated legs.  The
    stored doc is durable (visible category, NO soft-delete) so it stays
    discoverable + runnable on the Signals page.
    """
    doc = await upsert_durable_signal(
        client,
        signal_id=signal_id,
        name=name,
        inputs=inputs,
        rules=rules,
        category=category,
        settings=settings,
        description=description,
    )
    # Build the compute request exactly as the frontend does: the stored
    # ``inputs``/``rules`` become ``spec.inputs``/``spec.rules`` verbatim
    # (persisted shape == wire shape — see requestBuilder.js), and the
    # referenced indicator specs ride alongside.
    body: dict = {
        "spec": {
            "id": doc["id"],
            "name": doc["name"],
            "inputs": doc["inputs"],
            "rules": doc["rules"],
        },
        "indicators": list(indicators),
    }
    if start is not None:
        body["start"] = start
    if end is not None:
        body["end"] = end
    if slippage_bps > 0.0:
        body["slippage_bps"] = slippage_bps
    if fees_bps > 0.0:
        body["fees_bps"] = fees_bps

    kwargs = {"json": body}
    if timeout is not None:
        kwargs["timeout"] = timeout
    r = await client.post("/api/signals/compute", **kwargs)
    assert r.status_code == 200, f"signals compute failed: {r.status_code} {r.text}"
    result = r.json()
    assert "error_type" not in result, f"signals compute errored: {result}"
    return doc, result


__all__ = [
    "MonthlyGrid",
    "ChecksumResult",
    "Comparison",
    "ToleranceBand",
    "BandVerdict",
    "DEFAULT_BAND",
    "DEFAULT_TARGETS_MD",
    "equity_to_monthly_grid",
    "rebase_to_100",
    "parse_target_section",
    "compare",
    "check_band",
    "format_side_by_side",
    "saved_legs_to_compute_body",
    "persist_and_run_portfolio",
    "REPRO_ENTITY_50D_PUT",
    "REPRO_ENTITY_10D_PUT",
    "REPRO_ENTITY_USD_1M_RATE",
    "REPRO_ENTITY_SHORTPUT_HVOLOUT",
    "REPRO_VISIBLE_CATEGORY",
    "upsert_durable_portfolio",
    "durable_persist_and_run_portfolio",
    "upsert_durable_signal",
    "durable_persist_and_run_signal",
]
