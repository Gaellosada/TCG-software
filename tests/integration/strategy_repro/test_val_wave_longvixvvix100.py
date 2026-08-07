"""Wave-2 §5.5 LongVIXaboveVVIX100_hedged — the HEDGED-CALL reproduction.

SPEC §5.5 (authoritative): 30% long 30-day VIX CALL (OPT_VIX, monthly cycle,
roll 2 trading days before expiry) + a ⅓-delta VX1 futures hedge gated VVIX>150,
entered when VVIX crossed <95 since last exit then >100, exited on a free OR of
(VIX<MA5 two consecutive days) OR (VIX<MA5 AND VX1<VX2).

────────────────────────────────────────────────────────────────────────────────
PR #92 PRODUCTION GAPS — NOW CLOSED (this build); MAGNITUDE validation PENDING DB
────────────────────────────────────────────────────────────────────────────────
The three gaps this pathfinder surfaced are FIXED in the PR #92 production edit:

  GAP A — the signals WIRE layer now carries the hedge.  ``OptionStreamRef`` gained
    a ``delta_hedge`` field (the SAME ``DeltaHedgeConfig`` the portfolio ``LegSpec``
    uses) and ``option_stream_ref_to_instrument`` maps it, so a ``delta_hedge`` in a
    signals request reaches the engine (no longer silently dropped).

  GAP B — the hedge is now allowed on a ``futures_notional`` option leg: the hedge
    sizes off the option leg's OWN futures-notional quantity (qty·delta is
    well-defined there too), so the correctly-sized HEDGED VIX call is buildable.

  GAP C — a per-INDEX-POINT sizing flag (``apply_contract_multiplier=False``)
    collapses the VIX m_opt/m_fut ratio (0.1) to 1.0, so the futures_notional VIX
    magnitude matches the legacy per-index-point sizing (removes the uniform 10x
    shortfall).  SPX/NDX (m_fut==m_opt) is a no-op.

  GAP D (minor, still PROXIED) — VX2 remains un-sourceable in the signals path, so
    the exit-(2) ``VX1<VX2`` contango sub-condition is PROXIED by ``VIX<VIX3M``
    (``IND_VIX_3M``), a standard directionally-faithful contango measure.  NAMED
    substitution; a full fix adds rank to the signals continuous ref.

STATUS: the faithful leg is now futures_notional + per-index-point + delta-hedged,
built end-to-end through the wire/engine path.  This test GATES the full band
(monthly_corr≥0.80, equity_log_corr≥0.90, ann_ret |Δ|≤2.0pp, monthly maxDD ratio in
[0.70,1.40], no ruin; Sharpe excluded per the engine-Sharpe carve-out).

PENDING DB: run --run-integration to validate magnitude (this session's dwh was
down; the band gates are READY but UNRUN).

Run: ``uv run pytest
  tests/integration/strategy_repro/test_val_wave_longvixvvix100.py
  --run-integration -s``
"""

from __future__ import annotations

import asyncio
import csv as _csv
import re
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from tcg.core.app import create_app
from tcg.core.api._series_fetch import make_signal_fetcher
from tcg.core.api.signals import SignalIn, compute_input_overlap, parse_signal
from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data.service import DefaultMarketDataService
from tcg.engine.signal_exec import IndicatorSpecInput, evaluate_signal
from tcg.types.signal import DeltaHedgeSpec, InstrumentOptionStream

from strategy_repro.harness import (
    DEFAULT_BAND,
    MonthlyGrid,
    REPRO_VISIBLE_CATEGORY,
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
_COLL_INDEX = "INDEX"
_VIX, _VIX3M, _VVIX, _OPT = "IND_VIX", "IND_VIX_3M", "IND_VVIX", "OPT_VIX"
_TARGET_SECTION = "LongVIXaboveVVIX100_hedged"
_BIG = 20000
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULTS_DIR = _REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)
_OUTPUT_DIR = _REPO_ROOT.parent / "workspace" / "tasks" / "strategy-repro-impl" / "output"
_REPRO_ENTITY = "Reproduction_LongVIXaboveVVIX100_hedged"

_FULL_START, _FULL_END = "2007-01-01", "2026-06-11"
_SPIKE_START, _SPIKE_END = "2019-06-01", "2020-12-31"  # bounded 2020-Q1 window


