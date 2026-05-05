# Purpose: sanity-check a cached bar npz before backtesting (gaps, NaNs, monotonicity).

from tcg_backtester.lib import data_load, validate

NPZ_PATH = "data/SPY.npz"
EXCHANGE = "XNYS"

bars = data_load.load_npz(NPZ_PATH)
report = validate.bar_integrity(bars, exchange=EXCHANGE)
print(report.summary_line())
# Severity contract (pipeline/02-data.md): FAIL aborts, WARN proceeds with
# caveat surfaced, PASS silent. Do not treat WARN as FAIL.
if report.severity == "FAIL":
    raise SystemExit(f"data integrity failed: {report.failures}")
elif report.severity == "WARN":
    print(f"[bars] proceeding with caveats: {list(report.warnings)}")

# Edit points:
#   1. NPZ_PATH    — bar npz to validate
#   2. EXCHANGE    — calendar code (default "XNYS")
