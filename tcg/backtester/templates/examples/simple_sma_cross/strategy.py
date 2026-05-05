"""SMA 50/200 crossover on SPY, 2020-2024.

Long when SMA(50) > SMA(200) (golden cross), flat otherwise. The
simplest possible strategy that exercises the full pipeline — META at
the top, ``compute_signal`` body in five lines.
"""

META = {
    "slug": "simple-sma-cross",
    "description": "Long SPY when SMA(50) > SMA(200), flat otherwise.",
    "dates": {"start": "2020-01-01", "end": "2024-12-31"},
    "universe": ["SPY"],
    "benchmark": "SPY",
    "asset_class": "ETF",
    "sizing": {"method": "fixed_fraction", "fraction": 1.0},
    "execution": {"fees_bps": 5.0, "slippage_bps": 5.0, "fill_timing": "next_open"},
}

import numpy as np
from numpy.typing import NDArray

from lib.indicators import sma


def compute_signal(bars, ctx) -> NDArray[np.float64]:
    fast = sma(bars.close, 50)
    slow = sma(bars.close, 200)
    sig = np.where(fast > slow, 1.0, 0.0)
    sig[np.isnan(fast) | np.isnan(slow)] = 0.0
    return sig.astype(np.float64)
