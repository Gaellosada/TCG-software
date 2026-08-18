"""Wave-0 CALIBRATION golden tests — per-leg reproduction through the REAL,
PERSISTED-entity path (POST persistence -> GET -> translate -> POST compute).

These are integration-gated (live dwh + app-data).  Each test:
  1. persists a leg as a portfolio doc via the real persistence API
     (write-through ``tcg.persistence`` to ``tcg_app_data``);
  2. reads it back and translates the stored doc to a compute body exactly as
     the frontend does (harness.saved_legs_to_compute_body);
  3. runs ``/api/portfolio/compute`` and validates the reproduced monthly grid
     against the golden ``monthly_pnl_targets.md`` section, within the Wave-0
     tolerance band.

Run: ``uv run pytest tests/integration/strategy_repro/test_val_wave_calibration.py
      --run-integration -s``
"""

from __future__ import annotations

import uuid

import numpy as np
import pytest

from tcg.core.app import create_app

from strategy_repro.harness import (
    DEFAULT_BAND,
    REPRO_ENTITY_10D_PUT,
    REPRO_ENTITY_50D_PUT,
    REPRO_ENTITY_USD_1M_RATE,
    REPRO_VISIBLE_CATEGORY,
    check_band,
    compare,
    durable_persist_and_run_portfolio,
    equity_to_monthly_grid,
    format_side_by_side,
    parse_target_section,
    persist_and_run_portfolio,
    rebase_to_100,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def client():
    import httpx

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            # Generous timeout: the 50Δ leg runs a full ~20-year option backtest.
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=1800.0
            ) as c:
                yield c
    except Exception as exc:  # noqa: BLE001 — pool connect failure => skip
        pytest.skip(f"live dwh/app-data not reachable: {exc}")


def _finite(seq) -> np.ndarray:
    return np.asarray([np.nan if v is None else v for v in seq], dtype=float)


# --------------------------------------------------------------------------- #
# USD_1M_rate(P) — §5.7 F4 cash leg (REAL US 1M CMT rate series, CASH-ONLY)
# --------------------------------------------------------------------------- #
# Legacy-reported headline ann_ret for the §5.7 cash leg (target section header
# "cash leg; ann_ret ~1.8%, ~0 vol").  Only feeds the ann_ret |Δ| band gate.
_USD_RATE_GIVEN_ANN = 1.8


def _seg_avg_daily_factor(dates, equity, lo_iso: str, hi_iso: str) -> float:
    """Mean per-bar growth factor of ``equity`` over the [lo, hi) date window.

    ``dates`` are ISO strings; ``equity`` the aligned per-bar curve. Returns the
    average of ``eq[i]/eq[i-1]`` for bars whose date falls in the window — a
    proxy for the accrual RATE active in that period.
    """
    lo = int(lo_iso.replace("-", ""))
    hi = int(hi_iso.replace("-", ""))
    di = np.array([int(d.replace("-", "")) for d in dates], dtype=np.int64)
    fac = equity[1:] / equity[:-1]
    mask = (di[1:] >= lo) & (di[1:] < hi)
    return float(np.mean(fac[mask])) if mask.any() else float("nan")