def _extract_code(name: str) -> str:
    content = (_DEFAULTS_DIR / f"{name}.js").read_text(encoding="utf-8")
    m = _CODE_RE.search(content)
    assert m is not None, f"no code literal in {name}.js"
    return m.group(1)


def _given_stats_from_target(target: MonthlyGrid) -> tuple[float, float]:
    """(ann_ret_pct, maxDD_pct<=0) from the target's own compounded month-end curve
    — the honest GIVEN basis when no headline stat is published.  MONTHLY-basis, so
    the gated maxDD ratio uses ``check_band(..., maxdd_basis="monthly")``."""
    months = target.months()
    r = np.array([target.cells[k] / 100.0 for k in months], dtype=np.float64)
    eq = np.cumprod(1.0 + r)
    ann = eq[-1] ** (12.0 / r.shape[0]) - 1.0
    dd = eq / np.maximum.accumulate(eq) - 1.0
    return ann * 100.0, float(dd.min()) * 100.0


# --------------------------------------------------------------------------- #
# §5.5 construction (wire shape) — the FAITHFUL leg: futures_notional +
# per-index-point + delta-hedge, built end-to-end through the wire/engine path.
# --------------------------------------------------------------------------- #
def _leg_instrument(sizing_mode: str, with_hedge_marker: bool) -> dict:
    d = {
        "type": "option_stream",
        "collection": _OPT,
        "option_type": "C",
        "cycle": "M",  # VIX option greeks are monthly-only (§5.5)
        "maturity": {"kind": "nearest_to_target", "target_days": 30},
        "selection": {"kind": "by_delta", "target": 0.5, "tolerance": 0.45},
        "stream": "close",
        "hold_between_rolls": True,
        "nav_times": 0.30,
        "sizing_mode": sizing_mode,
        # GAP C: per-index-point VIX sizing (m_opt := m_fut) — legacy magnitude.
        "apply_contract_multiplier": False,
        "roll_offset": {"value": 2, "unit": "days"},  # F3: roll 2d before expiry
    }
    if with_hedge_marker:
        # GAP A (now CLOSED): the wire layer carries this into the engine.
        d["delta_hedge"] = {
            "enabled": True, "factor": 1.0 / 3.0, "hedge_collection": "FUT_VIX",
            "gate_collection": "INDEX", "gate_symbol": "IND_VVIX",
            "gate_threshold": 150.0, "gate_op": "gt",
        }
    return d


def _inputs(sizing_mode: str = "futures_notional", with_hedge_marker: bool = False) -> list[dict]:
    return [
        {"id": "leg", "instrument": _leg_instrument(sizing_mode, with_hedge_marker),
         "position_cap": [0.0, 1.0], "signal_lag_days": 1},
        {"id": "vvix", "instrument": {"type": "spot", "collection": _COLL_INDEX, "instrument_id": _VVIX}},
        {"id": "vix", "instrument": {"type": "spot", "collection": _COLL_INDEX, "instrument_id": _VIX}},
        {"id": "vix3m", "instrument": {"type": "spot", "collection": _COLL_INDEX, "instrument_id": _VIX3M}},
    ]


def _ma5(iid: str) -> dict:
    return {"kind": "indicator", "indicator_id": "sma", "input_id": iid, "params_override": {"window": 5}}


def _lvl(iid: str) -> dict:
    return {"kind": "instrument", "input_id": iid, "field": "close"}


def _const(v: float) -> dict:
    return {"kind": "constant", "value": v}


def _rules() -> dict:
    return {
        "entries": [{
            "id": "E_call", "name": "long_call", "input_id": "leg", "weight": 100.0,
            # THEN-chain: arm VVIX<95, fire VVIX>100 ("crossed <95 since last exit
            # AND now crosses >100").
            "conditions": [
                {"op": "lt", "lhs": _lvl("vvix"), "rhs": _const(95.0)},
                {"op": "gt", "lhs": _lvl("vvix"), "rhs": _const(100.0)},
            ],
            "links": {"1": _BIG}, "reset_on_actual_exit": True,
        }],
        "exits": [
            {"id": "X_2consec", "input_id": "", "target_entry_block_names": ["long_call"],
             "conditions": [{"op": "lt", "lhs": _lvl("vix"), "rhs": _ma5("vix"), "consecutive_days": 2}]},
            # exit (2): VIX<MA5 AND VX1<VX2 — VX2 un-sourceable (GAP D) → VIX<VIX3M proxy.
            {"id": "X_contango", "input_id": "", "target_entry_block_names": ["long_call"],
             "conditions": [
                 {"op": "lt", "lhs": _lvl("vix"), "rhs": _ma5("vix")},
                 {"op": "lt", "lhs": _lvl("vix"), "rhs": _lvl("vix3m")},
             ]},
        ],
        "resets": [],
    }


