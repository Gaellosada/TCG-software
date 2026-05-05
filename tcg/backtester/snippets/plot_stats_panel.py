# Purpose: performance stats panel (strategy vs Buy & Hold) as a Plotly table figure.
import pickle
from tcg_backtester.lib import plotting

RESULT_PKL = "results/raw_result.pkl"
OUT_DIR = "results/plots"

with open(RESULT_PKL, "rb") as f:
    result = pickle.load(f)

panel = plotting.stats_panel(result)
panel.write_json(f"{OUT_DIR}/stats_panel.json")
print("saved stats_panel.json")
# Edit points: 1. RESULT_PKL  2. OUT_DIR
