# Purpose: run a code-first strategy end-to-end and persist the result.
#
# Copy to scripts/03_backtest.py and run from the workspace root. The script
# imports strategy.py, calls lib.run_strategy, runs behavioural probes, and
# writes raw_result.pkl + raw_result.json.
#
# CWD must be the workspace root (the directory containing strategy.py).

from __future__ import annotations

import importlib.util
import json
import pickle
from pathlib import Path

from lib import run_strategy
from lib.validate import run_probes, first_fired

# ---- edit point 1: workspace root ------------------------------------------
WS = Path.cwd()  # run from workspace root; never hardcode an absolute path

# ---- load strategy module ---------------------------------------------------
_spec = importlib.util.spec_from_file_location("strategy", WS / "strategy.py")
strategy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(strategy)

# If requirements.txt is present, pre-flight should have already run
# `pip install -r requirements.txt` before this script is called.
# That step is handled by the pipeline's pre-flight phase; this script does
# NOT install dependencies itself.

# ---- run strategy -----------------------------------------------------------
result = run_strategy(strategy, workspace_path=WS)

# ---- behavioural probes -----------------------------------------------------
# run_probes needs the bars only for compute_signal-shape strategies.
# For run-shape strategies, pass None — the probe skips signal-specific checks.
_bars = getattr(result, "_bars", None)
report = run_probes(strategy, _bars, result, workspace_path=WS)
fired = first_fired(report)
if fired is not None:
    print(f"[probe WARN] {fired} — check strategy.py and re-run, or dismiss if intentional")
else:
    print(f"[probe] all probes PASS (severity={report.severity})")

# ---- persist ----------------------------------------------------------------
out_dir = WS / "results"
out_dir.mkdir(parents=True, exist_ok=True)

with open(out_dir / "raw_result.pkl", "wb") as f:
    pickle.dump(result, f)

with open(out_dir / "raw_result.json", "w") as f:
    json.dump(result.to_json_dict(), f)

print(
    f"backtest done: equity[-1]={result.equity[-1]:.4f} "
    f"trades={len(result.trades)} "
    f"n_bars={len(result.dates)}"
)

# Edit points:
#   1. WS          — workspace root (default: Path.cwd(); do not hardcode)
#   (everything else is driven by strategy.py's META and signal logic)
