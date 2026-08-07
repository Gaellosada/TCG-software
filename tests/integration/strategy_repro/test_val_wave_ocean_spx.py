"""Wave-1 golden test — SPEC §5.3 Ocean base leg (``OceanVVIXthird50_spx``)
reproduction through the REAL, PERSISTED SIGNAL-entity path
(POST persistence/signals -> GET -> build compute body -> POST signals/compute).

THE LEG (SPEC §5.3, authoritative — screenshots win)
────────────────────────────────────────────────────
An INDEX leg: **long 100% SPX** (IND_SP_500), gated intermittently by the Ocean
signal.  Compute-cheap (no option legs), ~18 s/run.

  ENTER if [ DSTAT_VVIX completes "hits p95 then descends to p75" (hysteresis UP)
             AND VVIX > MA50(VVIX) ]
        OR [ DSTAT_SPX crosses above its DSTAT_10 line for the 3rd time ]
  EXIT  if [ DSTAT_VIX completes "hits p10 then rises to p20" (hysteresis DOWN) ]
        OR [ DSTAT_SPX completes "hits p60 then drops to p50" (hysteresis UP) ]

DStat canonical params on EACH series (§4.1): MA=21, vol=63, percentile
window=1260.  SPX=IND_SP_500, VVIX=IND_VVIX, VIX=IND_VIX.  Timing:
``signal_lag_days=1`` (legacy D-1, validated on §5.2).  Dropped per Gael: the
VIX-curve-slope filter and the legacy SPX-only 0.6/0.5/0.1 machine.

CONSTRUCTION (reuses the committed pathfinder
``tests/integration/signals/test_ocean_base_leg_e2e.py`` which PROVED this
composes e2e: equity 1.0->3.12, both entry branches fire, 56 trades):

  * One series-agnostic DStat / DStat-percentile / SMA spec reused across
    VVIX/VIX/SPX by varying the operand ``input_id`` (the pathfinder proved
    multiple dstat-percentile operands off the same base series with different
    percentiles align correctly).
  * MA50(VVIX): the shipped ``sma`` indicator bound to the VVIX input with
    ``window=50`` (Gael: MovAvgState variants MA200/MA3 are the OUT-OF-SCOPE bull
    branches; MA50(VVIX) is a plain SMA here).
  * GAP-2 (OR position-doubling): entry is "(A AND B) OR C" = TWO entry blocks
    E1=[A,B], E2=[C], both weight +100 on the SAME ``leg`` input.  Two co-latched
    branches would sum to +200 %; the ``leg`` input carries
    ``position_cap=[0.0, 1.0]`` (long-or-flat) so the leg can never double.
  * RESET-ON-EXIT (working assumption, tested empirically below): E2's
    ``cross_above`` uses ``count_mode="since_reset"`` (fires on the 3rd crossing
    since the last reset).  The exits X1/X2 TARGET E2's name, and the engine
    OR's each exit-fire into E2's since_reset counter reset
    (``signal_exec.py`` ``_eval_block_activity`` ``cond_reset = reset_fire |
    chain_reset``) — so the 3rd-cross counter zeroes on every exit.

EMPIRICAL QUESTIONS (Gael) — resolved by the two tests below:
  1. RESET BEHAVIOUR of the 3rd-cross counter: reset-on-exit (counter zeroes each
     exit) vs cumulative (never resets).  ``test_ocean_reset_behaviour`` runs
     BOTH and compares monthly_corr + curve to the target.
  2. VARIANT IDENTITY: confirm the long-100 %-SPX base variant fits.
     ``test_ocean_builds_runs_and_reproduces`` gates it against the strict band;
     a poor fit would point at a bull-branch variant (MA20/200(SPX) + HVOL-out) —
     OUT OF SCOPE to build, only reported as evidence on identity.

CAPABILITY GAP FLAGGED (do NOT hack — needs Gael / PR #92 authorisation):
  For a plain-CNF ``cross_above`` block with ``count_mode="since_reset"``, the
  engine couples the counter reset to the exit-fire (chain_reset) whenever an
  exit TARGETS the block, AND ``target_entry_block_names`` is the SAME channel
  that clears the entry's latch.  So a truly CUMULATIVE counter cannot coexist
  with an exitable entry: un-targeting E2 to keep its counter cumulative also
  removes the exits' ability to clear E2's latch, so the leg gets STUCK LONG
  after the first 3rd-cross (~= buy-and-hold SPX).  ``reset_on_actual_exit`` does
  NOT decouple them here — it is a documented no-op on a non-THEN-chain CNF block
  (``signal_exec.py`` "reset_on_actual_exit on a plain CNF entry is a no-op").
  Cleanly separating the two hypotheses would need a production flag to decouple
  the since_reset counter reset from the exit chain_reset.  The cumulative
  variant here is therefore the ONLY buildable one (un-targeted E2); its
  stuck-long side-effect is itself the evidence that reset-on-exit is the correct
  reading.

Run: ``uv run pytest
  tests/integration/strategy_repro/test_val_wave_ocean_spx.py
  --run-integration -s``
"""