async def test_usd_1m_rate_persisted_entity(client):
    """Persist a CASH-ONLY cash_rate leg reading the REAL US 1M CMT rate series
    (RATE/RATE_US_CMT_1M, data_source='v2') and validate that it ACCRUES with the
    right shape: rises fast in the high-rate regimes (pre-2008, 2023-24), ~flat in
    the ZIRP years, monotone-up, never below funding.

    This is the F4 series repoint: the flat-1% source and the FUT_VIX companion
    are GONE. A rate-only portfolio is now valid — the rate series supplies its
    own trading calendar. Because the source is now the real path, the reproduced
    curve TRACKS the USD_1M_rate(P) golden (materially better than the old flat).
    """
    target, checks = parse_target_section("USD_1M_rate(P)")
    saved_legs = [
        {
            "label": "cash", "type": "cash_rate", "weight": 100.0,
            "data_source": "v2",
            "cash_rate": {
                "collection": "RATE", "symbol": "RATE_US_CMT_1M",
                "unit": "percent", "compound": True,
            },
        },
    ]
    # DURABLE, UI-visible entity `Reproduction_USD_1M_rate` (idempotent upsert,
    # DEV category via REPRO_VISIBLE_CATEGORY, NOT soft-deleted). The stored doc is
    # now a SINGLE cash leg (no companion) — a non-dev opens + runs it from the
    # Portfolio page.
    doc, result = await durable_persist_and_run_portfolio(
        client, saved_legs,
        portfolio_id=REPRO_ENTITY_USD_1M_RATE,
        name=REPRO_ENTITY_USD_1M_RATE,
        category=REPRO_VISIBLE_CATEGORY,
        start="2006-01-03", end="2026-06-11",
    )
    assert doc["category"] == REPRO_VISIBLE_CATEGORY and doc["category"] != "DELETED"
    # ---- REPOINT CONFIRMATION (read-back the STORED entity) ---------------- #
    # The durable entity must now be a SINGLE real-rate cash leg with NO FUT_VIX
    # calendar companion.  Assert the stored doc's legs directly (this is the
    # tcg_app_data round-trip, not the in-memory saved_legs).
    stored = doc["legs"]
    assert len(stored) == 1, f"expected a single leg, got {stored}"
    only = stored[0]
    assert only["type"] == "cash_rate", only
    assert not any(
        (leg.get("type") == "continuous") or (leg.get("collection") == "FUT_VIX")
        for leg in stored
    ), f"a FUT_VIX companion still remains: {stored}"
    assert only["data_source"] == "v2", only
    assert only["cash_rate"]["collection"] == "RATE", only
    assert only["cash_rate"]["symbol"] == "RATE_US_CMT_1M", only
    # Cash-only accepted: the single leg's own base-100 accrual curve.
    dates = result["dates"]
    cash_eq = _finite(result["leg_equities"]["cash"])
    ok = np.isfinite(cash_eq)
    dates = [d for d, k in zip(dates, ok) if k]
    cash_eq = rebase_to_100(cash_eq[ok])

    cmp = compare(
        dates, cash_eq, target,
        section="USD_1M_rate(P) series [persisted]",
        # Legacy-reported headline for the §5.7 cash leg (target section header:
        # "ann_ret ~1.8%").  Used only for the ann_ret |Δ| band gate (2.0pp).
        given_ann_ret_pct=_USD_RATE_GIVEN_ANN, given_maxdd_pct=None,
        checksums=checks, ruin_floor=99.0,
    )
    # Cash leg: target is a monotone-up monthly grid, so gate the drawdown on the
    # MONTHLY-vs-MONTHLY basis (daily-vs-monthly is apples-to-oranges here).
    verdict = check_band(cmp, DEFAULT_BAND, maxdd_basis="monthly")
    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                     title="USD_1M_rate(P) persisted: real series vs target"))
    print(f"\n[USD persisted] bars={cash_eq.shape[0]} overlap_months={cmp.n_overlap_months} "
          f"monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[USD persisted] repro ann_ret={cmp.repro_ann_ret_pct:.3f}% "
          f"(given {_USD_RATE_GIVEN_ANN}%, |Δ|={cmp.ann_ret_abs_diff_pp:.2f}pp) "
          f"min_equity={cmp.repro_min_equity:.4f} ruin_ok={cmp.ruin_ok}")

    # Shape: monotone-up (rate >= 0 over the window), never below funding.
    assert np.all(np.diff(cash_eq) >= -1e-9), "cash equity went down"
    assert cmp.repro_min_equity >= 100.0 - 1e-6
    # Regime shape: the high-rate windows accrue FASTER than the ZIRP trough.
    f_pre08 = _seg_avg_daily_factor(dates, cash_eq, "2006-08-01", "2007-12-31")
    f_zirp = _seg_avg_daily_factor(dates, cash_eq, "2011-01-01", "2015-01-01")
    f_2324 = _seg_avg_daily_factor(dates, cash_eq, "2023-06-01", "2024-12-31")
    assert f_pre08 > f_zirp, "pre-2008 (~5%) did not out-accrue ZIRP"
    assert f_2324 > f_zirp, "2023-24 (~5%) did not out-accrue ZIRP"
    assert f_zirp == pytest.approx(1.0, abs=5e-6), "ZIRP window should be ~flat"
    # The repointed real-rate series TRACKS the §5.7 golden and PASSES the Wave-0
    # band (monthly_corr>=0.80, equity_corr>=0.90, ann_ret|Δ|<=2pp, min>50) —
    # MATERIALLY better than the removed flat-1% leg (whose monthly_corr was 0.07).
    assert cmp.checksum_failures == [], cmp.checksum_failures
    assert verdict.passed, verdict.reasons
    # Tight regression floors just below the observed real-dwh values (0.90/0.99).
    assert cmp.monthly_corr >= 0.85, cmp.monthly_corr
    assert cmp.equity_log_corr >= 0.98, cmp.equity_log_corr


