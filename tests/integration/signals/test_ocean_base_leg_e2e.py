"""PATHFINDER (Phase-5): OceanVVIXthird50 BASE leg — end-to-end composition.

Proves the newly-built Phase-1..4 capabilities COMPOSE into a real reproduced
signal leg through the REAL API + engine (no mocks, live dwh + app-data):

    create_app() lifespan  ->  POST /api/signals/compute
      -> SignalComputeRequest (Pydantic wire model)
      -> parse_signal  (tcg/core/api/signals.py)
      -> evaluate_signal  (tcg/engine/signal_exec.py)
      -> live DefaultMarketDataService reads (INDEX: IND_SP_500 / IND_VIX / IND_VVIX)

The signal is SPEC §5.3's Ocean *base* rule (authoritative):

  ENTER if [ DSTAT_VVIX completes "hits p95 then descends to p75" (hysteresis up)
             AND VVIX > MA50(VVIX) ]
        OR [ DSTAT_SPX crosses above its DSTAT_10 line for the 3rd time
             (CrossCondition cross_above, count=3, count_mode="since_reset") ]
  EXIT  if [ DSTAT_VIX completes "hits p10 then goes to p20" (hysteresis down) ]
        OR [ DSTAT_SPX completes "hits p60 then drops to p50" (hysteresis up) ]

The "3rd time" counter resets on exit — see the reset-on-exit note below.

SUBSTITUTIONS / ASSUMPTIONS (named, per the brief):
  * INSTRUMENT SUBSTITUTION (blessed by the brief): the leg is the ``_spx``
    variant — long IND_SP_500 100% (weight +100) — NOT the short-VX1 variant.
    The GOAL of this pathfinder is to prove the SIGNAL composes; the VX1
    continuous-future wiring is orthogonal and higher-risk. The signal logic
    is IDENTICAL to leg 8's base signal.
  * OR-OF-BLOCKS MODELLING: SPEC's "(A AND B) OR C" entry is expressed as two
    entry blocks E1=[A,B] and E2=[C], both bound to the leg input. To stop two
    simultaneously-latched branches from DOUBLING the position (+200%), the leg
    input carries ``position_cap=[0.0, 1.0]`` (clamp to 0..100% long). See GAP-2
    in the pathfinder report.
  * RESET-ON-EXIT (SPEC §5.3 named assumption, Gael to veto): the 3rd-crossing
    counter resets on each exit. This is achieved by the engine's always-on
    exit->entry ``chain_reset`` channel (an exit that TARGETS entry E2 zeroes
    E2's ``since_reset`` cross counter) — NOT via ``requires_reset_block_id``
    (which can only bind to a RESET block, not to an exit). See GAP-3.

This is DE-RISKING, not final validation: it asserts the leg RUNS and produces a
finite equity curve with SOME entries and SOME exits (neither always-flat nor
always-in), a trade log, and derivable monthly returns. It does NOT assert a
golden monthly-return match (that per-leg table is pending from Gael).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from tcg.core.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"

# Same extraction the default-indicator smoke test uses — guarantees this
# pathfinder runs the ACTUAL shipped indicator library code, not a transcription.
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)


def _extract_code(name: str) -> str:
    content = (DEFAULTS_DIR / f"{name}.js").read_text(encoding="utf-8")
    m = _CODE_RE.search(content)
    if m is None:
        raise AssertionError(f"no `const code = ...` literal in {name}.js")
    return m.group(1)


# INDEX-collection series (F5). SPX index symbol discovered live: IND_SP_500.
_COLL = "INDEX"
_SPX = "IND_SP_500"
_VIX = "IND_VIX"
_VVIX = "IND_VVIX"


def _ind_operand(indicator_id: str, input_id: str, override: dict | None = None) -> dict:
    op: dict = {"kind": "indicator", "indicator_id": indicator_id, "input_id": input_id}
    if override is not None:
        op["params_override"] = override
    return op


def _build_payload() -> dict:
    dstat_code = _extract_code("dstat")
    dstatp_code = _extract_code("dstat-percentile")
    sma_code = _extract_code("sma")

    # One series-agnostic spec per indicator, reused across VVIX/VIX/SPX by
    # varying the operand's ``input_id`` (the engine binds the indicator's
    # first seriesMap label to the operand input's instrument). ``seriesMap``
    # value is a placeholder — overridden per operand for the primary label.
    # NOTE: ``run_indicator`` requires the COMPLETE param set (partial params are
    # rejected — see GAP-1). The shipped default JS files carry ``params: {}``;
    # the frontend Indicators page fills them from the ``def compute`` signature
    # defaults before a signal references them. We do the same here.
    series_map = {"close": {"collection": _COLL, "instrument_id": _SPX}}
    indicators = [
        {"id": "dstat", "name": "DStat", "code": dstat_code,
         "params": {"ma_window": 21, "vol_window": 63}, "seriesMap": series_map},
        {"id": "dstatp", "name": "DStat Percentile", "code": dstatp_code,
         "params": {"ma_window": 21, "vol_window": 63, "pct_window": 1260,
                    "percentile": 95.0}, "seriesMap": series_map},
        {"id": "sma", "name": "SMA", "code": sma_code,
         "params": {"window": 20}, "seriesMap": series_map},
    ]

    spec = {
        "id": "ocean_base_spx",
        "name": "OceanVVIXthird50 base (_spx substitution)",
        "inputs": [
            # The traded leg AND the SPX-DStat operand source (reused).
            {"id": "leg",
             "instrument": {"type": "spot", "collection": _COLL, "instrument_id": _SPX},
             "position_cap": [0.0, 1.0]},
            {"id": "vvix",
             "instrument": {"type": "spot", "collection": _COLL, "instrument_id": _VVIX}},
            {"id": "vix",
             "instrument": {"type": "spot", "collection": _COLL, "instrument_id": _VIX}},
        ],
        "rules": {
            "entries": [
                {
                    "id": "E1", "name": "vvix_hits95_desc75_and_above_ma50",
                    "input_id": "leg", "weight": 100.0,
                    "conditions": [
                        {"op": "hysteresis", "direction": "up",
                         "operand": _ind_operand("dstat", "vvix"),
                         "enter": _ind_operand("dstatp", "vvix", {"percentile": 95.0}),
                         "exit": _ind_operand("dstatp", "vvix", {"percentile": 75.0})},
                        {"op": "gt",
                         "lhs": {"kind": "instrument", "input_id": "vvix", "field": "close"},
                         "rhs": _ind_operand("sma", "vvix", {"window": 50})},
                    ],
                },
                {
                    "id": "E2", "name": "spx_dstat_3rd_cross",
                    "input_id": "leg", "weight": 100.0,
                    "conditions": [
                        {"op": "cross_above", "count": 3, "count_mode": "since_reset",
                         "lhs": _ind_operand("dstat", "leg"),
                         "rhs": _ind_operand("dstatp", "leg", {"percentile": 10.0})},
                    ],
                },
            ],
            "exits": [
                {
                    "id": "X1", "input_id": "",
                    "target_entry_block_names": [
                        "vvix_hits95_desc75_and_above_ma50", "spx_dstat_3rd_cross",
                    ],
                    "conditions": [
                        {"op": "hysteresis", "direction": "down",
                         "operand": _ind_operand("dstat", "vix"),
                         "enter": _ind_operand("dstatp", "vix", {"percentile": 10.0}),
                         "exit": _ind_operand("dstatp", "vix", {"percentile": 20.0})},
                    ],
                },
                {
                    "id": "X2", "input_id": "",
                    "target_entry_block_names": [
                        "vvix_hits95_desc75_and_above_ma50", "spx_dstat_3rd_cross",
                    ],
                    "conditions": [
                        {"op": "hysteresis", "direction": "up",
                         "operand": _ind_operand("dstat", "leg"),
                         "enter": _ind_operand("dstatp", "leg", {"percentile": 60.0}),
                         "exit": _ind_operand("dstatp", "leg", {"percentile": 50.0})},
                    ],
                },
            ],
            "resets": [],
        },
    }

    return {
        "spec": spec,
        "indicators": indicators,
        # Full available window so the 126+1260-bar DStat percentile warm-up
        # completes on the LATEST-starting series (VVIX, 2007-01-03). Passing a
        # 2012 start would re-anchor the fetcher window at 2012 and push the VVIX
        # percentile lines' first-valid bar to ~2017 (GAP-4). ``end`` = snapshot.
        "start": None,
        "end": "2026-06-11",
    }


@pytest.fixture
async def client():
    """Boot the real app (live dwh + app-data via lifespan) + ASGI httpx client.

    Skips when either PostgreSQL pool is unreachable — this is a no-mock,
    live-data integration test.
    """
    import httpx

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=120.0
            ) as c:
                yield c
    except Exception as exc:  # noqa: BLE001 — pool connect failure => skip
        pytest.skip(f"live dwh/app-data not reachable: {exc}")


def _monthly_returns(ts_ms: list[int], equity: list[float]) -> dict[str, float]:
    """Month-end-to-month-end returns of the equity curve (calendar buckets)."""
    months: dict[str, float] = {}
    for t, e in zip(ts_ms, equity):
        if e is None or not np.isfinite(e):
            continue
        d = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        months[f"{d.year:04d}-{d.month:02d}"] = float(e)  # last value in month
    keys = sorted(months)
    out: dict[str, float] = {}
    prev = None
    for k in keys:
        if prev is not None and prev != 0.0:
            out[k] = months[k] / prev - 1.0
        prev = months[k]
    return out


@pytest.mark.integration
async def test_ocean_base_leg_composes_and_runs(client):
    resp = await client.post("/api/signals/compute", json=_build_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # --- 1. It RAN (no error envelope) -------------------------------------
    assert "error_type" not in body, f"compute returned an error: {body}"
    assert "equity_ratio" in body and "events" in body and "trades" in body

    ts = body["timestamps"]
    equity = body["equity_ratio"]
    assert len(ts) == len(equity) and len(equity) > 2000, len(equity)

    # --- 2. Finite equity curve, starts ~1.0, positive & finite throughout --
    eq = np.array([np.nan if v is None else v for v in equity], dtype=float)
    assert np.all(np.isfinite(eq)), "equity curve has non-finite values"
    assert abs(eq[0] - 1.0) < 1e-9, f"equity should start at 1.0, got {eq[0]}"
    assert eq[-1] > 0.0, f"leg wiped to <=0: final equity {eq[-1]}"

    # --- 3. position_cap enforced: never above 100% long, never short ------
    leg_pos = next(p for p in body["positions"] if p["input_id"] == "leg")
    pos = np.array([0.0 if v is None else v for v in leg_pos["values"]], dtype=float)
    assert pos.max() <= 1.0 + 1e-9, f"position exceeded cap: max {pos.max()}"
    assert pos.min() >= 0.0 - 1e-9, f"long-only leg went short: min {pos.min()}"

    # --- 4. Neither always-flat nor always-in: SOME entries AND SOME exits --
    entry_ids = {"E1", "E2"}
    exit_ids = {"X1", "X2"}
    n_entry_latched = sum(
        len(ev["latched_indices"]) for ev in body["events"]
        if ev["block_id"] in entry_ids and ev["kind"] == "entry"
    )
    n_exit_latched = sum(
        len(ev["latched_indices"]) for ev in body["events"]
        if ev["block_id"] in exit_ids and ev["kind"] == "exit"
    )
    assert n_entry_latched > 0, "signal never entered (always-flat)"
    assert n_exit_latched > 0, "signal never effectively exited (always-in once entered)"

    # The leg must actually be in-market on some bars and flat on others.
    frac_in = float((pos > 0).mean())
    assert 0.0 < frac_in < 1.0, f"leg is trivially flat/in ({frac_in:.3f} in-market)"

    # --- 5. Trade log present with well-formed rows ------------------------
    trades = body["trades"]
    assert len(trades) > 0, "no trades produced"
    for tr in trades:
        assert tr["input_id"] == "leg"
        assert tr["entry_block_id"] in entry_ids
        assert tr["open_bar"] >= 0
        if tr["close_bar"] is not None:
            assert tr["close_bar"] >= tr["open_bar"]

    # --- 6. Monthly returns derivable, finite, non-degenerate --------------
    mret = _monthly_returns(ts, equity)
    assert len(mret) > 24, f"too few monthly buckets: {len(mret)}"
    vals = np.array(list(mret.values()), dtype=float)
    assert np.all(np.isfinite(vals))
    assert vals.std() > 0.0, "monthly returns are all identical (degenerate)"

    # --- 7. Both entry branches provably reachable -------------------------
    # (proves the VVIX-hysteresis+MA branch AND the 3rd-cross branch both wire
    # through the real operand/indicator system, not just one of them.)
    latched_by_block = {
        ev["block_id"]: len(ev["latched_indices"])
        for ev in body["events"]
        if ev["kind"] == "entry"
    }
    assert latched_by_block.get("E1", 0) > 0, "VVIX hysteresis+MA branch never fired"
    assert latched_by_block.get("E2", 0) > 0, "SPX 3rd-cross branch never fired"

    # --- diagnostics dump (captured by -s / on failure) --------------------
    print("\n=== OceanVVIXthird50 base (_spx) — pathfinder profile ===")
    print(f"bars={len(eq)}  span_ms=[{ts[0]}..{ts[-1]}]")
    print(f"equity: start={eq[0]:.4f} end={eq[-1]:.4f} "
          f"min={eq.min():.4f} max={eq.max():.4f}")
    print(f"in-market fraction={frac_in:.3f}")
    print(f"entry-latched: E1={latched_by_block.get('E1',0)} "
          f"E2={latched_by_block.get('E2',0)}  exit-latched total={n_exit_latched}")
    print(f"trades={len(trades)}  monthly_buckets={len(mret)}  "
          f"monthly_ret std={vals.std():.5f} "
          f"min={vals.min():.4f} max={vals.max():.4f}")
