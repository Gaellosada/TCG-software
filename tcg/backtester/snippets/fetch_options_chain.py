# Purpose: canonical OPT_<ROOT> chain loader for Phase-2 data load.
#
# Translates a STRATEGY.yaml-shaped spec into `load_chain` kwargs via
# `options.chain_args_from_spec` (auto: root, dates, dte window, right,
# strike_min/max from spot_hint, expiration_cycle from leg kind,
# use_aggregation=True), loads, validates with `validate.chain_integrity`,
# caches a pickle, emits data/data_summary.json. Copy to scripts/02_data.py
# and edit SPEC + ROOT_ALIAS + SPOT_HINT.
#
# CRITICAL — focused query principle: do NOT load all strikes / all expirations.
# A 4-year SPX chain with no strike band scans ~250K docs and takes ~21 min;
# with a ±30% strike band around spot it scans ~25K docs and takes <2 min.
# `chain_args_from_spec(spec, spot_hint=...)` derives the band automatically;
# you only need to supply a representative spot price.

from __future__ import annotations

# CWD: run from inside the backtester repo (or `workspaces/<slug>/`) — `mongo.sync_db()`
# walks parents from cwd to find a `pyproject.toml` with `[tool.tcg-claude-backtester]`;
# elsewhere it falls through to localhost and hangs on connection-refused timeout.

import json
import time
from pathlib import Path

from tcg_backtester.lib import mongo, options, validate

# Edit point 1 — SPEC. Inline by default; for real workspaces uncomment:
# import yaml; SPEC = yaml.safe_load(open("STRATEGY.yaml"))
SPEC = {
    "universe": [
        {"instrument_id": "SPX", "asset_class": "INDEX", "role": "tradable"},
    ],
    "date_range": {"start": 20220103, "end": 20241231},
    "signals": {
        "type": "option_strategy",
        "legs": [
            {
                "leg_id": "short_put_30d",
                "side": "short",
                "option_type": "P",
                "contract_selector": {
                    "kind": "delta",
                    "target_delta": -0.10,
                    "tolerance": 0.05,
                },
                "expiry_selector": {"kind": "dte", "target_dte": 30, "tolerance_days": 10},
            },
        ],
    },
}

# Edit point 2 — ROOT_ALIAS: instrument_id -> OPT_<root> collection name.
# SPX -> SP_500 is the IVOL-feed convention. The lib's built-in map already
# covers SPX/RUT/NDX (pass `root_alias=None`); override here for custom roots.
ROOT_ALIAS = {"SPX": "SP_500"}

# Edit point 3 — SPOT_HINT: representative spot price for strike-band push-down.
# Required for the focused-query speedup (~10x on multi-year SPX). Derive ONCE
# per workspace from the underlying bar series; the value need only be in the
# ballpark (within ~30% of mid-window spot). For SPX 2021-2025 use ~4500.
#
# Canonical pattern — fetch a single mid-window spot bar and reuse:
#
#     from tcg_backtester.lib import data_load
#     bars = data_load.load_index_bars_sync(mongo.sync_db(), "SPX",
#                                           start=20230101, end=20230102)
#     SPOT_HINT = float(bars.close[-1]) if len(bars.close) else 4500.0
#
# Or just hardcode for stable indices:
SPOT_HINT = 4500.0  # SPX mid-window spot for 2021-2025

# Edit point 4 — EXPIRATION_CYCLE override: "M" (monthly), "W" (weekly), or
# None (any). `chain_args_from_spec` auto-derives from `expiry_selector.kind`
# (weekly -> "W", monthly -> "M", dte -> None) so you usually leave this alone.
# Set explicitly only if your dte-band strategy is known to use a single
# cycle (e.g. SPX standard third-Friday monthlies → "M") and you want the
# extra ~4-5x narrowing.
EXPIRATION_CYCLE_OVERRIDE = None  # None = honor spec-derived value

# Edit point 5 — STRIKE_MIN / STRIKE_MAX overrides. Leave None to let
# `chain_args_from_spec` derive from spot_hint + selector kind. Set explicit
# floats only when you know better than the auto-derivation (e.g. far-OTM
# tail strategies that need a custom anchor).
STRIKE_MIN_OVERRIDE: float | None = None
STRIKE_MAX_OVERRIDE: float | None = None

