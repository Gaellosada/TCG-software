# Purpose: SMA crossover signal. +1 when fast > slow, -1 when fast < slow (or 0 if long_only).

import numpy as np
from tcg_backtester.lib import data_load, signals

INPUT_NPZ = "data/SPY.npz"
FAST = 20
SLOW = 50
DIRECTION = "long_only"

bars = data_load.load_npz(INPUT_NPZ)
fast_ma = signals.sma(bars.close, FAST)
slow_ma = signals.sma(bars.close, SLOW)
raw = np.where(fast_ma > slow_ma, 1, np.where(fast_ma < slow_ma, -1, 0))
sig = signals.apply_direction(raw, DIRECTION)
np.savez("data/signal_sma.npz", dates=bars.dates, signal=sig.astype(np.float64))
print(f"sma signal: long={int((sig==1).sum())}, short={int((sig==-1).sum())}, flat={int((sig==0).sum())}")

# Edit points:
#   1. INPUT_NPZ   — path to bar npz (from fetch_*)
#   2. FAST / SLOW — windows
#   3. DIRECTION   — "long_only" | "short_only" | "long_short"
