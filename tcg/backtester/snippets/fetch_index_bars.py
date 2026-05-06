# Purpose: load a single INDEX series (e.g. SPX) from Mongo into an NDArray bar struct.

from tcg.backtester.lib import data_load

INSTRUMENT_ID = "SPX"
PROVIDER = "YAHOO"
START = 20200102
END = 20241231

bars = data_load.fetch_index_bars(
    INSTRUMENT_ID,
    provider=PROVIDER,
    start=START,
    end=END,
)
data_load.save_npz(bars, f"data/{INSTRUMENT_ID}.npz")
print(f"loaded {INSTRUMENT_ID}: {len(bars.dates)} bars [{bars.dates[0]}..{bars.dates[-1]}]")

# Edit points:
#   1. INSTRUMENT_ID  — the index ticker
#   2. PROVIDER       — "YAHOO" by default
#   3. START / END    — YYYYMMDD ints