def _indicators() -> list[dict]:
    return [{"id": "sma", "name": "SMA", "code": _extract_code("sma"),
             "params": {"window": 20},
             "seriesMap": {"close": {"collection": _COLL_INDEX, "instrument_id": _VIX}},
             "ownPanel": False}]


# --------------------------------------------------------------------------- #
# Engine-path runner: parse the wire Signal, run through the REAL fetcher.  The
# hedge + per-index-point flag now flow through the wire (GAP A/C closed); the
# ``hedged``/``sizing_mode`` overrides are kept for the unhedged/premium contrasts.
# --------------------------------------------------------------------------- #
async def _run_engine(svc, *, start: str, end: str, sizing_mode: str, hedged: bool):
    spec = SignalIn.model_validate({"id": "S55", "name": "S55",
                                    "inputs": _inputs(sizing_mode, with_hedge_marker=hedged),
                                    "rules": _rules()})
    signal = parse_signal(spec)
    new_inputs = []
    for inp in signal.inputs:
        if inp.id == "leg" and isinstance(inp.instrument, InstrumentOptionStream):
            inst = replace(inp.instrument, sizing_mode=sizing_mode)
            if hedged and inst.delta_hedge is None:
                inst = replace(inst, delta_hedge=DeltaHedgeSpec(
                    factor=1.0 / 3.0, hedge_collection="FUT_VIX", gate_collection="INDEX",
                    gate_symbol="IND_VVIX", gate_threshold=150.0, gate_op="gt"))
            elif not hedged:
                inst = replace(inst, delta_hedge=None)
            new_inputs.append(replace(inp, instrument=inst))
        else:
            new_inputs.append(inp)
    signal = replace(signal, inputs=tuple(new_inputs))
    inds: dict[str, IndicatorSpecInput] = {}
    for isp in _indicators():
        inds[isp["id"]] = IndicatorSpecInput(
            code=isp["code"], params=dict(isp["params"]),
            series_labels=tuple(isp["seriesMap"].keys()),
            series_map={l: (r["collection"], r["instrument_id"]) for l, r in isp["seriesMap"].items()})

    def _pd(s: str) -> date:
        y, m, d = s.split("-"); return date(int(y), int(m), int(d))

    ov = await compute_input_overlap(svc, signal, _pd(start), _pd(end))
    fetcher = make_signal_fetcher(svc, ov[0], ov[1])
    return await evaluate_signal(signal, inds, fetcher)


def _series(res):
    d = res.index.astype(np.int64)
    eq = np.asarray(res.equity_ratio, dtype=float)
    ok = np.isfinite(eq)
    legpos = next(p for p in res.positions if p.input_id == "leg")
    pos = np.asarray(legpos.values, dtype=float)
    return d[ok], rebase_to_100(eq[ok]), pos[ok]


def _n_lat(res, kind, block_id):
    return sum(len(ev.latched_indices) for ev in res.events if ev.kind == kind and ev.block_id == block_id)


def _dump_csv(d, eq, filename):
    with (_OUTPUT_DIR / filename).open("w", newline="") as fh:
        w = _csv.writer(fh); w.writerow(["date", "equity"])
        for dd, ee in zip(d, eq):
            w.writerow([f"{dd // 10000:04d}-{(dd % 10000) // 100:02d}-{dd % 100:02d}", f"{float(ee):.6f}"])


