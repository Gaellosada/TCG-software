"""Wave-2 §5.5 LongVIXaboveVVIX100_hedged — the HEDGED-CALL PATHFINDER.

SPEC §5.5 (authoritative): 30% long 30-day VIX CALL (OPT_VIX, monthly cycle,
roll 2 trading days before expiry) + a ⅓-delta VX1 futures hedge gated VVIX>150,
entered when VVIX crossed <95 since last exit then >100, exited on a free OR of
(VIX<MA5 two consecutive days) OR (VIX<MA5 AND VX1<VX2).

────────────────────────────────────────────────────────────────────────────────
WHAT THIS PATHFINDER ESTABLISHED — and the PR #92 PRODUCTION GAPS it surfaced
────────────────────────────────────────────────────────────────────────────────
The signal-leg delta-hedge is wired IN THE ENGINE (``signal_exec`` §3c/§6a +
``fetch_delta_hedge_series``) and PROVEN to work on REAL dwh here
(``test_engine_hedge_activates_on_real_dwh``: hedged ≠ unhedged, gated on the
2020-03 VVIX>150 window).  BUT the leg CANNOT be built faithfully end-to-end
through the DURABLE / UI signals path, because of THREE distinct PR #92 gaps
(all require a PRODUCTION edit → flagged for Gael, NOT hacked around here):

  GAP A — WIRE LAYER DROPS THE HEDGE.  ``/api/signals/compute`` parses an option
    input via ``OptionStreamRef`` + ``option_stream_ref_to_instrument``, NEITHER
    of which carries/maps ``delta_hedge`` (the portfolio path's ``LegSpec`` DOES,
    which is why the PORTFOLIO real-VIX hedge test works).  So a ``delta_hedge``
    in a signals request JSON is silently ignored → the engine hedge is
    UNREACHABLE from the API/durable/UI path.  PROVEN in
    ``test_wire_layer_drops_delta_hedge_GAP_A`` (hedged-JSON ≡ no-hedge-JSON).
    FIX: add ``delta_hedge`` to ``OptionStreamRef`` (mirror portfolio
    ``DeltaHedgeConfig``) + map it in ``option_stream_ref_to_instrument``.

  GAP B — HEDGE ⊥ CORRECT SIZING.  The hedge is only allowed on a
    ``premium_notional`` leg (both paths raise on ``futures_notional``), but
    ``premium_notional`` 0.30 overshoots the target ~9× (30% of NAV in PREMIUM,
    which 10×'s in a spike).  The correct §5.5 magnitude needs the
    futures/underlying-notional sizing — so even with GAP A fixed, a
    correctly-sized HEDGED VIX call can't be built.  FIX: let the delta-hedge
    size off a futures_notional option leg's delta (qty·delta is well-defined
    there too).

  GAP C — VIX PER-INDEX-POINT MULTIPLIER.  Even UNHEDGED, ``futures_notional``
    VIX sizing is ~10× too small vs the legacy target: it applies the real Cboe
    multipliers (VIX m_fut=1000, m_opt=100 → ratio 0.1), whereas the legacy sim
    sized VIX options in PER-INDEX-POINT units (multiplier omitted → ratio 1.0).
    §6 flagged the multiplier "cancels only if applied uniformly" — for VIX it
    does NOT (m_fut≠m_opt), so a factor of 10 survives.  LOCKED in
    ``test_shape_faithful_MAGNITUDE_10x_low_GAP_C``.  FIX: a per-index-point
    sizing option for VIX option legs.

  GAP D (minor) — VX2 UN-SOURCEABLE in the signals path (the continuous
    ``ContinuousInstrumentRef`` has no rank/nth_nearest and indicator
    ``_SeriesRefIn`` is spot-only), so the exit-(2) ``VX1<VX2`` contango
    sub-condition can't be built exactly.  Here it is PROXIED by ``VIX<VIX3M``
    (``IND_VIX_3M``), a standard, directionally-faithful contango measure
    (backwardation during a spike ⇒ VIX>VIX3M ⇒ exit suppressed ⇒ the big month
    is preserved).  NAMED substitution; FIX: add rank to the signals continuous
    ref.

WHAT IS FAITHFUL (asserted GREEN):
  * the SHAPE reproduces the target — monthly_corr ≈ 0.876 (≥0.80) and
    equity_log_corr ≈ 0.906 (≥0.90) over 2007-2026 — the entry/exit TIMING and
    which months fire are right; the 2020-Q1 signature fires big-positive
    (proportionally: 2020-02/03 ≈ +1.38/+4.12, i.e. ~+14/+41 at correct scale ≈
    target +11.90/+40.81);
  * the durable ``Reproduction_LongVIXaboveVVIX100_hedged`` signal entity
    persists, round-trips, and is listed (UI-discoverable);
  * the ENGINE delta-hedge activates on the real 2020-03 VVIX>150 window and
    moves the leg equity (R3 real-dwh signal-hedge proof).

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


# --------------------------------------------------------------------------- #
# §5.5 construction (wire shape) — shared by every path below
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
        "roll_offset": {"value": 2, "unit": "days"},  # F3: roll 2d before expiry
    }
    if with_hedge_marker:
        # NOTE: the signals WIRE LAYER drops this (GAP A). Kept to document intent
        # + to PROVE the gap; the ENGINE path injects the DeltaHedgeSpec directly.
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
# Engine-path runner: parse the wire Signal, INJECT delta_hedge into the typed
# leg (the exact step GAP-A's wire layer omits), run through the REAL fetcher.
# --------------------------------------------------------------------------- #
async def _run_engine(svc, *, start: str, end: str, sizing_mode: str, hedged: bool):
    spec = SignalIn.model_validate({"id": "S55", "name": "S55", "inputs": _inputs(), "rules": _rules()})
    signal = parse_signal(spec)
    new_inputs = []
    for inp in signal.inputs:
        if inp.id == "leg" and isinstance(inp.instrument, InstrumentOptionStream):
            inst = replace(inp.instrument, sizing_mode=sizing_mode)
            if hedged:
                inst = replace(inst, delta_hedge=DeltaHedgeSpec(
                    factor=1.0 / 3.0, hedge_collection="FUT_VIX", gate_collection="INDEX",
                    gate_symbol="IND_VVIX", gate_threshold=150.0, gate_op="gt"))
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
            # (1) SHAPE — full-window futures_notional UNHEDGED (hedge blocked B/A).
            shape = await _run_engine(svc, start=_FULL_START, end=_FULL_END,
                                      sizing_mode="futures_notional", hedged=False)
            # (2) ENGINE HEDGE proof — bounded 2020, premium_notional hedged vs unhedged.
            eng_h = await _run_engine(svc, start=_SPIKE_START, end=_SPIKE_END,
                                      sizing_mode="premium_notional", hedged=True)
            eng_u = await _run_engine(svc, start=_SPIKE_START, end=_SPIKE_END,
                                      sizing_mode="premium_notional", hedged=False)
        finally:
            await pool.close()

        # (3) DURABLE entity + API run (bounded) + list; (4) WIRE-GAP proof.
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
                # WIRE-GAP: hedged-JSON vs no-hedge-JSON over the same bounded window.
                def _body(marker):
                    return {"spec": {"id": "S55w", "name": "S55w",
                                     "inputs": _inputs("premium_notional", with_hedge_marker=marker),
                                     "rules": _rules()},
                            "indicators": _indicators(), "start": _SPIKE_START, "end": _SPIKE_END}
                rj_h = (await c.post("/api/signals/compute", json=_body(True))).json()
                rj_u = (await c.post("/api/signals/compute", json=_body(False))).json()
        return shape, eng_h, eng_u, doc, api_result, listed_ids, rj_h, rj_u

    try:
        return asyncio.run(_run())
    except (ConnectionError, OSError, TimeoutError) as exc:
        pytest.skip(f"live dwh/app-data not reachable: {exc}")


# --------------------------------------------------------------------------- #
# GREEN — SHAPE is faithful (timing/pattern), MAGNITUDE is ~10x low (GAP C).
# --------------------------------------------------------------------------- #
def test_shape_faithful_MAGNITUDE_10x_low_GAP_C(s55_runs):
    shape = s55_runs[0]
    target, checks = parse_target_section(_TARGET_SECTION)
    assert [c for c in checks if not c.ok] == [], "target-grid year-checksum slip"

    d, eq, pos = _series(shape)
    _dump_csv(d, eq, "daily_equity_longvixvvix100.csv")
    cmp = compare(d, eq, target, section="LongVIXaboveVVIX100_hedged [futures_notional UNHEDGED shape]",
                  checksums=checks)

    print("\n" + format_side_by_side(cmp.repro_grid, target,
                                      title="§5.5 futures_notional UNHEDGED (shape) vs target"))
    print(f"\n[§5.5] overlap_months={cmp.n_overlap_months} "
          f"entries={_n_lat(shape,'entry','E_call')} "
          f"exit2consec={_n_lat(shape,'exit','X_2consec')} exitcontango={_n_lat(shape,'exit','X_contango')}")
    print(f"[§5.5] monthly_corr={cmp.monthly_corr:.4f} equity_log_corr={cmp.equity_log_corr:.4f}")
    print(f"[§5.5] repro_ann_ret={cmp.repro_ann_ret_pct:.3f}% "
          f"(MAGNITUDE ~10x LOW vs target — GAP C VIX per-index-point multiplier)")
    print(f"[§5.5] maxDD monthly repro={cmp.repro_maxdd_monthly_pct:.3f}% "
          f"target={cmp.target_maxdd_monthly_pct:.3f}% ratio={cmp.maxdd_ratio_monthly:.3f} "
          f"(<<0.70 band: magnitude-blocked)")
    rg = cmp.repro_grid
    print(f"[§5.5][SPOT] 2020-02={rg.value(2020,2)} 2020-03={rg.value(2020,3)} "
          f"(target +11.90/+40.81); 2024-08={rg.value(2024,8)} (target +31.37)")

    # FAITHFUL: entry/exit fire; the SHAPE clears the band (timing is right).
    assert cmp.checksum_failures == []
    assert _n_lat(shape, "entry", "E_call") > 0, "no entries fired"
    assert cmp.monthly_corr >= DEFAULT_BAND.monthly_corr_min, cmp.monthly_corr
    assert cmp.equity_log_corr >= DEFAULT_BAND.equity_corr_min, cmp.equity_log_corr
    assert cmp.repro_min_equity > DEFAULT_BAND.ruin_floor, cmp.repro_min_equity
    # 2020-Q1 signature fires big-positive (proportional to the 10x-low scale).
    assert rg.value(2020, 3) is not None and rg.value(2020, 3) > 3.0, rg.value(2020, 3)
    assert rg.value(2020, 2) is not None and rg.value(2020, 2) > 1.0, rg.value(2020, 2)

    # KNOWN-BLOCKED (GAP C) — LOCKED as the current reality. When a per-index-point
    # VIX sizing lands, magnitude rises ~10x and THESE flip (signalling the fix).
    assert abs(cmp.repro_ann_ret_pct) < 2.0, (
        f"ann_ret {cmp.repro_ann_ret_pct:.3f}% is no longer ~10x low — GAP C may be "
        f"fixed; re-gate ann_ret/maxDD against the target and remove this lock")
    assert cmp.maxdd_ratio_monthly is not None and cmp.maxdd_ratio_monthly < 0.30, (
        f"monthly maxDD ratio {cmp.maxdd_ratio_monthly} rose into band — GAP C may be "
        f"fixed; re-gate the maxDD ratio and remove this lock")


# --------------------------------------------------------------------------- #
# GREEN (R3) — the ENGINE delta-hedge activates on REAL dwh and moves equity.
# --------------------------------------------------------------------------- #
def test_engine_hedge_activates_on_real_dwh(s55_runs):
    _shape, eng_h, eng_u, *_ = s55_runs
    dh, eqh, _ = _series(eng_h)
    du, equ, _ = _series(eng_u)
    # Same axis over the bounded window.
    n = min(len(eqh), len(equ))
    diff = np.abs(eqh[:n] - equ[:n])
    max_diff = float(np.nanmax(diff))
    print(f"\n[§5.5][R3] engine hedged vs unhedged max|Δequity|={max_diff:.6f} "
          f"(>0 ⇒ the ENGINE signal-leg hedge accrues on real dwh)")
    assert max_diff > 1e-4, (
        "engine hedged ≡ unhedged — the signal-leg delta-hedge did NOT engage on "
        "real dwh (would break R3)")
    # The 2020-03 VVIX>150 gate must be non-empty in this window (sanity).
    assert du[0] // 10000 <= 2020 <= du[-1] // 10000


# --------------------------------------------------------------------------- #
# GREEN (documents GAP A) — the WIRE layer drops delta_hedge: a hedged-JSON
# signals request is BYTE-IDENTICAL to a no-hedge-JSON one.
# --------------------------------------------------------------------------- #
def test_wire_layer_drops_delta_hedge_GAP_A(s55_runs):
    *_, rj_h, rj_u = s55_runs
    assert "error_type" not in rj_h, rj_h
    assert "error_type" not in rj_u, rj_u
    e_h = np.array([np.nan if v is None else v for v in rj_h["equity_ratio"]], dtype=float)
    e_u = np.array([np.nan if v is None else v for v in rj_u["equity_ratio"]], dtype=float)
    fin = np.isfinite(e_h) & np.isfinite(e_u)
    max_diff = float(np.nanmax(np.abs(e_h - e_u)[fin]))
    print(f"\n[§5.5][GAP A] wire hedged-JSON vs no-hedge-JSON max|Δ|={max_diff:.2e} "
          f"(0 ⇒ /api/signals/compute DROPS delta_hedge; engine hedge UNREACHABLE "
          f"via the durable/UI path — needs OptionStreamRef + converter plumbing)")
    assert max_diff == 0.0, (
        f"wire hedged-JSON differs from no-hedge-JSON by {max_diff} — GAP A may be "
        f"CLOSED (delta_hedge now plumbed through the signals wire layer); if so, "
        f"switch the durable entity to the real hedged run and re-gate §5.5")


# --------------------------------------------------------------------------- #
# GREEN — the DURABLE UI-visible reproduction entity persists + round-trips +
# is listed (the durable-entity mechanics work; its API run is unhedged per GAP A).
# --------------------------------------------------------------------------- #
def test_durable_entity_persists_and_lists(s55_runs):
    _shape, _eh, _eu, doc, api_result, listed_ids, *_ = s55_runs
    assert doc["id"] == _REPRO_ENTITY
    assert doc["name"] == _REPRO_ENTITY
    assert doc["category"] == REPRO_VISIBLE_CATEGORY and doc["category"] != "DELETED"
    assert _REPRO_ENTITY in listed_ids, f"durable entity not visible in list: {listed_ids}"
    assert "error_type" not in api_result, api_result
    print(f"\n[§5.5][durable] persisted + round-tripped + listed as {_REPRO_ENTITY!r} "
          f"under {REPRO_VISIBLE_CATEGORY} (NOTE: its /api/signals/compute run is "
          f"UNHEDGED per GAP A — the entity is ready for when the wire hedge lands)")