# --------------------------------------------------------------------------- #
# Short_SPX_50d_Put_2M — §5.1 always-on 50Δ put (val_5_1 recipe)
# --------------------------------------------------------------------------- #
_PUT_50D_GIVEN_ANN = 6.78
_PUT_50D_GIVEN_MAXDD = -34.58


async def test_short_spx_50d_put_persisted_entity(client):
    """Persist the always-on 50Δ short-put leg (val_5_1 config, futures_notional
    sizing), run the full 2006->2026 window, and validate the reproduced monthly
    grid against the Short_SPX_50d_Put_2M target within the Wave-0 band."""
    target, checks = parse_target_section("Short_SPX_50d_Put_2M")
    saved_legs = [
        {
            "label": "put", "type": "option_stream", "collection": "OPT_SP_500",
            "option_type": "P", "cycle": "M",
            "maturity": {"kind": "nearest_to_target", "target_days": 60},
            "selection": {"kind": "by_delta", "target": -0.50, "tolerance": 0.10},
            "stream": "close", "hold_between_rolls": True, "nav_times": 1.0,
            "sizing_mode": "futures_notional", "weight": -100.0,
        }
    ]
    result = await persist_and_run_portfolio(
        client, saved_legs,
        portfolio_id=f"valwave-50dput-{uuid.uuid4().hex[:8]}",
        name="Wave0 Short_SPX_50d_Put_2M (calibration)",
        start="2006-01-01", end="2026-06-11",
        # Single standalone leg: Σ|w| normalizes -100 to -1.0× (short sign, no
        # rescale) — the val_5_1 recipe.
        normalize_weights=True,
    )
    dates = result["dates"]
    eq = _finite(result["portfolio_equity"])   # short sign already applied
    ok = np.isfinite(eq)
    dates = [d for d, k in zip(dates, ok) if k]
    eq = eq[ok]

    cmp = compare(
        dates, eq, target,
        section="Short_SPX_50d_Put_2M [persisted]",
        given_ann_ret_pct=_PUT_50D_GIVEN_ANN,
        given_maxdd_pct=_PUT_50D_GIVEN_MAXDD,
        checksums=checks,
    )
    verdict = check_band(cmp, DEFAULT_BAND)
    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                     title="Short_SPX_50d_Put_2M persisted vs target"))
    print(f"\n[50d persisted] bars={eq.shape[0]} overlap_months={cmp.n_overlap_months}")
    print(f"[50d persisted] monthly_corr={cmp.monthly_corr:.4f} "
          f"equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[50d persisted] repro ann_ret={cmp.repro_ann_ret_pct:.2f}% "
          f"(given {_PUT_50D_GIVEN_ANN}%, |Δ|={cmp.ann_ret_abs_diff_pp:.2f}pp)")
    print(f"[50d persisted] repro maxDD={cmp.repro_maxdd_pct:.2f}% "
          f"(given {_PUT_50D_GIVEN_MAXDD}%, ratio={cmp.maxdd_ratio:.2f})")
    print(f"[50d persisted] min_equity={cmp.repro_min_equity:.2f} ruin_ok={cmp.ruin_ok}")
    for line in verdict.reasons:
        print(f"  band: {line}")

    # BASELINE (SUPERSEDED roll): this leg uses NearestToTarget(60)+hold, whose
    # near-monthly roll drifts off the calendar month-end and depresses
    # monthly_corr to 0.780 — BELOW the 0.80 band.  The CANONICAL §5.1 config is
    # the strict-EOM test (test_short_spx_50d_put_EOM_persisted_entity, 0.897).
    # This test is kept for the roll-timing HEAD-TO-HEAD, so it does NOT gate on
    # the 0.80 monthly_corr band — only on equity_corr (primary shape gate),
    # magnitudes, ruin, and an observed-value regression floor.
    assert cmp.checksum_failures == [], cmp.checksum_failures
    assert cmp.monthly_corr >= 0.76, cmp.monthly_corr        # regression floor
    assert cmp.equity_log_corr >= 0.99, cmp.equity_log_corr  # primary shape gate
    assert cmp.ann_ret_abs_diff_pp <= DEFAULT_BAND.ann_ret_abs_pp_max, cmp.ann_ret_abs_diff_pp
    assert (
        DEFAULT_BAND.maxdd_ratio_lo <= cmp.maxdd_ratio <= DEFAULT_BAND.maxdd_ratio_hi
    ), cmp.maxdd_ratio
    assert cmp.ruin_ok, cmp.repro_min_equity


