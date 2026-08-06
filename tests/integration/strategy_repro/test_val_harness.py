"""Unit + DB-free validation tests for the strategy-repro harness.

These are NOT integration-gated: they exercise the harness's pure math and the
target-file parser (no dwh / app-data), plus a DB-FREE reproduction of the
USD_1M_rate flat-1% cash leg via the pure ``tcg.engine.cash_rate`` accrual — a
real comparison against the real golden target that proves the harness end to
end without the warehouse.

The persisted-entity API runs live in ``test_val_wave_calibration.py`` (gated).
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine import aggregate_returns
from tcg.engine.cash_rate import accrue_cash_equity

from strategy_repro.harness import (
    DEFAULT_BAND,
    DEFAULT_TARGETS_MD,
    MonthlyGrid,
    check_band,
    compare,
    equity_to_monthly_grid,
    format_side_by_side,
    parse_target_section,
    saved_legs_to_compute_body,
)


def _maxdd_line(verdict) -> str:
    """The single ``maxDD ratio ...`` reason line from a BandVerdict."""
    return next(r for r in verdict.reasons if "maxDD ratio" in r)


# --------------------------------------------------------------------------- #
# 1. Monthly grid == engine aggregate_returns (convention lock)
# --------------------------------------------------------------------------- #


def _business_days(start: str, end: str) -> np.ndarray:
    """YYYYMMDD int array of Mon-Fri days in [start, end) (holidays ignored —
    they don't move calendar-month buckets)."""
    days = np.arange(start, end, dtype="datetime64[D]")
    wd = (days.astype("datetime64[D]").view("int64") - 4) % 7  # 0=Mon .. 6=Sun
    days = days[wd < 5]
    ymd = days.astype("datetime64[D]").astype(str)
    return np.array([int(s.replace("-", "")) for s in ymd], dtype=np.int64)


def test_monthly_grid_matches_engine_aggregate_returns():
    """The harness monthly grid is byte-identical to the engine's monthly
    aggregation — so the reproduced grid agrees with the API's own
    ``monthly_returns`` block."""
    rng = np.random.default_rng(42)
    dates = _business_days("2018-01-01", "2020-07-01")
    n = dates.shape[0]
    # A positive random-walk equity curve.
    steps = 1.0 + rng.normal(0.0004, 0.01, size=n)
    steps[0] = 1.0
    equity = 100.0 * np.cumprod(steps)

    grid = equity_to_monthly_grid(dates, equity)

    # Engine reference: returns[0]=nan, returns[i]=e[i]/e[i-1]-1.
    returns = np.empty(n, dtype=np.float64)
    returns[0] = np.nan
    returns[1:] = equity[1:] / equity[:-1] - 1.0
    ref = aggregate_returns(dates, returns, {}, "normal", "monthly")

    ref_map = {row["period"]: row["portfolio"] * 100.0 for row in ref}
    grid_map = {f"{y:04d}-{m:02d}": v for (y, m), v in grid.cells.items()}

    assert set(ref_map) == set(grid_map), (set(ref_map) ^ set(grid_map))
    for k in ref_map:
        assert grid_map[k] == pytest.approx(ref_map[k], abs=1e-9), k
    print(f"\n[convention-lock] {len(grid_map)} monthly buckets match engine to 1e-9")


# --------------------------------------------------------------------------- #
# 2. Target-file parser + year checksum
# --------------------------------------------------------------------------- #


def test_parse_50d_known_cells_and_checksum():
    grid, checks = parse_target_section("Short_SPX_50d_Put_2M")
    # Known transcribed cells.
    assert grid.value(2006, 1) == pytest.approx(1.28)
    assert grid.value(2006, 5) == pytest.approx(-1.84)
    assert grid.value(2020, 3) == pytest.approx(-8.37)
    assert grid.year_totals[2006] == pytest.approx(10.08)
    # Year checksum: 2006 row compounds to ~10.08.
    row06 = next(c for c in checks if c.year == 2006)
    assert row06.ok, f"2006 checksum diff={row06.abs_diff_pp}"
    fails = [c for c in checks if not c.ok]
    print(f"\n[50d parse] years={len(grid.years())} checksum_fails={len(fails)}")
    for c in fails:
        print(f"  {c.year}: stated={c.stated} computed={c.computed:.2f} "
              f"diff={c.abs_diff_pp:.2f}pp")


def test_all_target_sections_parse_and_checksum():
    """Every named section parses and its rows pass the compounding checksum
    (a transcription-fidelity gate — the harness runs it before trusting data)."""
    sections = [
        "Short_SPX_50d_Put_2M",
        "Short_SPX_10d_Put_2M",
        "ShortPutHVOLout",
        "LongVIXaboveVVIX100_hedged",
        "VixLongHVOL_hedged",
        "USD_1M_rate(P)",
        "Ocean",
    ]
    total_rows = 0
    total_fails = 0
    print(f"\n[checksum sweep] file={DEFAULT_TARGETS_MD.name}")
    for s in sections:
        grid, checks = parse_target_section(s)
        assert grid.cells, f"section {s} parsed empty"
        fails = [c for c in checks if not c.ok]
        total_rows += len(checks)
        total_fails += len(fails)
        print(f"  {s:32s} years={len(grid.years()):2d} rows={len(checks):2d} "
              f"checksum_fails={len(fails)}")
        for c in fails:
            print(f"      {c.year}: stated={c.stated} "
                  f"computed={c.computed:.2f} diff={c.abs_diff_pp:.2f}pp")
    # The vast majority of rows must reconcile; a couple of screenshot slips are
    # tolerable (documented), but a systemic parse bug would fail many.
    assert total_fails <= 3, f"{total_fails}/{total_rows} checksum failures — parse bug?"


# --------------------------------------------------------------------------- #
# 3. compare(): a curve that reconstructs the target should correlate ~1.0
# --------------------------------------------------------------------------- #


def test_compare_perfect_reconstruction_high_corr():
    """Rebuild a synthetic DAILY curve whose monthly returns EQUAL the 50Δ
    target, then compare — monthly + equity corr must be ~1.0 and the diff grid
    ~0.  Validates compare()/corr end to end on the real target grid."""
    target, checks = parse_target_section("Short_SPX_50d_Put_2M")
    # Build one business-day curve per month applying that month's return on the
    # month's last bar (so the monthly bucket reproduces the target exactly).
    months = [k for k in target.months() if k >= (2006, 1)]  # skip 2005 stub
    dates: list[int] = []
    equity: list[float] = []
    eq = 100.0
    for (y, mo) in months:
        # ~21 business days per month; apply the whole month move on the last day.
        r = target.cells[(y, mo)] / 100.0
        for day in range(1, 21):
            dates.append(y * 10000 + mo * 100 + day)
            equity.append(eq)
        eq = eq * (1.0 + r)
        dates.append(y * 10000 + mo * 100 + 28)
        equity.append(eq)
    cmp = compare(
        np.array(dates), np.array(equity), target,
        section="50d-reconstruction",
        given_ann_ret_pct=6.78, given_maxdd_pct=-34.58,
        checksums=checks,
    )
    print(f"\n[reconstruction] months={cmp.n_overlap_months} "
          f"monthly_corr={cmp.monthly_corr:.4f} equity_corr={cmp.equity_log_corr:.4f} "
          f"ann={cmp.repro_ann_ret_pct:.2f}% maxdd={cmp.repro_maxdd_pct:.2f}%")
    assert cmp.monthly_corr > 0.999, cmp.monthly_corr
    assert cmp.equity_log_corr > 0.99, cmp.equity_log_corr
    # Diff grid must be ~0 everywhere.
    max_abs_diff = max(abs(v) for v in cmp.diff_grid.cells.values())
    assert max_abs_diff < 1e-6, max_abs_diff


def test_monthly_vs_monthly_maxdd_basis_dbfree():
    """The monthly-vs-monthly maxDD basis (review nit 1): for a MONTHLY-only
    target, gating a *daily* repro maxDD against a *monthly* target maxDD is
    apples-to-oranges and prints a false FAIL; the month-end-vs-month-end ratio
    is the sound gate.

    Construct a repro DAILY curve whose MONTH-END returns EQUAL the target
    (so the monthly-vs-monthly maxDD ratio ~ 1.0) but which has a deep
    INTRA-MONTH trough that recovers by month end (so the DAILY maxDD is far
    deeper than any month-end drawdown).  Assert the daily basis FAILs the maxDD
    gate while the monthly basis PASSes it — same Comparison, same band."""
    rets = [2.0, -10.0, 2.0, -10.0, 2.0, 5.0]  # % per month
    target = MonthlyGrid(cells={(2010, m): r for m, r in zip(range(1, 7), rets)})

    # Target's own month-end maxDD — the honest GIVEN for a monthly-only target.
    teq = np.cumprod([1.0 + r / 100.0 for r in rets])
    given_monthly_maxdd = float((teq / np.maximum.accumulate(teq) - 1.0).min()) * 100.0

    dates: list[int] = []
    equity: list[float] = []
    eq = 100.0
    for m, r in zip(range(1, 7), rets):
        rr = r / 100.0
        if m == 2:
            month_end = eq * (1.0 + rr)
            trough = eq * 0.60  # -40% INTRA-month, invisible to month-end sampling
            for day, val in [(3, eq), (10, trough), (17, trough * 1.05), (28, month_end)]:
                dates.append(2010 * 10000 + m * 100 + day)
                equity.append(val)
            eq = month_end
        else:
            for day in range(1, 21):
                dates.append(2010 * 10000 + m * 100 + day)
                equity.append(eq)
            eq = eq * (1.0 + rr)
            dates.append(2010 * 10000 + m * 100 + 28)
            equity.append(eq)

    cmp = compare(
        np.array(dates), np.array(equity), target,
        section="maxdd-basis",
        given_ann_ret_pct=None,
        given_maxdd_pct=given_monthly_maxdd,  # monthly-granularity given
    )
    # Daily maxDD is the -40% intra-month crash; month-end maxDD is far shallower.
    assert cmp.repro_maxdd_pct < -35.0, cmp.repro_maxdd_pct
    assert abs(cmp.repro_maxdd_monthly_pct) < 0.5 * abs(cmp.repro_maxdd_pct)
    # Repro month-ends EQUAL the target ⇒ monthly-vs-monthly ratio ≈ 1.0.
    assert cmp.maxdd_ratio_monthly is not None
    assert 0.90 <= cmp.maxdd_ratio_monthly <= 1.10, cmp.maxdd_ratio_monthly
    # Daily-basis ratio is inflated (deep daily / shallow monthly-given).
    assert cmp.maxdd_ratio is not None and cmp.maxdd_ratio > DEFAULT_BAND.maxdd_ratio_hi

    v_daily = check_band(cmp, DEFAULT_BAND, maxdd_basis="daily")
    v_monthly = check_band(cmp, DEFAULT_BAND, maxdd_basis="monthly")
    daily_line = _maxdd_line(v_daily)
    monthly_line = _maxdd_line(v_monthly)
    print(f"\n[maxdd-basis] daily:   {daily_line}")
    print(f"[maxdd-basis] monthly: {monthly_line}")
    assert daily_line.startswith("FAIL") and "(daily)" in daily_line
    assert monthly_line.startswith("PASS") and "(monthly)" in monthly_line


# --------------------------------------------------------------------------- #
# 4. DB-FREE USD_1M_rate flat-1% reproduction (pure cash_rate engine)
# --------------------------------------------------------------------------- #


def test_usd_1m_rate_flat1_reproduction_dbfree():
    """Reproduce the USD_1M_rate(P) cash leg as flat 1%/yr via the PURE
    ``accrue_cash_equity`` engine (no dwh), build the monthly grid, and compare
    to the golden target.

    This validates (a) the harness end to end on real target data and (b) the
    "cash carry" MECHANIC (monotone-up, ~0 vol, all-positive drift).  It is NOT
    a shape match: flat-1% is CONSTANT while the target rides the real short-rate
    path (higher in 2006-08 & 2023-25, ~0 in the ZIRP years) — the documented,
    expected level divergence (F4: no dwh rate series exists, flat-1% operative).
    """
    target, checks = parse_target_section("USD_1M_rate(P)")
    dates = _business_days("2006-01-03", "2026-06-12")
    equity = accrue_cash_equity(0.01, n=dates.shape[0], compound=True)  # flat 1 %/yr

    cmp = compare(
        dates, equity, target,
        section="USD_1M_rate(P) flat-1%",
        given_ann_ret_pct=1.8,      # SPEC header (rate-path average, NOT flat-1%)
        given_maxdd_pct=None,       # cash leg has ~0 drawdown
        checksums=checks,
        ruin_floor=99.0,            # a cash leg must never dip below funding
    )

    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                      title="USD_1M_rate(P): flat-1% vs target"))
    print(f"\n[USD cash] overlap_months={cmp.n_overlap_months} "
          f"monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[USD cash] repro ann_ret={cmp.repro_ann_ret_pct:.3f}% "
          f"min_equity={cmp.repro_min_equity:.4f} ruin_ok={cmp.ruin_ok}")

    # MECHANIC assertions (what flat-1% MUST satisfy):
    assert np.all(np.diff(equity) >= -1e-12), "cash equity went down"   # monotone up
    assert cmp.repro_min_equity >= 100.0 - 1e-6                          # never below funding
    assert cmp.repro_ann_ret_pct == pytest.approx(1.0, abs=0.05)        # ~1 %/yr
    # Reproduced monthly returns are near-constant (only tiny business-day-count
    # variation, ~0.077-0.088 %/month), so they carry ~no information about the
    # real rate path.  The honest outcome is a NEAR-ZERO monthly corr — flat-1%
    # does NOT reproduce the rate-path SHAPE (the documented divergence), while
    # the equity-level corr is spuriously high because BOTH curves merely rise.
    repro_vals = np.array([cmp.repro_grid.cells[k] for k in cmp.repro_grid.cells])
    assert repro_vals.std() < 0.02, repro_vals.std()          # ~flat month-to-month
    assert abs(cmp.monthly_corr) < 0.30, (                    # no shape tracking
        f"flat-1% unexpectedly correlates with the rate path: {cmp.monthly_corr}"
    )
    print(f"[USD cash] DIVERGENCE: monthly_corr={cmp.monthly_corr:.3f} (~0 => flat-1% "
          f"does NOT track the rate path); equity_log_corr={cmp.equity_log_corr:.3f} "
          f"(spurious trend, both curves rise)")