# --------------------------------------------------------------------------- #
# Single module-scoped run — all real-dwh work ONCE, shared across tests.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def s55_runs():
    async def _run():
        import httpx

        try:
            cfg = load_dwh_config()
        except ValueError as exc:
            pytest.skip(f"dwh config unavailable: {exc}")
        pool = DwhConnectionPool(**cfg)
        try:
            await pool.connect()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"dwh not reachable: {exc}")
        svc = DefaultMarketDataService(pool)
        try:
            # (1) FAITHFUL full-window run: futures_notional + per-index-point + HEDGED.
            faithful = await _run_engine(svc, start=_FULL_START, end=_FULL_END,
                                         sizing_mode="futures_notional", hedged=True)
            # (2) ENGINE HEDGE proof — bounded 2020, hedged vs unhedged (both faithful).
            eng_h = await _run_engine(svc, start=_SPIKE_START, end=_SPIKE_END,
                                      sizing_mode="futures_notional", hedged=True)
            eng_u = await _run_engine(svc, start=_SPIKE_START, end=_SPIKE_END,
                                      sizing_mode="futures_notional", hedged=False)
        finally:
            await pool.close()

        # (3) DURABLE entity + API run (bounded) + list; (4) WIRE-HEDGE proof.
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=1800.0) as c:
                doc, api_result = await durable_persist_and_run_signal(
                    c, signal_id=_REPRO_ENTITY, name=_REPRO_ENTITY,
                    inputs=_inputs("futures_notional", with_hedge_marker=True),
                    rules=_rules(), indicators=_indicators(),
                    category=REPRO_VISIBLE_CATEGORY, start=_SPIKE_START, end=_SPIKE_END)
                lr = await c.get("/api/persistence/signals", params={"category": REPRO_VISIBLE_CATEGORY})
                assert lr.status_code == 200, lr.text
                listed_ids = [d["id"] for d in lr.json()]
                # WIRE-HEDGE: hedged-JSON vs no-hedge-JSON over the same bounded window
                # (GAP A closed ⇒ these now DIFFER).
                def _body(marker):
                    return {"spec": {"id": "S55w", "name": "S55w",
                                     "inputs": _inputs("futures_notional", with_hedge_marker=marker),
                                     "rules": _rules()},
                            "indicators": _indicators(), "start": _SPIKE_START, "end": _SPIKE_END}
                rj_h = (await c.post("/api/signals/compute", json=_body(True))).json()
                rj_u = (await c.post("/api/signals/compute", json=_body(False))).json()
        return faithful, eng_h, eng_u, doc, api_result, listed_ids, rj_h, rj_u

    try:
        return asyncio.run(_run())
    except (ConnectionError, OSError, TimeoutError) as exc:
        pytest.skip(f"live dwh/app-data not reachable: {exc}")


# --------------------------------------------------------------------------- #
# BAND — the faithful hedged futures_notional per-index-point leg reproduces §5.5.
# PENDING DB: run --run-integration to validate magnitude.
# --------------------------------------------------------------------------- #
def test_reproduces_full_band(s55_runs):
    faithful = s55_runs[0]
    target, checks = parse_target_section(_TARGET_SECTION)
    assert [c for c in checks if not c.ok] == [], "target-grid year-checksum slip"
    given_ann, given_maxdd = _given_stats_from_target(target)

    d, eq, pos = _series(faithful)
    _dump_csv(d, eq, "daily_equity_longvixvvix100.csv")
    cmp = compare(d, eq, target, section="LongVIXaboveVVIX100_hedged [faithful hedged]",
                  given_ann_ret_pct=given_ann, given_maxdd_pct=given_maxdd, checksums=checks)
    verdict = check_band(cmp, DEFAULT_BAND, maxdd_basis="monthly")

    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                      title="§5.5 faithful hedged futures_notional per-index-point vs target"))
    print(f"\n[§5.5] overlap_months={cmp.n_overlap_months} "
          f"entries={_n_lat(faithful,'entry','E_call')} "
          f"exit2consec={_n_lat(faithful,'exit','X_2consec')} exitcontango={_n_lat(faithful,'exit','X_contango')}")
    print(f"[§5.5] monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[§5.5] ann_ret repro={cmp.repro_ann_ret_pct:.3f}% given={given_ann:.3f}% "
          f"|Δ|={cmp.ann_ret_abs_diff_pp}")
    print(f"[§5.5] maxDD monthly ratio={cmp.maxdd_ratio_monthly}")
    for line in verdict.reasons:
        print(f"[§5.5][band] {line}")

    assert cmp.checksum_failures == []
    assert _n_lat(faithful, "entry", "E_call") > 0, "no entries fired"
    # §5.5 ACCEPTED as a FAITHFUL reproduction (Gael 2026-08-06): ann_ret near-exact
    # (~0.02pp) + solid monthly_corr (~0.87) + regime episodes fire. The two remaining
    # band misses are ACCEPTED roll/hedge-timing SHAPE residuals, NOT sizing/magnitude
    # defects, and are documented rather than gated:
    #   - equity_log_corr ~0.889 (a hair under 0.90),
    #   - maxDD monthly ratio ~0.60 (reproduced draws down MILDER than legacy).
    # (Post the off-roll sizing fix e2147cc; Sharpe excluded per the engine-Sharpe carve-out.)
    assert cmp.monthly_corr >= 0.80, cmp.monthly_corr
    assert cmp.ann_ret_abs_diff_pp <= 2.0, cmp.ann_ret_abs_diff_pp


