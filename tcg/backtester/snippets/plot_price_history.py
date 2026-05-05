# Purpose: raw-input sanity-check chart -- close + (optional) benchmark + (conditional) volume.
#
# Renders BENEATH the §3 Data summary table. Reads the cached `.npz` produced
# by P2 (data/<id>.npz) — never re-fetches from MongoDB. Volume sub-row is
# automatic: skipped when the column is missing or all-zero (typical for
# INDEX series whose providers publish synthetic zero volume).
#
# Single-instrument: edit INPUT_NPZ + (optional) BENCHMARK_NPZ.
# Multi-instrument:  populate PANELS with one (instrument_id, npz_path) entry
#                    per series; one `price_history_<id>.json` is written per panel.
#
# Options strategies: pass the UNDERLYING spot/futures npz here, NEVER an
# option contract. The §3 chart is a sanity-check on the data feeding the
# backtest, and option chains have their own diagnostics.

from tcg_backtester.lib import data_load, plotting

# --- Single-instrument path (most common) --------------------------------
INPUT_NPZ = "data/SPX.npz"
BENCHMARK_NPZ: str | None = None  # e.g. "data/SPY.npz" or None
OUT_DIR = "results/plots"

# --- Multi-instrument path (leave empty for single-instrument) -----------
# Each entry: (instrument_id_for_filename, npz_path). Benchmark overlay is
# typically not used in multi-instrument mode — each series gets its own panel.
PANELS: list[tuple[str, str]] = []
# Example:
#   PANELS = [("SPX", "data/SPX.npz"), ("VIX", "data/VIX.npz"), ("TLT", "data/TLT.npz")]


if PANELS:
    for instrument_id, npz_path in PANELS:
        bars = data_load.load_npz(npz_path)
        fig = plotting.plot_price_history(bars)
        out_name = f"price_history_{instrument_id}.json"
        fig.write_json(f"{OUT_DIR}/{out_name}")
        print(f"saved {out_name} ({len(bars.dates)} bars)")
else:
    bars = data_load.load_npz(INPUT_NPZ)
    bench = data_load.load_npz(BENCHMARK_NPZ) if BENCHMARK_NPZ else None
    fig = plotting.plot_price_history(bars, benchmark=bench)
    fig.write_json(f"{OUT_DIR}/price_history.json")
    print(f"saved price_history.json ({len(bars.dates)} bars)")

# Edit points:
#   1. INPUT_NPZ      — the underlying series npz (NOT option contracts)
#   2. BENCHMARK_NPZ  — optional benchmark npz (or None)
#   3. PANELS         — list of (id, npz_path) for multi-instrument; one panel per
#   4. OUT_DIR        — usually "results/plots"
