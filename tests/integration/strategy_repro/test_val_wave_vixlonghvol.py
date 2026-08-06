"""Wave-2 §5.6 VixLongHVOL_hedged — the HVOL-gated HEDGED VIX-call reproduction.

SPEC §5.6 (authoritative): the SAME leg as §5.5 — 30% long 30-day VIX CALL
(OPT_VIX, monthly cycle, roll 2 trading days before expiry) + a ⅓-delta VX1 futures
hedge gated VVIX>150 — but a DIFFERENT lifecycle: entered when the SPX HVOL regime
turns ON (HV20<HV100 armed THEN HV20>HV30 AND HV30>HV100), exited on HV20<HV30.

This build depends on the SAME PR #92 production edits as §5.5 (see that module's
header): GAP A (wire carries delta_hedge), GAP B (hedge on a futures_notional leg),
GAP C (per-index-point VIX sizing).  The leg is futures_notional + per-index-point +
delta-hedged, built end-to-end through the wire/engine path.

Band: monthly_corr≥0.80, equity_log_corr≥0.90, ann_ret |Δ|≤2.0pp, monthly maxDD
ratio in [0.70,1.40], no ruin; Sharpe excluded (engine-Sharpe carve-out).

PENDING DB: run --run-integration to validate magnitude (this session's dwh was
down; the band gates are READY but UNRUN).

Run: ``uv run pytest
  tests/integration/strategy_repro/test_val_wave_vixlonghvol.py
  --run-integration -s``
"""

from __future__ import annotations

import asyncio
import csv as _csv
import re
from dataclasses import replace
from datetime import date
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
_SPX, _VVIX, _OPT = "IND_SP_500", "IND_VVIX", "OPT_VIX"
_TARGET_SECTION = "VixLongHVOL_hedged"
_BIG = 20000
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULTS_DIR = _REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)
_OUTPUT_DIR = _REPO_ROOT.parent / "workspace" / "tasks" / "strategy-repro-impl" / "output"
_REPRO_ENTITY = "Reproduction_VixLongHVOL_hedged"

_FULL_START, _FULL_END = "2007-01-01", "2026-06-11"
_SPIKE_START, _SPIKE_END = "2019-06-01", "2020-12-31"


def _extract_code(name: str) -> str:
    content = (_DEFAULTS_DIR / f"{name}.js").read_text(encoding="utf-8")
    m = _CODE_RE.search(content)
    assert m is not None, f"no code literal in {name}.js"
    return m.group(1)


def _given_stats_from_target(target: MonthlyGrid) -> tuple[float, float]:
    """(ann_ret_pct, maxDD_pct<=0) from the target's own compounded month-end
    curve (MONTHLY basis → gate with ``check_band(..., maxdd_basis='monthly')``)."""
    months = target.months()
    r = np.array([target.cells[k] / 100.0 for k in months], dtype=np.float64)
    eq = np.cumprod(1.0 + r)
    ann = eq[-1] ** (12.0 / r.shape[0]) - 1.0
    dd = eq / np.maximum.accumulate(eq) - 1.0
    return ann * 100.0, float(dd.min()) * 100.0


# --------------------------------------------------------------------------- #
# §5.6 construction — SAME faithful leg as §5.5 (VIX call, futures_notional +
# per-index-point + delta-hedged); the lifecycle is the SPX HVOL regime.
# --------------------------------------------------------------------------- #
def _leg_instrument(sizing_mode: str, with_hedge_marker: bool) -> dict:
    d = {
        "type": "option_stream",
        "collection": _OPT,
        "option_type": "C",
        "cycle": "M",
        "maturity": {"kind": "nearest_to_target", "target_days": 30},
        "selection": {"kind": "by_delta", "target": 0.5, "tolerance": 0.45},
        "stream": "close",
        "hold_between_rolls": True,
        "nav_times": 0.30,
        "sizing_mode": sizing_mode,
        "apply_contract_multiplier": False,  # GAP C: per-index-point VIX sizing
        "roll_offset": {"value": 2, "unit": "days"},
    }
    if with_hedge_marker:
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
        {"id": "spx", "instrument": {"type": "spot", "collection": _COLL_INDEX, "instrument_id": _SPX}},
    ]


def _hv(input_id: str, window: int) -> dict:
    return {"kind": "indicator", "indicator_id": "historical-vol",
            "input_id": input_id, "params_override": {"window": window}}


def _rules() -> dict:
    return {
        "entries": [{
            "id": "E_call", "name": "long_call", "input_id": "leg", "weight": 100.0,
            # HVOL-ON THEN-chain: arm HV20<HV100, fire HV20>HV30 AND HV30>HV100.
            "conditions": [
                {"op": "lt", "lhs": _hv("spx", 20), "rhs": _hv("spx", 100)},  # arm
                {"op": "gt", "lhs": _hv("spx", 20), "rhs": _hv("spx", 30)},   # fire1
                {"op": "gt", "lhs": _hv("spx", 30), "rhs": _hv("spx", 100)},  # fire2
            ],
            "links": {"1": _BIG}, "reset_on_actual_exit": True,
        }],
        "exits": [{
            "id": "X_off", "input_id": "", "target_entry_block_names": ["long_call"],
            "conditions": [{"op": "lt", "lhs": _hv("spx", 20), "rhs": _hv("spx", 30)}],
        }],
        "resets": [],
    }