from __future__ import annotations

import asyncio
import csv as _csv
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from psycopg import OperationalError

from tcg.core.app import create_app

from strategy_repro.harness import (
    DEFAULT_BAND,
    REPRO_VISIBLE_CATEGORY,
    MonthlyGrid,
    check_band,
    compare,
    durable_persist_and_run_signal,
    format_side_by_side,
    parse_target_section,
    rebase_to_100,
)

pytestmark = pytest.mark.integration

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

_COLL, _SPX, _VIX, _VVIX = "INDEX", "IND_SP_500", "IND_VIX", "IND_VVIX"
_TARGET_SECTION = "Ocean"
# DURABLE, UI-visible reproduction entity (Signals page).  Stable id == name.
REPRO_ENTITY_OCEAN_SPX = "Reproduction_OceanVVIXthird50_spx"

_E1_NAME = "vvix_hits95_desc75_and_above_ma50"
_E2_NAME = "spx_dstat_3rd_cross"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULTS_DIR = _REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)
_OUTPUT_DIR = _REPO_ROOT.parent / "workspace" / "tasks" / "strategy-repro-impl" / "output"


def _extract_code(name: str) -> str:
    content = (_DEFAULTS_DIR / f"{name}.js").read_text(encoding="utf-8")
    m = _CODE_RE.search(content)
    if m is None:
        raise AssertionError(f"no `const code = ...` literal in {name}.js")
    return m.group(1)


def _ind(indicator_id: str, input_id: str, override: dict | None = None) -> dict:
    op: dict = {"kind": "indicator", "indicator_id": indicator_id, "input_id": input_id}
    if override is not None:
        op["params_override"] = override
    return op


def _indicators() -> list[dict]:
    # One series-agnostic spec per indicator, reused across VVIX/VIX/SPX by the
    # operand's ``input_id``.  ``run_indicator`` needs the COMPLETE param set
    # (partial params are rejected) — fill from each ``def compute`` default, the
    # same as the frontend Indicators page.
    series_map = {"close": {"collection": _COLL, "instrument_id": _SPX}}
    return [
        {"id": "dstat", "name": "DStat", "code": _extract_code("dstat"),
         "params": {"ma_window": 21, "vol_window": 63}, "seriesMap": series_map},
        {"id": "dstatp", "name": "DStat Percentile",
         "code": _extract_code("dstat-percentile"),
         "params": {"ma_window": 21, "vol_window": 63, "pct_window": 1260,
                    "percentile": 95.0}, "seriesMap": series_map},
        {"id": "sma", "name": "SMA", "code": _extract_code("sma"),
         "params": {"window": 20}, "seriesMap": series_map},
    ]


