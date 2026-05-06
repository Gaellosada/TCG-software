# Purpose: compute the diagnostics (regime_concentration, trade_skew,
# sharpe_below_benchmark, max_drawdown_exceeds, turnover_excessive) and write
# `results/diagnostics.json`. P6 reads `should_suggest` from this file.

import json
import pickle
from pathlib import Path

from tcg.backtester.lib import metrics as mm
from tcg.backtester.lib.diagnostics import compute_diagnostics

WORKSPACE = Path(".").resolve()

with open(WORKSPACE / "results" / "raw_result.pkl", "rb") as f:
    result = pickle.load(f)

metrics_suite = mm.compute_metrics(result)
# Optional: if a benchmark equity curve exists, compute benchmark metrics so
# `sharpe_below_benchmark` has a reference. Otherwise pass None.
benchmark_metrics = None
if result.benchmark_curve is not None:
    benchmark_metrics = mm.compute_metrics(
        result.benchmark_curve, dates=result.dates, trades=[], return_type="normal"
    )

diag = compute_diagnostics(result, metrics_suite, benchmark_metrics=benchmark_metrics)
out = WORKSPACE / "results" / "diagnostics.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(diag, indent=2))
print(f"diagnostics: {out}  should_suggest={diag['should_suggest']}")
