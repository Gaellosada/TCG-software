# Purpose: canonical OPT_<ROOT> chain loader for Phase-2 data load.
#
# Calls lib.options.load_chain with an explicit strike band derived from
# SPOT_HINT to keep query time manageable. A 4-year SPX chain with no strike
# band scans ~250K docs and takes ~21 min; with a ±30% strike band it scans
# ~25K docs and takes <2 min.
#
# Copy to scripts/02_data.py and edit the seven edit-point constants below.

from __future__ import annotations

import json
import time
from pathlib import Path

from tcg_backtester.lib import mongo, options, validate

# ---- edit point 1: option root name ----------------------------------------
# OPT_<ROOT> collection name in MongoDB.
# SPX trades as "SP_500" in the IVOL feed; RUT as "RU_2000"; NDX as "ND_100".
ROOT = "SP_500"

# ---- edit point 2: date range (YYYYMMDD ints) ------------------------------
START = 20220103
END = 20241231

# ---- edit point 3: DTE window ----------------------------------------------
DTE_MIN = 20
DTE_MAX = 40

# ---- edit point 4: option right ("C", "P", or None for both) ---------------
RIGHT: str | None = None  # None = load both calls and puts

# ---- edit point 5: spot hint for strike-band derivation --------------------
# A representative spot price for the mid-window period. Used to derive
# strike_min / strike_max automatically. For SPX 2021-2025, ~4500 is fine.
# Derive it once from the underlying bars:
#
#   from tcg_backtester.lib import data_load
#   bars = data_load.load_index_bars_sync(mongo.sync_db(), "SPX",
#                                         start=20230101, end=20230102)
#   SPOT_HINT = float(bars.close[-1]) if len(bars.close) else 4500.0
#
SPOT_HINT = 4500.0

# ---- edit point 6: strike band (fraction of spot_hint) ---------------------
# Default ±30% around spot covers delta-targeted and moneyness strategies.
# Widen only when the strategy genuinely needs far-OTM strikes.
STRIKE_BAND = 0.30  # fraction; strike_min = SPOT_HINT*(1-band), strike_max = SPOT_HINT*(1+band)

# ---- edit point 7: expiration cycle ----------------------------------------
# "M" = monthly (third-Friday), "W" = weekly, None = any cycle.
# Narrowing to "M" gives an additional ~4-5x speedup for monthly-expiry strategies.
EXPIRATION_CYCLE: str | None = None

# ---- edit point 8: output directory ----------------------------------------
OUT_DIR = Path("data")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Derive strike band from spot hint.
strike_min = SPOT_HINT * (1.0 - STRIKE_BAND)
strike_max = SPOT_HINT * (1.0 + STRIKE_BAND)

print(
    f"[chain] loading {ROOT} {START}..{END} "
    f"dte=[{DTE_MIN},{DTE_MAX}] right={RIGHT!r} "
    f"strike=[{strike_min:.0f},{strike_max:.0f}] "
    f"cycle={EXPIRATION_CYCLE!r}"
)

db = mongo.sync_db()
t0 = time.perf_counter()
chain = options.load_chain(
    db,
    root=ROOT,
    start=START,
    end=END,
    dte_min=DTE_MIN,
    dte_max=DTE_MAX,
    right=RIGHT,
    strike_min=strike_min,
    strike_max=strike_max,
    expiration_cycle=EXPIRATION_CYCLE,
    use_aggregation=True,
)
elapsed = time.perf_counter() - t0
print(
    f"[chain] loaded in {elapsed:.2f}s: n_snapshots={len(chain.snapshots)} "
    f"n_contracts={chain.n_contracts} n_observations={chain.n_observations}"
)

# Validate — surface findings loudly.
report = validate.chain_integrity(
    chain,
    start=START,
    end=END,
    dte_min=DTE_MIN,
    dte_max=DTE_MAX,
)
print(report.summary_line())
if report.severity == "FAIL":
    print(f"[chain] !! VALIDATION FAIL: {list(report.failures)}")
elif report.severity == "WARN":
    print(f"[chain] validation WARN (proceeding): {list(report.warnings)}")
else:
    print("[chain] validation OK")

# Cache pickle.
pkl_path = OUT_DIR / f"chain_{ROOT}_{START}_{END}.pkl"
options.save_chain_pkl(chain, pkl_path)
print(f"[chain] cached -> {pkl_path}")

# Emit data_summary.json entry.
summary = {
    "root": ROOT,
    "start": START,
    "end": END,
    "dte_min": DTE_MIN,
    "dte_max": DTE_MAX,
    "right": RIGHT,
    "strike_min": strike_min,
    "strike_max": strike_max,
    "spot_hint": SPOT_HINT,
    "expiration_cycle": EXPIRATION_CYCLE,
    "use_aggregation": True,
    "n_snapshots": len(chain.snapshots),
    "n_contracts": int(chain.n_contracts),
    "n_observations": int(chain.n_observations),
    "wall_time_seconds": round(elapsed, 3),
    "cache_path": str(pkl_path),
    "validation": {
        "ok": bool(report.ok),
        "severity": report.severity,
        "failures": list(report.failures),
        "warnings": list(report.warnings),
    },
}
(OUT_DIR / "data_summary.json").write_text(json.dumps(summary, indent=2))
print(f"[chain] wrote {OUT_DIR / 'data_summary.json'}")

# Edit points:
#   1. ROOT                 — OPT_<ROOT> collection name (SP_500, RU_2000, ND_100, ...)
#   2. START / END          — date range as YYYYMMDD ints
#   3. DTE_MIN / DTE_MAX    — days-to-expiry window
#   4. RIGHT                — "C", "P", or None (both)
#   5. SPOT_HINT            — representative spot for strike-band derivation (REQUIRED for speedup)
#   6. STRIKE_BAND          — fraction of spot_hint; default 0.30 = ±30%
#   7. EXPIRATION_CYCLE     — "M", "W", or None (auto from dte window)
#   8. OUT_DIR              — cache + summary destination
