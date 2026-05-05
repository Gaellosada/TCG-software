# Purpose: equity vs benchmark line chart + drawdown shaded chart.

import pickle
from tcg_backtester.lib import plotting

RESULT_PKL = "results/raw_result.pkl"
OUT_DIR = "results/plots"

with open(RESULT_PKL, "rb") as f:
    result = pickle.load(f)

eq = plotting.equity_curve(result, show_benchmark=True)
eq.write_json(f"{OUT_DIR}/equity.json")

dd = plotting.drawdown(result)
dd.write_json(f"{OUT_DIR}/drawdown.json")
print("saved equity.json + drawdown.json")

# Edit points:
#   1. RESULT_PKL  — usually "results/raw_result.pkl"
#   2. OUT_DIR     — usually "results/plots"
