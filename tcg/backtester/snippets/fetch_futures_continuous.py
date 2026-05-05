# Purpose: build a continuous front-month series (e.g. VIX futures) with adjustment.

from tcg_backtester.lib import data_load

ROOT = "VIX"
ADJUSTMENT = "ratio"
ROLL_OFFSET = 5
START = 20200102
END = 20241231

bars = data_load.fetch_continuous_future(
    ROOT,
    adjustment=ADJUSTMENT,
    roll_offset_days=ROLL_OFFSET,
    start=START,
    end=END,
)
data_load.save_npz(bars, f"data/FUT_{ROOT}_cont.npz")
print(f"continuous {ROOT}: {len(bars.dates)} bars, adjustment={ADJUSTMENT}")

# Edit points:
#   1. ROOT          — futures root, e.g. "VIX" -> collection FUT_VIX
#   2. ADJUSTMENT    — "none" | "ratio" | "difference"
#   3. ROLL_OFFSET   — days before expiry to roll (default 5)
#   4. START / END   — YYYYMMDD ints
