# Purpose: price chart with entry/exit markers + hold-time histogram.

import pickle
from tcg_backtester.lib import plotting

RESULT_PKL = "results/raw_result.pkl"
OUT_DIR = "results/plots"

with open(RESULT_PKL, "rb") as f:
    result = pickle.load(f)

markers = plotting.trade_markers(result)
markers.write_json(f"{OUT_DIR}/trade_markers.json")

hold = plotting.hold_time_histogram(result)
hold.write_json(f"{OUT_DIR}/hold_time_hist.json")
print("saved trade_markers.json + hold_time_hist.json")

# Edit points:
#   1. RESULT_PKL  — usually "results/raw_result.pkl"
#   2. OUT_DIR     — usually "results/plots"
