"""Capability-SUFFICIENCY composition tests (strategy-repro VALIDATION worker).

Goal: prove each SPEC §5 leg CAN be built from the committed features and
COMPOSES + RUNS through the REAL API/engine on a SHORT / representative window
(a finite curve comes out; entries/exits fire where expected). This is NOT
golden reproduction — no 20-year backtests, no monthly-table match.

This file covers the FAST (index/futures/cash) legs + the KEY HVOL-regime
question. Option legs (§5.1 short puts) live in
``test_short_spx_puts_composes.py`` (kept on a short window per P-VAL-1).

Legs exercised here:
  * Leg 10  Short_VIX_Fut_3M (§5.4)  — continuous FUT_VIX, nth_nearest rank≈3, roll-early
  * Leg 13  USD_1M_rate(P)   (§5.7)  — cash_rate flat leg
  * Leg 4/12 HVOL-ON regime  (§5.2/§5.6/§4.2) — THEN-chain arm→trigger + HV compares
  * §6 F1 non-normalizing leveraged combine (two legs, gross > 100%)
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from tcg.core.app import create_app

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)


def _extract_code(name: str) -> str:
    content = (DEFAULTS_DIR / f"{name}.js").read_text(encoding="utf-8")
    m = _CODE_RE.search(content)
    if m is None:
        raise AssertionError(f"no `const code = ...` literal in {name}.js")
    return m.group(1)


_INDEX = "INDEX"
_SPX = "IND_SP_500"
_FUT_VIX = "FUT_VIX"


@pytest.fixture
async def client():
    import httpx

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=180.0
            ) as c:
                yield c
    except Exception as exc:  # noqa: BLE001 — pool connect failure => skip
        pytest.skip(f"live dwh/app-data not reachable: {exc}")


def _finite(seq) -> np.ndarray:
    return np.asarray([np.nan if v is None else v for v in seq], dtype=float)


# ---------------------------------------------------------------------------
# Leg 10 — Short_VIX_Fut_3M (§5.4): short 3-month constant-maturity VIX future.
# The "3-month constant maturity" is the NTH_NEAREST continuous strategy holding
# the rank-th nearest monthly contract (rank=3 ≈ 3 months on VIX's monthly
# cycle); early roll via roll_offset; short direction is the (negative) weight.
# ---------------------------------------------------------------------------
async def test_short_vix_fut_3m_composes(client):
    leg = {
        "type": "continuous",
        "collection": _FUT_VIX,
        "strategy": "nth_nearest",
        "rank": 3,               # 3rd-nearest monthly VIX future ≈ 3M maturity
        "roll_offset": 2,        # roll 2 days early (SPEC §5.4 uses 2d)
        "adjustment": "difference",
    }
    payload = {
        "legs": {"vix3m": leg},
        "weights": {"vix3m": -60.0},          # short 60% (SPEC §5.4 navTimes 0.10 * combine w)
        "start": "2018-01-02",
        "end": "2019-06-30",                  # ~18 months → several rolls, fast
        "use_cache": False,
        "normalize_weights": False,           # F1: preserve the signed leverage
    }
    r = await client.post("/api/portfolio/compute", json=payload)
    assert r.status_code == 200, r.text
    b = r.json()
    assert "error_type" not in b, f"compute errored: {b}"
    eq = _finite(b["portfolio_equity"])
    assert eq.shape[0] > 200, eq.shape
    assert np.all(np.isfinite(eq)), "non-finite equity"
    assert abs(eq[0] - 100.0) < 1e-6, eq[0]  # portfolio_equity is base-100
    assert eq.std() > 0.0, "flat equity — leg produced no P&L"
    print("\n=== Leg10 Short_VIX_Fut_3M (nth_nearest rank3, roll-2d, w=-60) ===")
    print(f"bars={eq.shape[0]} eq[start={eq[0]:.4f} end={eq[-1]:.4f} "
          f"min={eq.min():.4f} max={eq.max():.4f}]")


# ---------------------------------------------------------------------------
# Leg 13 — USD_1M_rate(P) (§5.7): cash_rate flat leg (feature F4).
# ---------------------------------------------------------------------------
async def test_cash_rate_leg_composes(client):
    # A cash-rate leg has no calendar of its own (F4 guard rejects a cash-ONLY
    # portfolio) — pair it with a dated companion leg (short VIX future) to
    # supply the date axis, then assert on the CASH leg's OWN per-leg equity so
    # this stays a pure-cash-accrual check (the companion's vol is excluded).
    legs = {
        "cash": {"type": "cash_rate", "cash_rate": {"kind": "flat",
                                                    "rate_pct": 1.8, "compound": True}},
        "vix3m": {"type": "continuous", "collection": _FUT_VIX, "strategy": "nth_nearest",
                  "rank": 3, "roll_offset": 2, "adjustment": "difference"},
    }
    payload = {
        "legs": legs,
        "weights": {"cash": 100.0, "vix3m": -60.0},
        "start": "2018-01-02",
        "end": "2019-06-30",
        "use_cache": False,
        "normalize_weights": False,
    }
    r = await client.post("/api/portfolio/compute", json=payload)
    assert r.status_code == 200, r.text
    b = r.json()
    assert "error_type" not in b, f"compute errored: {b}"
    # Portfolio composes.
    peq = _finite(b["portfolio_equity"])
    assert peq.shape[0] > 200 and np.all(np.isfinite(peq)), peq.shape
    # The CASH leg's OWN equity is the pure-cash accrual: monotone up, ~rate.
    ceq = _finite(b["leg_equities"]["cash"])
    ceq = ceq[np.isfinite(ceq)]
    assert ceq.shape[0] > 200, ceq.shape
    assert ceq[-1] > ceq[0], "cash leg did not accrue upward"
    dif = np.diff(ceq)
    assert (dif >= -1e-12).all(), "cash accrual went DOWN on some bar"
    ann = (ceq[-1] / ceq[0]) ** (252.0 / ceq.shape[0]) - 1.0
    print("\n=== Leg13 USD_1M_rate cash leg (flat 1.8%/yr, per-leg equity) ===")
    print(f"bars={ceq.shape[0]} cash_eq_end={ceq[-1]:.6f} implied_ann={ann*100:.3f}%")


# ---------------------------------------------------------------------------
# Legs 4 / 12 CORE — HVOL-ON regime (§4.2) expressibility.
#
# HVOL turns ON = (HV20 > HV30 > HV100) AND "HV20 crossed below HV100 since
# last exit".  Modelled with EXISTING primitives:
#   * the arm→trigger latch = a THEN-chain (``links``): group0 [HV20 < HV100]
#     (arm) THEN group1 [HV20 > HV30 AND HV30 > HV100] (fire) within a wide
#     window (arm-to-trigger gap is unbounded in practice → window = big).
#   * "since last exit" = the exit that targets this entry resets its chain.
#
# This proves the HVOL regime COMPOSES + FIRES.  Traded proxy = SPX spot
# (INDEX, fast); the regime logic is instrument-independent, so this settles
# the capability question for the option-gated legs 4/12.
# ---------------------------------------------------------------------------
def _hv_op(input_id: str, window: int) -> dict:
    return {"kind": "indicator", "indicator_id": "hv", "input_id": input_id,
            "params_override": {"window": window}}


async def test_hvol_on_regime_composes_and_fires(client):
    hv_code = _extract_code("historical-vol")
    series_map = {"close": {"collection": _INDEX, "instrument_id": _SPX}}
    indicators = [
        {"id": "hv", "name": "HV", "code": hv_code,
         "params": {"window": 20}, "seriesMap": series_map},
    ]
    BIG = 20000  # arm→trigger gap is unbounded; window just bounds the search
    spec = {
        "id": "hvol_on_probe",
        "name": "HVOL-ON regime (THEN-chain arm→trigger)",
        "inputs": [
            {"id": "leg", "instrument": {"type": "spot", "collection": _INDEX,
                                         "instrument_id": _SPX}},
        ],
        "rules": {
            "entries": [
                {
                    "id": "E_on", "name": "hvol_turns_on",
                    "input_id": "leg", "weight": 100.0,
                    # cond0: arm (HV20<HV100). cond1,cond2: fire level.
                    "conditions": [
                        {"op": "lt", "lhs": _hv_op("leg", 20), "rhs": _hv_op("leg", 100)},
                        {"op": "gt", "lhs": _hv_op("leg", 20), "rhs": _hv_op("leg", 30)},
                        {"op": "gt", "lhs": _hv_op("leg", 30), "rhs": _hv_op("leg", 100)},
                    ],
                    # THEN boundary before cond1 → group0={c0}, group1={c1,c2}.
                    "links": {"1": BIG},
                },
            ],
            "exits": [
                {
                    "id": "X_off", "input_id": "",
                    "target_entry_block_names": ["hvol_turns_on"],
                    # HVOL leaves ON when HV20 < HV30 (§4.2 ON→exit; §5.6 call exit).
                    "conditions": [
                        {"op": "lt", "lhs": _hv_op("leg", 20), "rhs": _hv_op("leg", 30)},
                    ],
                },
            ],
            "resets": [],
        },
    }
    payload = {"spec": spec, "indicators": indicators,
               "start": "2014-01-02", "end": "2021-06-30"}
    r = await client.post("/api/signals/compute", json=payload)
    assert r.status_code == 200, r.text
    b = r.json()
    assert "error_type" not in b, f"HVOL regime compute errored: {b}"

    eq = _finite(b["equity_ratio"])
    assert eq.shape[0] > 1000, eq.shape
    assert np.all(np.isfinite(eq)), "non-finite equity"

    # Entry (HVOL-ON THEN-chain) provably latched, and the exit provably fired.
    n_on = sum(len(ev["latched_indices"]) for ev in b["events"]
               if ev["kind"] == "entry" and ev["block_id"] == "E_on")
    n_off = sum(len(ev["latched_indices"]) for ev in b["events"]
                if ev["kind"] == "exit" and ev["block_id"] == "X_off")
    assert n_on > 0, "HVOL-ON THEN-chain never fired — regime NOT expressible this way"
    assert n_off > 0, "HVOL exit (HV20<HV30) never fired"

    leg_pos = next(p for p in b["positions"] if p["input_id"] == "leg")
    pos = _finite(leg_pos["values"])
    frac_in = float((np.nan_to_num(pos) > 0).mean())
    assert 0.0 < frac_in < 1.0, f"trivially flat/in ({frac_in:.3f})"
    print("\n=== Legs4/12 HVOL-ON regime (THEN-chain arm→trigger) ===")
    print(f"bars={eq.shape[0]} on_latched={n_on} off_latched={n_off} "
          f"in_frac={frac_in:.3f} eq_end={eq[-1]:.4f}")


# ---------------------------------------------------------------------------
# §6 F1 — non-normalizing leveraged combine: two legs whose gross |w| = 160%
# (> 100%). With normalize_weights=False the leverage is preserved (a
# normalized combine would rescale to Σ|w|=1).
# ---------------------------------------------------------------------------
async def test_f1_leveraged_combine_two_legs(client):
    legs = {
        "vix3m": {"type": "continuous", "collection": _FUT_VIX, "strategy": "nth_nearest",
                  "rank": 3, "roll_offset": 2, "adjustment": "difference"},
        "cash": {"type": "cash_rate", "cash_rate": {"kind": "flat", "rate_pct": 1.8}},
    }
    weights = {"vix3m": -60.0, "cash": 100.0}   # gross = 160% > 100%
    base = {"legs": legs, "weights": weights, "start": "2018-01-02",
            "end": "2019-06-30", "use_cache": False}

    r_lev = await client.post("/api/portfolio/compute",
                              json={**base, "normalize_weights": False})
    r_norm = await client.post("/api/portfolio/compute",
                               json={**base, "normalize_weights": True})
    assert r_lev.status_code == 200, r_lev.text
    assert r_norm.status_code == 200, r_norm.text
    b_lev, b_norm = r_lev.json(), r_norm.json()
    assert "error_type" not in b_lev and "error_type" not in b_norm

    eq_lev = _finite(b_lev["portfolio_equity"])
    eq_norm = _finite(b_norm["portfolio_equity"])
    assert eq_lev.shape == eq_norm.shape and eq_lev.shape[0] > 200
    # Non-normalizing must DIFFER from normalizing when gross != 100%.
    fin = np.isfinite(eq_lev) & np.isfinite(eq_norm)
    assert np.nanmax(np.abs(eq_lev - eq_norm)[fin]) > 1e-6, (
        "leveraged and normalized combine are identical — F1 not honoured"
    )
    print("\n=== §6 F1 leveraged combine (gross 160%) ===")
    print(f"end_lev={eq_lev[fin][-1]:.4f} end_norm={eq_norm[fin][-1]:.4f} "
          f"max|Δ|={np.nanmax(np.abs(eq_lev - eq_norm)[fin]):.4f}")