# --------------------------------------------------------------------------- #
# Short_SPX_10d_Put_2M — §5.1 always-on 10Δ put (val_5_1 recipe, target -0.10)
# --------------------------------------------------------------------------- #
_PUT_10D_GIVEN_ANN = 3.13
_PUT_10D_GIVEN_MAXDD = -13.07


async def test_short_spx_10d_put_persisted_entity(client):
    """Persist the always-on 10Δ short-put leg (val_5_1 config, target -0.10,
    futures_notional sizing), run the full 2006->2026 window, and validate the
    reproduced monthly grid against the Short_SPX_10d_Put_2M target."""
    target, checks = parse_target_section("Short_SPX_10d_Put_2M")
    saved_legs = [
        {
            "label": "put", "type": "option_stream", "collection": "OPT_SP_500",
            "option_type": "P", "cycle": "M",
            "maturity": {"kind": "nearest_to_target", "target_days": 60},
            "selection": {"kind": "by_delta", "target": -0.10, "tolerance": 0.10},
            "stream": "close", "hold_between_rolls": True, "nav_times": 1.0,
            "sizing_mode": "futures_notional", "weight": -100.0,
        }
    ]
    result = await persist_and_run_portfolio(
        client, saved_legs,
        portfolio_id=f"valwave-10dput-{uuid.uuid4().hex[:8]}",
        name="Wave0 Short_SPX_10d_Put_2M (calibration)",
        start="2006-01-01", end="2026-06-11",
        normalize_weights=True,
    )
    dates = result["dates"]
    eq = _finite(result["portfolio_equity"])
    ok = np.isfinite(eq)
    dates = [d for d, k in zip(dates, ok) if k]
    eq = eq[ok]

    cmp = compare(
        dates, eq, target,
        section="Short_SPX_10d_Put_2M [persisted]",
        given_ann_ret_pct=_PUT_10D_GIVEN_ANN,
        given_maxdd_pct=_PUT_10D_GIVEN_MAXDD,
        checksums=checks,
    )
    verdict = check_band(cmp, DEFAULT_BAND)
    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                     title="Short_SPX_10d_Put_2M persisted vs target"))
    print(f"\n[10d persisted] bars={eq.shape[0]} overlap_months={cmp.n_overlap_months}")
    print(f"[10d persisted] monthly_corr={cmp.monthly_corr:.4f} "
          f"equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[10d persisted] repro ann_ret={cmp.repro_ann_ret_pct:.2f}% "
          f"(given {_PUT_10D_GIVEN_ANN}%, |Δ|={cmp.ann_ret_abs_diff_pp:.2f}pp)")
    print(f"[10d persisted] repro maxDD={cmp.repro_maxdd_pct:.2f}% "
          f"(given {_PUT_10D_GIVEN_MAXDD}%, ratio={cmp.maxdd_ratio:.2f})")
    print(f"[10d persisted] min_equity={cmp.repro_min_equity:.2f} ruin_ok={cmp.ruin_ok}")
    for line in verdict.reasons:
        print(f"  band: {line}")

    # BASELINE (SUPERSEDED roll): NearestToTarget(60)+hold, monthly_corr 0.764
    # (below the 0.80 band).  Canonical §5.1 config = the strict-EOM test
    # (test_short_spx_10d_put_EOM_persisted_entity, 0.873).  Kept for the roll
    # head-to-head; gates only on equity_corr + magnitudes + regression floor.
    assert cmp.checksum_failures == [], cmp.checksum_failures
    assert cmp.monthly_corr >= 0.74, cmp.monthly_corr
    assert cmp.equity_log_corr >= 0.97, cmp.equity_log_corr
    assert cmp.ann_ret_abs_diff_pp <= DEFAULT_BAND.ann_ret_abs_pp_max, cmp.ann_ret_abs_diff_pp
    assert (
        DEFAULT_BAND.maxdd_ratio_lo <= cmp.maxdd_ratio <= DEFAULT_BAND.maxdd_ratio_hi
    ), cmp.maxdd_ratio
    assert cmp.ruin_ok, cmp.repro_min_equity