# Edit point 6 — OUT_DIR: cache + summary destination.
OUT_DIR = Path("data")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Translate spec -> load_chain kwargs (use_aggregation=True is hardcoded).
# `spot_hint` triggers strike-band derivation; `expiration_cycle` is auto-set
# from the leg's expiry_selector.kind.
kwargs = options.chain_args_from_spec(SPEC, root_alias=ROOT_ALIAS, spot_hint=SPOT_HINT)

# Apply optional explicit overrides.
if STRIKE_MIN_OVERRIDE is not None:
    kwargs["strike_min"] = STRIKE_MIN_OVERRIDE
if STRIKE_MAX_OVERRIDE is not None:
    kwargs["strike_max"] = STRIKE_MAX_OVERRIDE
if EXPIRATION_CYCLE_OVERRIDE is not None:
    kwargs["expiration_cycle"] = EXPIRATION_CYCLE_OVERRIDE

print(
    f"[chain] loading {kwargs['root']} {kwargs['start']}..{kwargs['end']} "
    f"dte=[{kwargs['dte_min']},{kwargs['dte_max']}] right={kwargs['right']} "
    f"strike=[{kwargs.get('strike_min')},{kwargs.get('strike_max')}] "
    f"cycle={kwargs.get('expiration_cycle')!r} "
    f"use_aggregation={kwargs['use_aggregation']}"
)

# Load — single canonical call. Never set use_aggregation by hand.
db = mongo.sync_db()
t0 = time.perf_counter()
chain = options.load_chain(db, **kwargs)
elapsed = time.perf_counter() - t0
print(
    f"[chain] loaded in {elapsed:.2f}s: n_snapshots={len(chain.snapshots)} "
    f"n_contracts={chain.n_contracts} n_observations={chain.n_observations}"
)

# Validate — surface findings loudly; do not raise (caller decides).
report = validate.chain_integrity(
    chain,
    start=kwargs["start"], end=kwargs["end"],
    dte_min=kwargs["dte_min"], dte_max=kwargs["dte_max"],
)
print(report.summary_line())
# Severity contract (pipeline/02-data.md): FAIL aborts; WARN proceeds with
# caveat; PASS silent. The validate_data snippet ABORTS on FAIL — this
# canonical chain loader keeps the print-and-continue pattern (caller decides
# whether to halt) but classifies the three states separately.
if report.severity == "FAIL":
    print(f"[chain] !! VALIDATION FAIL: {list(report.failures)}")
elif report.severity == "WARN":
    print(f"[chain] validation WARN (proceeding): {list(report.warnings)}")
else:
    print("[chain] validation OK")

# Cache pickle.
pkl_path = OUT_DIR / f"chain_{kwargs['root']}_{kwargs['start']}_{kwargs['end']}.pkl"
options.save_chain_pkl(chain, pkl_path)
print(f"[chain] cached -> {pkl_path}")

# Emit data_summary.json (canonical OPTION_CHAIN entry shape).
summary = {
    "root": kwargs["root"], "start": kwargs["start"], "end": kwargs["end"],
    "dte_min": kwargs["dte_min"], "dte_max": kwargs["dte_max"],
    "right": kwargs["right"], "use_aggregation": kwargs["use_aggregation"],
    "strike_min": kwargs.get("strike_min"),
    "strike_max": kwargs.get("strike_max"),
    "expiration_cycle": kwargs.get("expiration_cycle"),
    "spot_hint": SPOT_HINT,
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
        "checks": dict(report.checks),
    },
}
(OUT_DIR / "data_summary.json").write_text(json.dumps(summary, indent=2))
print(f"[chain] wrote {OUT_DIR / 'data_summary.json'}")

# Edit points:
#   1. SPEC                     — strategy spec dict (inline or yaml.safe_load(STRATEGY.yaml)).
#   2. ROOT_ALIAS               — instrument_id -> option-root override (SPX -> SP_500 default).
#   3. SPOT_HINT                — representative spot for strike-band derivation. REQUIRED for
#                                 the multi-year speedup; pass even an approximate value.
#   4. EXPIRATION_CYCLE_OVERRIDE— None (auto from spec) | "M" | "W". Leave None usually.
#   5. STRIKE_MIN/MAX_OVERRIDE  — None (auto from spot_hint+selector) | float. Leave None usually.
#   6. OUT_DIR                  — pickle + summary destination (default "data/").
