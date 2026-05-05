# Purpose: load a single option contract daily series.

from tcg_backtester.lib import data_load, options, mongo

ROOT = "SPX"
CONTRACT_ID = "SPX_20241220_4500_C"
START = 20240901
END = 20241220

db = mongo.sync_db()
series = data_load.load_option_contract_series_sync(
    db,
    ROOT,
    CONTRACT_ID,
    start=START,
    end=END,
)
options.save_contract_pkl(series, f"data/{CONTRACT_ID}.pkl")
print(f"{CONTRACT_ID}: {len(series.dates)} daily rows")

# Edit points:
#   1. ROOT        — e.g. "SPX"
#   2. CONTRACT_ID — full contract id as stored in Mongo (e.g. SPX_20241220_4500_C)
#   3. START / END — YYYYMMDD ints