# --------------------------------------------------------------------------- #
# EOM ROLL smoke — confirm end_of_month(offset) rolls at CALENDAR month-end
# with a ~2M target-delta contract (SHORT window, fast).
# --------------------------------------------------------------------------- #
import pytest as _pytest


@_pytest.mark.parametrize("offset_months", [2, 3])
async def test_eom_roll_smoke(client, offset_months):
    """A short-window 50Δ put with maturity=end_of_month(offset) must compose,
    produce finite equity, and roll ~once per calendar month (12/yr)."""
    saved_legs = [
        {
            "label": "put", "type": "option_stream", "collection": "OPT_SP_500",
            "option_type": "P", "cycle": "M",
            "maturity": {"kind": "end_of_month", "offset_months": offset_months},
            "selection": {"kind": "by_delta", "target": -0.50, "tolerance": 0.10},
            "stream": "close", "hold_between_rolls": True, "nav_times": 1.0,
            "sizing_mode": "futures_notional", "weight": -100.0,
        }
    ]
    result = await persist_and_run_portfolio(
        client, saved_legs,
        portfolio_id=f"valwave-eomsmoke-{offset_months}-{uuid.uuid4().hex[:8]}",
        name=f"EOM smoke offset={offset_months}",
        start="2015-01-01", end="2017-01-01", normalize_weights=True,
    )
    eq = _finite(result["portfolio_equity"])
    eq = eq[np.isfinite(eq)]
    roll_rows = [t for t in result.get("trades", []) if t.get("input_id") == "put"]
    n_years = 2.0
    print(f"\n[EOM smoke offset={offset_months}] bars={eq.shape[0]} "
          f"roll_rows={len(roll_rows)} (~{len(roll_rows)/n_years:.1f}/yr) "
          f"eq[end={eq[-1]:.3f} min={eq.min():.3f}]")
    assert eq.shape[0] > 400 and np.all(np.isfinite(eq))
    # ~12 rolls/yr for a monthly calendar roll (allow 9-15/yr slack).
    assert 18 <= len(roll_rows) <= 30, len(roll_rows)


# --------------------------------------------------------------------------- #
# EOM full-window golden tests — STRICT calendar-month roll (end_of_month
# offset=2) matching the legacy simpleMonthlyPutRollEom.  Head-to-head vs the
# NearestToTarget(60) baseline in the report.  Daily equity dumped to CSV.
# --------------------------------------------------------------------------- #
import csv as _csv
from pathlib import Path as _Path

_OUTPUT_DIR = (
    _Path(__file__).resolve().parents[3].parent
    / "workspace" / "tasks" / "strategy-repro-impl" / "output"
)


