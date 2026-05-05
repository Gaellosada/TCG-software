# Purpose: RSI mean-reversion. +1 when RSI < lower, -1 when RSI > upper.

import numpy as np
from tcg_backtester.lib import data_load, signals

INPUT_NPZ = "data/SPY.npz"
WINDOW = 14
LOWER = 30.0
UPPER = 70.0
DIRECTION = "long_short"

bars = data_load.load_npz(INPUT_NPZ)
rsi = signals.rsi(bars.close, WINDOW)
raw = np.where(rsi < LOWER, 1, np.where(rsi > UPPER, -1, 0))
sig = signals.apply_direction(raw, DIRECTION)
np.savez("data/signal_rsi.npz", dates=bars.dates, signal=sig.astype(np.float64))
print(f"rsi signal: long={int((sig==1).sum())}, short={int((sig==-1).sum())}, flat={int((sig==0).sum())}")

# Edit points:
#   1. INPUT_NPZ      — bar npz path
#   2. WINDOW         — RSI lookback
#   3. LOWER / UPPER  — thresholds (default 30 / 70)
#   4. DIRECTION      — "long_only" | "short_only" | "long_short"
