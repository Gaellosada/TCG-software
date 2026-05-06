# Purpose: compute the metrics suite + monthly/yearly aggregations from a saved result.

import json, pickle
from datetime import datetime, timezone
from tcg.backtester.lib import metrics

RESULT_PKL = "results/raw_result.pkl"
OUT_JSON = "results/metrics.json"

with open(RESULT_PKL, "rb") as f:
    result = pickle.load(f)

suite = metrics.compute_metrics(result)
monthly = metrics.aggregate_returns(result, "M")
yearly = metrics.aggregate_returns(result, "Y")

payload = {
    "as_of": datetime.now(timezone.utc).isoformat(),
    **suite.to_dict(),
    "monthly_returns": monthly,
    "yearly_returns": yearly,
}
with open(OUT_JSON, "w") as f:
    json.dump(payload, f, indent=2)
print(f"metrics: sharpe={suite.sharpe_ratio:.2f} mdd={suite.max_drawdown:.2%} trades={suite.num_trades}")

# Edit points:
#   1. RESULT_PKL  — usually "results/raw_result.pkl"
#   2. OUT_JSON    — usually "results/metrics.json"