def _dump_daily_csv(dates, equity, filename: str) -> None:
    path = _OUTPUT_DIR / filename
    with path.open("w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["date", "equity"])
        for d, e in zip(dates, equity):
            w.writerow([d, f"{float(e):.6f}"])


async def _run_eom_put_leg(client, *, target_delta, portfolio_id, name):
    saved_legs = [
        {
            "label": "put", "type": "option_stream", "collection": "OPT_SP_500",
            "option_type": "P", "cycle": "M",
            # STRICT calendar-EOM roll at ~2M tenor (offset=2) — legacy roll.
            "maturity": {"kind": "end_of_month", "offset_months": 2},
            "selection": {"kind": "by_delta", "target": target_delta, "tolerance": 0.10},
            "stream": "close", "hold_between_rolls": True, "nav_times": 1.0,
            "sizing_mode": "futures_notional", "weight": -100.0,
        }
    ]
    # DURABLE, UI-visible entity `Reproduction_<leg>` (idempotent upsert, DEV
    # category via REPRO_VISIBLE_CATEGORY, NOT soft-deleted) — the canonical §5.1
    # reproduction a non-dev opens + runs from the Portfolio page.
    doc, result = await durable_persist_and_run_portfolio(
        client, saved_legs, portfolio_id=portfolio_id, name=name,
        category=REPRO_VISIBLE_CATEGORY,
        start="2006-01-01", end="2026-06-11", normalize_weights=True,
    )
    assert doc["category"] == REPRO_VISIBLE_CATEGORY and doc["category"] != "DELETED"
    dates = result["dates"]
    eq = _finite(result["portfolio_equity"])
    ok = np.isfinite(eq)
    dates = [d for d, k in zip(dates, ok) if k]
    eq = eq[ok]
    return dates, eq


async def test_short_spx_50d_put_EOM_persisted_entity(client):
    """50Δ put, STRICT calendar-EOM roll (end_of_month offset=2).

    Canonical §5.1 config, persisted as the DURABLE UI-visible entity
    `Reproduction_Short_SPX_50d_Put_2M`.
    """
    target, checks = parse_target_section("Short_SPX_50d_Put_2M")
    dates, eq = await _run_eom_put_leg(
        client, target_delta=-0.50,
        portfolio_id=REPRO_ENTITY_50D_PUT,
        name=REPRO_ENTITY_50D_PUT)
    _dump_daily_csv(dates, eq, "daily_equity_50d_eom.csv")
    cmp = compare(dates, eq, target, section="Short_SPX_50d_Put_2M [EOM]",
                  given_ann_ret_pct=_PUT_50D_GIVEN_ANN, given_maxdd_pct=_PUT_50D_GIVEN_MAXDD,
                  checksums=checks)
    verdict = check_band(cmp, DEFAULT_BAND)
    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                     title="Short_SPX_50d_Put_2M EOM vs target"))
    print(f"\n[50d EOM] bars={eq.shape[0]} overlap_months={cmp.n_overlap_months} "
          f"monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[50d EOM] ann_ret={cmp.repro_ann_ret_pct:.2f}% (|Δ|={cmp.ann_ret_abs_diff_pp:.2f}pp) "
          f"maxDD={cmp.repro_maxdd_pct:.2f}% (ratio={cmp.maxdd_ratio:.2f}) "
          f"min_equity={cmp.repro_min_equity:.2f}")
    for line in verdict.reasons:
        print(f"  band: {line}")
    # CANONICAL §5.1 50Δ: full confirmed band (monthly_corr >= 0.80) + tight
    # regression floors just below observed (0.897 / 0.987).
    assert cmp.checksum_failures == []
    assert verdict.passed, verdict.reasons
    assert cmp.monthly_corr >= 0.88, cmp.monthly_corr
    assert cmp.equity_log_corr >= 0.98, cmp.equity_log_corr
    assert cmp.ann_ret_abs_diff_pp <= DEFAULT_BAND.ann_ret_abs_pp_max, cmp.ann_ret_abs_diff_pp