# --------------------------------------------------------------------------- #
# 5. saved-legs -> compute-body translation (the persisted-entity recipe)
# --------------------------------------------------------------------------- #


def test_saved_legs_to_compute_body_50d():
    """The stored (array, weight-inline) legs translate to the dict-keyed
    compute body exactly as the frontend does — with sizing_mode preserved and
    weights split into their own dict."""
    saved = [
        {
            "label": "put", "type": "option_stream", "collection": "OPT_SP_500",
            "option_type": "P", "cycle": "M",
            "maturity": {"kind": "nearest_to_target", "target_days": 60},
            "selection": {"kind": "by_delta", "target": -0.50, "tolerance": 0.10},
            "stream": "close", "hold_between_rolls": True, "nav_times": 1.0,
            "sizing_mode": "futures_notional", "weight": -100.0,
        }
    ]
    body = saved_legs_to_compute_body(saved, start="2006-01-01", end="2026-06-11")
    assert set(body["legs"]) == {"put"}
    leg = body["legs"]["put"]
    assert leg["type"] == "option_stream"
    assert leg["collection"] == "OPT_SP_500"
    assert leg["sizing_mode"] == "futures_notional"
    assert leg["hold_between_rolls"] is True
    assert body["weights"] == {"put": -100.0}
    # UI-faithful default: single leg -> Σ|w| normalizes to 1.0× (short sign, no
    # rescale) — the val_5_1 recipe; the leveraged combine sets this False later.
    assert body["normalize_weights"] is True
    assert body["start"] == "2006-01-01"
    assert body["return_type"] == "normal"


def test_saved_legs_to_compute_body_cash():
    saved = [
        {"label": "cash", "type": "cash_rate", "weight": 100.0, "data_source": "v2",
         "cash_rate": {"collection": "RATE", "symbol": "RATE_US_CMT_1M",
                       "unit": "percent", "compound": True}},
    ]
    body = saved_legs_to_compute_body(saved)
    assert body["legs"]["cash"] == {
        "type": "cash_rate",
        "data_source": "v2",
        "cash_rate": {"collection": "RATE", "symbol": "RATE_US_CMT_1M",
                      "unit": "percent", "compound": True},
    }
    assert body["weights"] == {"cash": 100.0}
