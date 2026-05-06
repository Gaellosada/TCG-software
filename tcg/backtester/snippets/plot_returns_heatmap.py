# Purpose: monthly returns heatmap + yearly bar chart.

import pickle
from tcg.backtester.lib import plotting

RESULT_PKL = "results/raw_result.pkl"
OUT_DIR = "results/plots"

with open(RESULT_PKL, "rb") as f:
    result = pickle.load(f)

heatmap = plotting.monthly_returns_heatmap(result)
heatmap.write_json(f"{OUT_DIR}/returns_heatmap.json")

log_heatmap = plotting.monthly_log_returns_heatmap(result)
log_heatmap.write_json(f"{OUT_DIR}/log_returns_heatmap.json")

yearly = plotting.yearly_returns_bars(result)
yearly.write_json(f"{OUT_DIR}/yearly_bars.json")
print("saved returns_heatmap.json + log_returns_heatmap.json + yearly_bars.json")

# Edit points:
#   1. RESULT_PKL  — usually "results/raw_result.pkl"
#   2. OUT_DIR     — usually "results/plots"