async def test_short_spx_10d_put_EOM_persisted_entity(client):
    """10Δ put, STRICT calendar-EOM roll (end_of_month offset=2).

    Canonical §5.1 config, persisted as the DURABLE UI-visible entity
    `Reproduction_Short_SPX_10d_Put_2M`.
    """
    target, checks = parse_target_section("Short_SPX_10d_Put_2M")
    dates, eq = await _run_eom_put_leg(
        client, target_delta=-0.10,
        portfolio_id=REPRO_ENTITY_10D_PUT,
        name=REPRO_ENTITY_10D_PUT)
    _dump_daily_csv(dates, eq, "daily_equity_10d_eom.csv")
    cmp = compare(dates, eq, target, section="Short_SPX_10d_Put_2M [EOM]",
                  given_ann_ret_pct=_PUT_10D_GIVEN_ANN, given_maxdd_pct=_PUT_10D_GIVEN_MAXDD,
                  checksums=checks)
    verdict = check_band(cmp, DEFAULT_BAND)
    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                     title="Short_SPX_10d_Put_2M EOM vs target"))
    print(f"\n[10d EOM] bars={eq.shape[0]} overlap_months={cmp.n_overlap_months} "
          f"monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[10d EOM] ann_ret={cmp.repro_ann_ret_pct:.2f}% (|Δ|={cmp.ann_ret_abs_diff_pp:.2f}pp) "
          f"maxDD={cmp.repro_maxdd_pct:.2f}% (ratio={cmp.maxdd_ratio:.2f}) "
          f"min_equity={cmp.repro_min_equity:.2f}")
    for line in verdict.reasons:
        print(f"  band: {line}")
    # CANONICAL §5.1 10Δ: full confirmed band + regression floors (0.873 / 0.997).
    assert cmp.checksum_failures == []
    assert verdict.passed, verdict.reasons
    assert cmp.monthly_corr >= 0.86, cmp.monthly_corr
    assert cmp.equity_log_corr >= 0.99, cmp.equity_log_corr
    assert cmp.ann_ret_abs_diff_pp <= DEFAULT_BAND.ann_ret_abs_pp_max, cmp.ann_ret_abs_diff_pp


# --------------------------------------------------------------------------- #
# v2 WEEKLY PROXY (v1->v2 rebase, Gael 2026-08-07) — the §5.1 puts run on the v2
# (Databento) warehouse.  v2 has NO monthly S&P options (empty object + the
# adapter raises V2UnsupportedCycle for cycle "M"), so the closest cadence is the
# EW3 "W3 Friday" weekly — the classic monthly 3rd-Friday expiry — with greeks
# from 2016-02-22.  This is a deliberately WINDOW-LIMITED proxy (2016-2026, no
# 2008/GFC): the faithful full-window reproduction stays on v1 (the _EOM_ tests
# above).  Gated on SHAPE over the available overlap (monthly_corr, equity_log_corr,
# no-ruin); ann_ret/maxDD are informational because the proxy window differs from
# the full 2006-2026 target window.  Persisted as a SEPARATE durable DEV entity so
# the v1 faithful and the v2 proxy coexist and are both UI-visible.
# --------------------------------------------------------------------------- #
REPRO_ENTITY_50D_PUT_V2WK = "Reproduction_Short_SPX_50d_Put_2M_v2wk"
REPRO_ENTITY_10D_PUT_V2WK = "Reproduction_Short_SPX_10d_Put_2M_v2wk"
_V2WK_START = "2016-02-22"  # v2 EW3 greeks floor
_V2WK_END = "2026-06-11"