def _inputs() -> list[dict]:
    return [
        # The traded leg AND the SPX-DStat operand source (reused).  Long 100 %
        # SPX; position_cap [0,1] (GAP-2: long-or-flat, no OR-doubling).
        {"id": "leg",
         # v1->v2 rebase: SPX spot leg (traded + SPX-DStat source) reads the v2
         # warehouse (IND_SP_500 fact_bar, YAHOO ^GSPC). VVIX/VIX gates stay v1
         # (v2 has no VIX/VVIX universe) — per-instrument data_source is resolved
         # by the signal fetcher's svc_for, so a mixed-source signal is supported.
         "instrument": {"type": "spot", "collection": _COLL, "instrument_id": _SPX,
                        "data_source": "v2"},
         "position_cap": [0.0, 1.0],
         # LEGACY D-1 (PR #92 ``signal_lag_days``): the regime resolved from
         # close[D] is the position HELD on D+1 — validated decisive on §5.2.
         "signal_lag_days": 1},
        {"id": "vvix",
         "instrument": {"type": "spot", "collection": _COLL, "instrument_id": _VVIX}},
        {"id": "vix",
         "instrument": {"type": "spot", "collection": _COLL, "instrument_id": _VIX}},
    ]


def _rules(*, exit_targets_e2: bool) -> dict:
    """Ocean §5.3 rules.

    ``exit_targets_e2`` selects the reset-behaviour of the 3rd-cross counter:
      * True  — exits target E2 → engine OR's the exit-fire into E2's since_reset
        counter reset → RESET-ON-EXIT (the working assumption).
      * False — exits do NOT target E2 → E2's counter is CUMULATIVE from bar 0
        (fires on the 3rd/6th/9th... crossing).  NOTE (capability gap): with E2
        un-targeted the exits can also no longer CLEAR E2's latch, so the leg
        gets STUCK LONG after the first fire — the only buildable "cumulative".
    """
    exit_targets = [_E1_NAME, _E2_NAME] if exit_targets_e2 else [_E1_NAME]
    return {
        "entries": [
            {"id": "E1", "name": _E1_NAME, "input_id": "leg", "weight": 100.0,
             "conditions": [
                 {"op": "hysteresis", "direction": "up",
                  "operand": _ind("dstat", "vvix"),
                  "enter": _ind("dstatp", "vvix", {"percentile": 95.0}),
                  "exit": _ind("dstatp", "vvix", {"percentile": 75.0})},
                 {"op": "gt",
                  "lhs": {"kind": "instrument", "input_id": "vvix", "field": "close"},
                  "rhs": _ind("sma", "vvix", {"window": 50})},
             ]},
            {"id": "E2", "name": _E2_NAME, "input_id": "leg", "weight": 100.0,
             "conditions": [
                 {"op": "cross_above", "count": 3, "count_mode": "since_reset",
                  "lhs": _ind("dstat", "leg"),
                  "rhs": _ind("dstatp", "leg", {"percentile": 10.0})},
             ]},
        ],
        "exits": [
            {"id": "X1", "input_id": "", "target_entry_block_names": exit_targets,
             "conditions": [
                 {"op": "hysteresis", "direction": "down",
                  "operand": _ind("dstat", "vix"),
                  "enter": _ind("dstatp", "vix", {"percentile": 10.0}),
                  "exit": _ind("dstatp", "vix", {"percentile": 20.0})},
             ]},
            {"id": "X2", "input_id": "", "target_entry_block_names": exit_targets,
             "conditions": [
                 {"op": "hysteresis", "direction": "up",
                  "operand": _ind("dstat", "leg"),
                  "enter": _ind("dstatp", "leg", {"percentile": 60.0}),
                  "exit": _ind("dstatp", "leg", {"percentile": 50.0})},
             ]},
        ],
        "resets": [],
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ts_to_yyyymmdd(ts_ms: list[int]) -> list[int]:
    out: list[int] = []
    for t in ts_ms:
        d = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        out.append(d.year * 10000 + d.month * 100 + d.day)
    return out


def _leg_series(result):
    ts = result["timestamps"]
    eqr = np.array([np.nan if v is None else v for v in result["equity_ratio"]], dtype=float)
    ok = np.isfinite(eqr)
    dates_all = _ts_to_yyyymmdd(ts)
    dates = [d for d, k in zip(dates_all, ok) if k]
    eq = rebase_to_100(eqr[ok])
    leg_pos = next(p for p in result["positions"] if p["input_id"] == "leg")
    pos = np.array([0.0 if v is None else v for v in leg_pos["values"]], dtype=float)[ok]
    return dates, eq, pos, leg_pos


def _given_stats_from_target(target: MonthlyGrid) -> tuple[float, float]:
    """Derive (ann_ret_pct, maxDD_pct<=0) from the target's own compounded
    month-end curve — the honest GIVEN basis when no headline stat is published.
    The maxDD is MONTHLY-granularity (the gated maxDD ratio uses monthly basis)."""
    months = target.months()
    r = np.array([target.cells[k] / 100.0 for k in months], dtype=np.float64)
    eq = np.cumprod(1.0 + r)
    ann = eq[-1] ** (12.0 / r.shape[0]) - 1.0
    dd = eq / np.maximum.accumulate(eq) - 1.0
    return ann * 100.0, float(dd.min()) * 100.0


def _dump_daily_csv(dates_yyyymmdd, equity, filename: str) -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _OUTPUT_DIR / filename
    with path.open("w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["date", "equity"])
        for d, e in zip(dates_yyyymmdd, equity):
            iso = f"{d // 10000:04d}-{(d % 10000) // 100:02d}-{d % 100:02d}"
            w.writerow([iso, f"{float(e):.6f}"])


def _window_return_sign(dates, eq, lo_yyyymm: int, hi_yyyymm: int) -> float:
    """Total %-return of the equity curve restricted to [lo, hi] calendar
    months (for big-episode spot-checks: 2008 down, 2013/2019/2020 up)."""
    w = np.array([e for d, e in zip(dates, eq) if lo_yyyymm <= d // 100 <= hi_yyyymm],
                 dtype=float)
    if w.size < 2:
        return 0.0
    return (w[-1] / w[0] - 1.0) * 100.0


# --------------------------------------------------------------------------- #
# Single module-scoped run: persist the durable reset-on-exit entity + compute
# it ONCE, plus a NON-durable cumulative run for the reset-behaviour comparison.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def ocean_runs():
    async def _run():
        import httpx

        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=1800.0
            ) as c:
                # A. RESET-ON-EXIT — the DURABLE, UI-visible reproduction entity.
                #    start=None => full available window so the 1260-bar DStat
                #    percentile warm-up completes on the latest-starting series
                #    (VVIX, 2007-01-03); passing a later start would re-anchor the
                #    fetcher window and push the VVIX percentile lines' first-valid
                #    bar out by years (pathfinder GAP-4).
                doc, result_reset = await durable_persist_and_run_signal(
                    c,
                    signal_id=REPRO_ENTITY_OCEAN_SPX,
                    name=REPRO_ENTITY_OCEAN_SPX,
                    inputs=_inputs(),
                    rules=_rules(exit_targets_e2=True),
                    indicators=_indicators(),
                    category=REPRO_VISIBLE_CATEGORY,
                    start=None,
                    end="2026-06-11",
                    description="SPEC §5.3 Ocean base leg — long 100% SPX, "
                                "reset-on-exit 3rd-cross counter, signal_lag_days=1.",
                )
                # Visible-in-list proof (the Signals page lists by category).
                lr = await c.get(
                    "/api/persistence/signals",
                    params={"category": REPRO_VISIBLE_CATEGORY},
                )
                assert lr.status_code == 200, lr.text
                listed_ids = [d["id"] for d in lr.json()]

                # B. CUMULATIVE — a NON-durable compute (exits do NOT target E2)
                #    for the reset-behaviour comparison only.  Not persisted (we
                #    keep exactly one durable Ocean entity).
                cum_payload = {
                    "spec": {"id": "ocean_cumulative_experiment",
                             "name": "ocean_cumulative_experiment",
                             "inputs": _inputs(),
                             "rules": _rules(exit_targets_e2=False)},
                    "indicators": _indicators(),
                    "start": None,
                    "end": "2026-06-11",
                }
                rc = await c.post("/api/signals/compute", json=cum_payload)
                assert rc.status_code == 200, rc.text[:2000]
                result_cumulative = rc.json()
                assert "error_type" not in result_cumulative, result_cumulative

                return doc, result_reset, result_cumulative, listed_ids

    try:
        return asyncio.run(_run())
    except (ConnectionError, OSError, TimeoutError, OperationalError) as exc:
        # pool connect / RDS unreachable => skip (PoolTimeout is an
        # OperationalError, NOT an OSError/TimeoutError — must be caught
        # explicitly or a DB-down run ERRORs instead of skipping).
        pytest.skip(f"live dwh/app-data not reachable: {exc}")


# --------------------------------------------------------------------------- #
# GREEN — the durable entity builds, runs, gates, and reproduces the target
# (VARIANT IDENTITY = long-100%-SPX base) within the strict band.
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="§5.3 Ocean OPEN (Gael 2026-08-07): the confirmed OceanVVIXthird50_spx "
    "base variant reproduces direction + ann_ret (|Δ|~1.3pp) but MISSES the 2008 "
    "crash magnitude — it goes FLAT in 2008 while the target was long -27.3% — so "
    "monthly_corr~0.65 (<0.80) and monthly maxDD ratio~0.33 (<0.70). This is a "
    "signal-VARIANT gap, NOT a data one: identical on v1 and v2 (both ^GSPC), so the "
    "v2 rebase (data_source=v2 on the SPX leg) is immaterial to the fit. The correct "
    "variant is likely an out-of-scope bull-branch (MA20/200(SPX)+HVOL-out). The "
    "durable DEV/v2 entity Reproduction_OceanVVIXthird50_spx is kept persisted; "
    "remove this xfail once the variant is resolved.",
)
def test_ocean_builds_runs_and_reproduces(ocean_runs):
    doc, result, _result_cum, listed_ids = ocean_runs

    # Year-checksum tripwire — trust the target grid before comparing.
    target, checks = parse_target_section(_TARGET_SECTION)
    assert [c for c in checks if not c.ok] == [], "target-grid year-checksum slip"
    given_ann, given_maxdd = _given_stats_from_target(target)

    # Durable-entity proof: GET round-trip + VISIBLE category + listed.
    assert doc["id"] == REPRO_ENTITY_OCEAN_SPX
    assert doc["category"] == REPRO_VISIBLE_CATEGORY and doc["category"] != "DELETED"
    assert REPRO_ENTITY_OCEAN_SPX in listed_ids, (
        f"durable entity not visible in the {REPRO_VISIBLE_CATEGORY} list: {listed_ids}"
    )

    dates, eq, pos, leg_pos = _leg_series(result)
    _dump_daily_csv(dates, eq, "daily_equity_ocean_spx.csv")

    # position_cap enforced: long-or-flat, never doubled, never short (GAP-2).
    assert pos.max() <= 1.0 + 1e-9, f"position exceeded cap: max {pos.max()}"
    assert pos.min() >= 0.0 - 1e-9, f"long-only leg went short: min {pos.min()}"
    frac_in = float((pos > 1e-9).mean())
    assert 0.0 < frac_in < 1.0, f"leg trivially flat/in ({frac_in:.3f})"

    # Both entry branches provably reachable (the VVIX-hysteresis+MA branch AND
    # the SPX 3rd-cross branch), and the leg effectively exits.
    latched = {ev["block_id"]: len(ev["latched_indices"]) for ev in result["events"]
               if ev["kind"] == "entry"}
    exit_latched = sum(len(ev["latched_indices"]) for ev in result["events"]
                       if ev["kind"] == "exit")
    assert latched.get("E1", 0) > 0, "VVIX hysteresis+MA branch never fired"
    assert latched.get("E2", 0) > 0, "SPX 3rd-cross branch never fired"
    assert exit_latched > 0, "signal never effectively exited (always-in)"
    n_trades = len(result["trades"])

    cmp = compare(dates, eq, target, section="OceanVVIXthird50_spx [persisted signal]",
                  given_ann_ret_pct=given_ann, given_maxdd_pct=given_maxdd,
                  checksums=checks)
    # SOUND maxDD basis (review nit 1): §5.3's target is a MONTHLY grid only, so
    # the gated verdict uses the MONTH-END-vs-MONTH-END maxDD ratio, NOT the
    # apples-to-oranges daily-repro-vs-monthly-target ratio.
    verdict = check_band(cmp, DEFAULT_BAND, maxdd_basis="monthly")

    # Big-episode spot-check (the target's signature years).
    ep_2008 = _window_return_sign(dates, eq, 200801, 200812)
    ep_2013 = _window_return_sign(dates, eq, 201301, 201312)
    ep_2019 = _window_return_sign(dates, eq, 201901, 201912)
    ep_2020 = _window_return_sign(dates, eq, 202001, 202012)

    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                     title="OceanVVIXthird50_spx persisted-signal vs target"))
    print(f"\n[Ocean] bars={eq.shape[0]} overlap_months={cmp.n_overlap_months} "
          f"frac_in={frac_in:.3f} trades={n_trades} "
          f"E1_latched={latched.get('E1',0)} E2_latched={latched.get('E2',0)} "
          f"exit_latched={exit_latched}")
    print(f"[Ocean] monthly_corr={cmp.monthly_corr:.4f} "
          f"equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[Ocean] repro ann_ret={cmp.repro_ann_ret_pct:.3f}% "
          f"(target-derived {given_ann:.3f}%, |Δ|={cmp.ann_ret_abs_diff_pp:.3f}pp)")
    print(f"[Ocean] repro maxDD MONTHLY={cmp.repro_maxdd_monthly_pct:.3f}% "
          f"target MONTHLY={cmp.target_maxdd_monthly_pct:.3f}% "
          f"monthly-vs-monthly ratio={cmp.maxdd_ratio_monthly} [SOUND, gated]")
    print(f"[Ocean] min_equity={cmp.repro_min_equity:.3f} ruin_ok={cmp.ruin_ok}")
    print(f"[Ocean] big episodes: 2008={ep_2008:+.1f}% (target -27.3) "
          f"2013={ep_2013:+.1f}% (target +21.1) 2019={ep_2019:+.1f}% (target +24.9) "
          f"2020={ep_2020:+.1f}% (target +20.1)")
    print("[Ocean] STRICT-BAND verdict (monthly-vs-monthly maxDD basis):")
    for line in verdict.reasons:
        print(f"  band: {line}")

    # HARD facts (data-independent).
    assert cmp.checksum_failures == []
    assert np.all(np.isfinite(eq)), "equity curve has non-finite values"
    assert cmp.ruin_ok, cmp.repro_min_equity

    # PRIMARY SHAPE GATE — a 100%-SPX-exposed leg must track the target curve.
    assert cmp.equity_log_corr >= DEFAULT_BAND.equity_corr_min, cmp.equity_log_corr

    # STRICT BAND (brief §Validation): monthly_corr>=0.80 & equity_corr>=0.90;
    # ann_ret |Δ|<=2.0pp; maxDD(monthly) ratio [0.70,1.40]; no ruin.  Sharpe
    # excluded.  GATED-LEG HONESTY (brief): Ocean trades intermittently, so if
    # this fails PURELY on monthly_corr (sparse-trading dilution) while
    # equity_corr holds and the big 2008/2013/2019/2020 episodes match, that is
    # reported honestly — the bar is NOT lowered or tuned.  ⚑ PENDING-DB: the
    # exact monthly_corr / maxDD-ratio numbers below could not be measured this
    # session (dwh RDS hard-down — egress IP outside the RDS allowlist); this
    # assertion runs the strict band unchanged the instant DB is restored.
    assert verdict.passed, verdict.reasons


