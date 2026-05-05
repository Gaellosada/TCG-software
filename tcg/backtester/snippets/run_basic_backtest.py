# Purpose: run a single-leg backtest end-to-end and persist the result.

import json, pickle
from tcg_backtester.lib import data_load, engine, types

BARS_NPZ = "data/SPY.npz"
SIGNAL_NPZ = "data/signal_sma.npz"
BENCH_NPZ = "data/SPY.npz"
EXECUTION = types.ExecutionConfig(fees_bps=5, slippage_bps=5, fill_timing="next_open", look_ahead_shift=1)
SIZING = types.SizingConfig(method="fixed_fraction", fraction=1.0)
OUT_DIR = "results"

bars = data_load.load_npz(BARS_NPZ)
sig = data_load.load_signal_npz(SIGNAL_NPZ)
bench = data_load.load_npz(BENCH_NPZ)

spec = types.BacktestSpec(
    bars=bars, signal=sig, benchmark=bench, execution=EXECUTION, sizing=SIZING,
)
result = engine.run_backtest(spec)

with open(f"{OUT_DIR}/raw_result.pkl", "wb") as f:
    pickle.dump(result, f)
with open(f"{OUT_DIR}/raw_result.json", "w") as f:
    json.dump(result.to_json_dict(), f)
print(f"backtest done: equity[-1]={result.equity[-1]:.4f}, trades={len(result.trades)}")

# Edit points:
#   1. BARS_NPZ        — tradable bar npz path
#   2. SIGNAL_NPZ      — signal npz path (must align to BARS_NPZ dates)
#   3. BENCH_NPZ       — benchmark bar npz path (often same as BARS_NPZ)
#   4. EXECUTION       — fees, slippage, fill_timing, look_ahead_shift
#   5. SIZING          — fixed_fraction / inverse_vol / kelly_capped
#   6. OUT_DIR         — usually "results"
