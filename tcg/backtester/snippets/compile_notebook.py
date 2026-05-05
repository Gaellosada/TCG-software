# Purpose: compile scripts/* into the final notebook + emit the JSON manifest.
#
# `compile_workspace` runs jupytext+nbclient over `scripts/*.py` and writes
# `results/notebook.ipynb`. `emit_manifest` then assembles the schema-compliant
# JSON manifest from the BacktestResult + MetricsSuite + plot paths.

import json
import pickle
from pathlib import Path

from tcg_backtester.lib import compile as cc
from tcg_backtester.lib.metrics import compute_metrics

WORKSPACE = Path(".").resolve()

# 1) Compile scripts into the notebook (executes cells; bails on first error).
notebook_path = cc.compile_workspace(WORKSPACE, execute=True)
print(f"notebook: {notebook_path}")

# 2) Load the BacktestResult + MetricsSuite produced by P3 / P4.
with open(WORKSPACE / "results" / "raw_result.pkl", "rb") as f:
    result = pickle.load(f)

metrics_path = WORKSPACE / "results" / "metrics.json"
if metrics_path.is_file():
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
else:
    metrics = compute_metrics(result).to_dict()

# 3) Discover plots under results/plots/*.json (P4 wrote them there).
plot_paths: dict[str, str] = {}
plots_dir = WORKSPACE / "results" / "plots"
if plots_dir.is_dir():
    for p in sorted(plots_dir.glob("*.json")):
        plot_paths[p.stem] = str(p.relative_to(WORKSPACE))

# 4) Emit the manifest. emit_manifest derives monthly/yearly returns and
#    rebalance dates from the result automatically.
manifest_path = cc.emit_manifest(WORKSPACE, result, metrics, plot_paths=plot_paths)
print(f"manifest: {manifest_path}")

# Edit points:
#   1. WORKSPACE       — usually "."
#   2. raw_result path — usually "results/raw_result.pkl"
#   3. metrics path    — usually "results/metrics.json"
#   4. plots glob      — defaults to results/plots/*.json