# --------------------------------------------------------------------------- #
# EMPIRICAL — reset-behaviour of the 3rd-cross counter: reset-on-exit vs
# cumulative.  Runs BOTH, prints monthly_corr + curve for each, and asserts the
# reset-on-exit reading is the one that reproduces the intermittent target.
# --------------------------------------------------------------------------- #


def test_ocean_reset_behaviour(ocean_runs):
    _doc, result_reset, result_cumulative, _listed = ocean_runs
    target, checks = parse_target_section(_TARGET_SECTION)
    given_ann, given_maxdd = _given_stats_from_target(target)

    d_r, eq_r, pos_r, _ = _leg_series(result_reset)
    d_c, eq_c, pos_c, _ = _leg_series(result_cumulative)

    cmp_r = compare(d_r, eq_r, target, section="Ocean reset-on-exit",
                    given_ann_ret_pct=given_ann, given_maxdd_pct=given_maxdd)
    cmp_c = compare(d_c, eq_c, target, section="Ocean cumulative",
                    given_ann_ret_pct=given_ann, given_maxdd_pct=given_maxdd)

    frac_in_r = float((pos_r > 1e-9).mean())
    frac_in_c = float((pos_c > 1e-9).mean())

    print("\n[Ocean][reset] RESET-ON-EXIT (exits target E2):")
    print(f"  frac_in={frac_in_r:.3f} monthly_corr={cmp_r.monthly_corr:.4f} "
          f"equity_log_corr={cmp_r.equity_log_corr:.4f} "
          f"ann_ret={cmp_r.repro_ann_ret_pct:.2f}% end_eq={eq_r[-1]:.1f}")
    print("[Ocean][reset] CUMULATIVE (exits do NOT target E2 — E2 latch sticky):")
    print(f"  frac_in={frac_in_c:.3f} monthly_corr={cmp_c.monthly_corr:.4f} "
          f"equity_log_corr={cmp_c.equity_log_corr:.4f} "
          f"ann_ret={cmp_c.repro_ann_ret_pct:.2f}% end_eq={eq_c[-1]:.1f}")
    verdict = ("RESET-ON-EXIT" if cmp_r.monthly_corr >= cmp_c.monthly_corr
               else "CUMULATIVE")
    print(f"[Ocean][reset] VERDICT: {verdict} fits the target better "
          f"(monthly_corr {cmp_r.monthly_corr:.4f} reset vs {cmp_c.monthly_corr:.4f} cumul)")

    # Both constructions must produce finite, non-degenerate curves.
    assert np.all(np.isfinite(eq_r)) and np.all(np.isfinite(eq_c))

    # PREDICTED (engine-mechanics + strategy reading): the cumulative variant is
    # STUCK LONG after the first 3rd-cross (its exits cannot clear E2's latch —
    # the flagged capability gap), so it degenerates toward buy-and-hold and is
    # in-market a far larger fraction of the time than the intermittent target.
    # Reset-on-exit re-enters only on FRESH 3rd-crosses and exits properly, so it
    # tracks the intermittent target.  ⚑ PENDING-DB: assert the reset-on-exit
    # reading fits AT LEAST as well as cumulative on monthly_corr — the empirical
    # answer to Gael's reset question.
    assert cmp_r.monthly_corr >= cmp_c.monthly_corr - 1e-9, (
        f"cumulative unexpectedly beat reset-on-exit "
        f"({cmp_c.monthly_corr:.4f} > {cmp_r.monthly_corr:.4f}) — revisit the "
        f"reset assumption with Gael before trusting reset-on-exit"
    )
    # The cumulative degeneracy (stuck-long) shows up as a strictly higher
    # in-market fraction than reset-on-exit.
    assert frac_in_c >= frac_in_r - 1e-9, (frac_in_c, frac_in_r)