async def _run_v2wk_put_leg(client, *, target_delta, portfolio_id, name):
    saved_legs = [
        {
            "label": "put", "type": "option_stream", "collection": "OPT_SP_500",
            "option_type": "P", "cycle": "W3 Friday", "data_source": "v2",
            "maturity": {"kind": "nearest_to_target", "target_days": 60},
            "selection": {"kind": "by_delta", "target": target_delta, "tolerance": 0.10},
            "stream": "close", "hold_between_rolls": True, "nav_times": 1.0,
            "sizing_mode": "futures_notional", "weight": -100.0,
        }
    ]
    doc, result = await durable_persist_and_run_portfolio(
        client, saved_legs, portfolio_id=portfolio_id, name=name,
        category=REPRO_VISIBLE_CATEGORY,
        start=_V2WK_START, end=_V2WK_END, normalize_weights=True,
    )
    assert doc["category"] == REPRO_VISIBLE_CATEGORY and doc["category"] != "DELETED"
    # rebase proof: the persisted leg actually carries the v2 source.
    assert any(leg.get("data_source") == "v2" for leg in doc["legs"]), doc["legs"]
    dates = result["dates"]
    eq = np.array([np.nan if v is None else v for v in result["portfolio_equity"]], dtype=float)
    ok = np.isfinite(eq)
    return [d for d, k in zip(dates, ok) if k], eq[ok]


async def test_short_spx_50d_put_v2wk_proxy(client):
    """§5.1 50Δ put — v2 EW3-weekly proxy (2016+), durable DEV entity
    ``Reproduction_Short_SPX_50d_Put_2M_v2wk``.  Shape-gated over the overlap."""
    target, checks = parse_target_section("Short_SPX_50d_Put_2M")
    dates, eq = await _run_v2wk_put_leg(
        client, target_delta=-0.50,
        portfolio_id=REPRO_ENTITY_50D_PUT_V2WK, name=REPRO_ENTITY_50D_PUT_V2WK)
    cmp = compare(dates, eq, target, section="Short_SPX_50d_Put_2M [v2 EW3-wk proxy]",
                  checksums=checks)
    print(f"\n[50d v2wk] overlap_months={cmp.n_overlap_months} "
          f"monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f} "
          f"ann_ret={cmp.repro_ann_ret_pct:.2f}% maxDD={cmp.repro_maxdd_pct:.2f}% "
          f"min_equity={cmp.repro_min_equity:.2f}")
    assert cmp.checksum_failures == []
    assert cmp.monthly_corr >= 0.80, cmp.monthly_corr
    assert cmp.equity_log_corr >= 0.90, cmp.equity_log_corr
    assert cmp.repro_min_equity > DEFAULT_BAND.ruin_floor, cmp.repro_min_equity


async def test_short_spx_10d_put_v2wk_proxy(client):
    """§5.1 10Δ put — v2 EW3-weekly proxy (2016+), durable DEV entity
    ``Reproduction_Short_SPX_10d_Put_2M_v2wk``.  Shape-gated over the overlap
    (10Δ monthly_corr sits near the 0.80 floor — the leg is thin-tailed)."""
    target, checks = parse_target_section("Short_SPX_10d_Put_2M")
    dates, eq = await _run_v2wk_put_leg(
        client, target_delta=-0.10,
        portfolio_id=REPRO_ENTITY_10D_PUT_V2WK, name=REPRO_ENTITY_10D_PUT_V2WK)
    cmp = compare(dates, eq, target, section="Short_SPX_10d_Put_2M [v2 EW3-wk proxy]",
                  checksums=checks)
    print(f"\n[10d v2wk] overlap_months={cmp.n_overlap_months} "
          f"monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f} "
          f"ann_ret={cmp.repro_ann_ret_pct:.2f}% maxDD={cmp.repro_maxdd_pct:.2f}% "
          f"min_equity={cmp.repro_min_equity:.2f}")
    assert cmp.checksum_failures == []
    # NOTE (thin margin, intentional): observed monthly_corr ~= 0.8019 sits just
    # above the Wave-0 band floor 0.80 — the 10Δ leg is thin-tailed and this is a
    # window-limited v2 proxy.  The gate is deliberately the band value (not a
    # regression floor): if a future in-window v2 data correction pushes it below
    # 0.80 the proxy genuinely no longer meets the band, and a FAIL is the correct
    # signal to revisit — not a flake to paper over.
    assert cmp.monthly_corr >= 0.80, cmp.monthly_corr
    assert cmp.equity_log_corr >= 0.90, cmp.equity_log_corr
    assert cmp.repro_min_equity > DEFAULT_BAND.ruin_floor, cmp.repro_min_equity