def _indicators() -> list[dict]:
    return [{"id": "historical-vol", "name": "Historical Volatility",
             "code": _extract_code("historical-vol"), "params": {"window": 20},
             "seriesMap": {"close": {"collection": _COLL_INDEX, "instrument_id": _SPX}},
             "ownPanel": True}]


# --------------------------------------------------------------------------- #
# Engine-path runner (mirrors §5.5): the hedge + per-index-point flow via the wire.
# --------------------------------------------------------------------------- #
async def _run_engine(svc, *, start: str, end: str, sizing_mode: str, hedged: bool):
    spec = SignalIn.model_validate({"id": "S56", "name": "S56",
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
def s56_runs():
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
            faithful = await _run_engine(svc, start=_FULL_START, end=_FULL_END,
                                         sizing_mode="futures_notional", hedged=True)
            eng_h = await _run_engine(svc, start=_SPIKE_START, end=_SPIKE_END,
                                      sizing_mode="futures_notional", hedged=True)
            eng_u = await _run_engine(svc, start=_SPIKE_START, end=_SPIKE_END,
                                      sizing_mode="futures_notional", hedged=False)
        finally:
            await pool.close()

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
        return faithful, eng_h, eng_u, doc, api_result, listed_ids

    try:
        return asyncio.run(_run())
    except (ConnectionError, OSError, TimeoutError) as exc:
        pytest.skip(f"live dwh/app-data not reachable: {exc}")


# --------------------------------------------------------------------------- #
# BAND — the faithful hedged futures_notional per-index-point leg reproduces §5.6.
# PENDING DB: run --run-integration to validate magnitude.
# --------------------------------------------------------------------------- #
def test_reproduces_full_band(s56_runs):
    faithful = s56_runs[0]
    target, checks = parse_target_section(_TARGET_SECTION)
    assert [c for c in checks if not c.ok] == [], "target-grid year-checksum slip"
    given_ann, given_maxdd = _given_stats_from_target(target)

    d, eq, pos = _series(faithful)
    _dump_csv(d, eq, "daily_equity_vixlonghvol.csv")
    cmp = compare(d, eq, target, section="VixLongHVOL_hedged [faithful hedged]",
                  given_ann_ret_pct=given_ann, given_maxdd_pct=given_maxdd, checksums=checks)
    verdict = check_band(cmp, DEFAULT_BAND, maxdd_basis="monthly")

    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                      title="§5.6 faithful hedged futures_notional per-index-point vs target"))
    print(f"\n[§5.6] overlap_months={cmp.n_overlap_months} "
          f"entries={_n_lat(faithful,'entry','E_call')} exits={_n_lat(faithful,'exit','X_off')}")
    print(f"[§5.6] monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[§5.6] ann_ret repro={cmp.repro_ann_ret_pct:.3f}% given={given_ann:.3f}% "
          f"|Δ|={cmp.ann_ret_abs_diff_pp}")
    print(f"[§5.6] maxDD monthly ratio={cmp.maxdd_ratio_monthly}")
    for line in verdict.reasons:
        print(f"[§5.6][band] {line}")

    assert cmp.checksum_failures == []
    assert _n_lat(faithful, "entry", "E_call") > 0, "no entries fired"
    assert verdict.passed, verdict.reasons


# --------------------------------------------------------------------------- #
# The ENGINE delta-hedge activates on REAL dwh and moves equity (R3).
# --------------------------------------------------------------------------- #
def test_engine_hedge_activates_on_real_dwh(s56_runs):
    _faithful, eng_h, eng_u, *_ = s56_runs
    _dh, eqh, _ = _series(eng_h)
    du, equ, _ = _series(eng_u)
    n = min(len(eqh), len(equ))
    max_diff = float(np.nanmax(np.abs(eqh[:n] - equ[:n])))
    print(f"\n[§5.6][R3] engine hedged vs unhedged max|Δequity|={max_diff:.6f}")
    assert max_diff > 1e-4, "engine hedged ≡ unhedged — the signal-leg hedge did NOT engage"
    assert du[0] // 10000 <= 2020 <= du[-1] // 10000


# --------------------------------------------------------------------------- #
# The DURABLE UI-visible reproduction entity persists + round-trips + is listed.
# --------------------------------------------------------------------------- #
def test_durable_entity_persists_and_lists(s56_runs):
    _faithful, _eh, _eu, doc, api_result, listed_ids = s56_runs
    assert doc["id"] == _REPRO_ENTITY
    assert doc["name"] == _REPRO_ENTITY
    assert doc["category"] == REPRO_VISIBLE_CATEGORY and doc["category"] != "DELETED"
    assert _REPRO_ENTITY in listed_ids, f"durable entity not visible in list: {listed_ids}"
    assert "error_type" not in api_result, api_result
    print(f"\n[§5.6][durable] persisted + round-tripped + listed as {_REPRO_ENTITY!r} "
          f"under {REPRO_VISIBLE_CATEGORY} (HEDGED end-to-end via the wire path)")