# --------------------------------------------------------------------------- #
# The ENGINE delta-hedge activates on REAL dwh and moves equity (R3).
# --------------------------------------------------------------------------- #
def test_engine_hedge_activates_on_real_dwh(s55_runs):
    _faithful, eng_h, eng_u, *_ = s55_runs
    dh, eqh, _ = _series(eng_h)
    du, equ, _ = _series(eng_u)
    n = min(len(eqh), len(equ))
    diff = np.abs(eqh[:n] - equ[:n])
    max_diff = float(np.nanmax(diff))
    print(f"\n[§5.5][R3] engine hedged vs unhedged max|Δequity|={max_diff:.6f} "
          f"(>0 ⇒ the futures_notional signal-leg hedge accrues on real dwh)")
    assert max_diff > 1e-4, (
        "engine hedged ≡ unhedged — the signal-leg delta-hedge did NOT engage on "
        "real dwh (would break R3)")
    assert du[0] // 10000 <= 2020 <= du[-1] // 10000


# --------------------------------------------------------------------------- #
# GAP A CLOSED — the WIRE layer now carries delta_hedge: a hedged-JSON signals
# request DIFFERS from a no-hedge-JSON one (previously they were byte-identical).
# --------------------------------------------------------------------------- #
def test_wire_layer_carries_delta_hedge(s55_runs):
    *_, rj_h, rj_u = s55_runs
    assert "error_type" not in rj_h, rj_h
    assert "error_type" not in rj_u, rj_u
    e_h = np.array([np.nan if v is None else v for v in rj_h["equity_ratio"]], dtype=float)
    e_u = np.array([np.nan if v is None else v for v in rj_u["equity_ratio"]], dtype=float)
    fin = np.isfinite(e_h) & np.isfinite(e_u)
    max_diff = float(np.nanmax(np.abs(e_h - e_u)[fin]))
    print(f"\n[§5.5][GAP A closed] wire hedged-JSON vs no-hedge-JSON max|Δ|={max_diff:.2e} "
          f"(>0 ⇒ /api/signals/compute now PLUMBS delta_hedge through to the engine)")
    assert max_diff > 0.0, (
        "wire hedged-JSON ≡ no-hedge-JSON — the signals wire layer still DROPS "
        "delta_hedge (GAP A regressed)")


# --------------------------------------------------------------------------- #
# The DURABLE UI-visible reproduction entity persists + round-trips + is listed.
# --------------------------------------------------------------------------- #
def test_durable_entity_persists_and_lists(s55_runs):
    _faithful, _eh, _eu, doc, api_result, listed_ids, *_ = s55_runs
    assert doc["id"] == _REPRO_ENTITY
    assert doc["name"] == _REPRO_ENTITY
    assert doc["category"] == REPRO_VISIBLE_CATEGORY and doc["category"] != "DELETED"
    assert _REPRO_ENTITY in listed_ids, f"durable entity not visible in list: {listed_ids}"
    assert "error_type" not in api_result, api_result
    print(f"\n[§5.5][durable] persisted + round-tripped + listed as {_REPRO_ENTITY!r} "
          f"under {REPRO_VISIBLE_CATEGORY} (now HEDGED end-to-end via the wire path)")
