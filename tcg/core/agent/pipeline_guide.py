"""PIPELINE_GUIDE.md content written into each new agent session workspace.

Contains the full pipeline phases, probe catalog, and STRATEGY.yaml schema
so the agent can read it on first turn rather than carrying it in the system prompt.
"""

from __future__ import annotations

PIPELINE_GUIDE_MD = """\
# Pipeline Guide

Complete reference for the backtester agent. Read once on first turn.

---

## Decision Tree (Session Start)

```
STRATEGY.yaml exists?
  NO  -> Intake (parse user request into spec)
  YES -> results/manifest.json exists?
           NO  -> Resume at first phase with missing output
           YES -> User wants a variant? -> Iterate
                  Otherwise -> Answer questions about existing results
```

---

## Library API Reference

### Data Loading (tcg.backtester.lib.data_load)

All fetch functions are **synchronous** — they create their own MongoDB connection internally.

```python
from tcg.backtester.lib import data_load

# Equities / indices
bars = data_load.fetch_index_bars("IND_SP_500", start=20200101, end=20241231)
bars = data_load.fetch_etf_bars("ETF_SPY", start=20200101, end=20241231)

# Continuous futures (stitched from individual contracts)
bars = data_load.fetch_continuous_future(
    "VIX", start=20200101, end=20241231,
    cycle="HMUZ", roll_offset_days=0, adjustment="ratio"
)

# Option chains (returns OptionChainSnapshot per date)
chain = data_load.fetch_option_chain(...)  # See option snippets for usage
```

Returns: `PriceSeries(instrument_id, provider, dates, open, high, low, close, volume, meta)`
- All arrays are parallel NDArray, dates are int64 YYYYMMDD sorted ASC.
- `.slice(start, end)` returns an inclusive sub-range.

Persistence:
```python
data_load.save_bars_npz(bars, "data/SPX.npz")
bars = data_load.load_bars_npz("data/SPX.npz")  # if it exists
```

### Signals (tcg.backtester.lib.signals)

```python
from tcg.backtester.lib import signals

signals.sma(close, window)        # NaN for first window-1 bars
signals.ema(close, span)          # seeded at first finite value, no NaN warm-up
signals.rsi(close, window=14)     # values [0,100]; NaN for first window bars
signals.rolling_vol(close, window, annualise_by=252)  # annualised rolling std
signals.apply_direction(raw, "long_only"|"short_only"|"long_short")  # clip sign
signals.daily_pulse(n_bars)       # alternating +1/-1 for daily-rebalance entries
```

All return NDArray[float64] same length as input.

### Engine (tcg.backtester.lib.engine)

```python
from tcg.backtester.lib.engine import BacktestSpec, ExecutionConfig, SizingConfig, run_backtest

spec = BacktestSpec(
    bars=bars,                    # PriceSeries (REQUIRED)
    signal=sig,                   # NDArray same len as bars.dates (REQUIRED)
    benchmark=bench_bars,         # PriceSeries or None
    execution=ExecutionConfig(fees_bps=5, slippage_bps=5, fill_timing="next_open",
                              look_ahead_shift=1, risk_free_rate=0.0),
    sizing=SizingConfig(method="fixed_fraction", fraction=1.0),
    capital_base=100_000.0,
    option_legs=(),               # tuple of OptionLeg or OptionLegSpec
    secondary_signals={},         # for ExitSignal exit rules
)
result = run_backtest(spec)
```

SizingConfig.method: "fixed_fraction" | "equity_compound" | "inverse_vol" | "kelly_capped"

BacktestResult fields: dates, equity_curve, benchmark_curve, drawdown_curve, trades, positions, cash, gross_exposure, meta
- result.to_json_dict() for JSON serialization
- result.equity is alias for equity_curve

**Signal semantics**: engine opens position on 0->nonzero transition or sign change. Constant signal=1.0 opens ONE position for the entire run. Use `signals.daily_pulse(n)` for daily re-entry.

### Metrics (tcg.backtester.lib.metrics)

```python
from tcg.backtester.lib import metrics
m = metrics.compute_metrics(result)  # accepts BacktestResult directly
m = metrics.compute_metrics(result, risk_free_rate=0.04)  # override rf
print(m.to_dict())
```

MetricsSuite fields: total_return, annualized_return, sharpe_ratio, sortino_ratio, max_drawdown, calmar_ratio, cvar_5, time_underwater_days, annualized_volatility, num_trades, win_rate

### Validation (tcg.backtester.lib.validate)

```python
from tcg.backtester.lib.validate import bar_integrity, signal_integrity
report = bar_integrity(bars)           # -> IntegrityReport
report = signal_integrity(sig, bars)   # -> IntegrityReport
# report.ok (bool), report.severity ("PASS"|"WARN"|"FAIL"), report.summary_line()
```

### Diagnostics (tcg.backtester.lib.diagnostics)

```python
from tcg.backtester.lib import diagnostics
diag = diagnostics.compute_diagnostics(result, m, benchmark_metrics=bench_m)
# Returns: {"as_of": ..., "should_suggest": bool, "diagnostics": [...]}
```

### Plotting (tcg.backtester.lib.plotting)

```python
from tcg.backtester.lib import plotting
paths = plotting.write_plot_set(result, "results/plots",
    ["equity", "drawdown", "returns_heatmap", "yearly_bars", "trade_markers", "hold_time_hist", "stats_panel"])
# Writes one .json per plot_id; returns {id: Path}
```

### Compilation (tcg.backtester.lib.compile)

```python
from tcg.backtester.lib.compile import compile_workspace
nb_path = compile_workspace(Path.cwd(), execute=True)
# Runs scripts/01-04 for artifacts, compiles scripts/05+ into notebook
```

Note: the compile_notebook TOOL handles this automatically. Use the tool, not direct import.

---

## Workspace Layout

```
STRATEGY.yaml          Strategy specification (you create this)
ASSUMPTIONS.json       Running assumption log (use write_assumptions tool)
PIPELINE_GUIDE.md      This file
ITERATIONS.md          Append-only iteration log
PROBLEMS.md            Failure/issue log
snippets/              Ready-to-use code templates (read before writing scripts)
data/                  Cached .npz files + data_summary.json
scripts/               Numbered pipeline scripts (01_*, 02_*, ...)
results/               metrics.json, plots/*.json, notebook.ipynb, manifest.json
research/              Research notes
```

Script numbering convention:
- 01_fetch_data.py — load and validate data
- 02_compute_signals.py — build signal array
- 03_backtest.py — run engine, save result
- 04_analyze.py — metrics + diagnostics + plots
- 05_report.py — compile notebook + manifest (presentation layer)

---

## Workflow Summary

### Intake
Parse user prompt into STRATEGY.yaml. For unspecified fields, apply defaults (see Default Ladder below) and log via write_assumptions. Run probes mentally. If a probe fires, ask ONE focused question about the strategy. Max 3 questions; beyond that, take defaults with confidence: "low". Print a 3-line summary when done.

### Data
Fetch every series needed. Validate with bar_integrity(). Cache to data/*.npz. Write data/data_summary.json. If validation fails (report.ok == False), stop and ask.

### Backtest
Write scripts/03_backtest.py. Load cached data, build BacktestSpec, run engine, save result.to_json_dict() to results/raw_result.json.

### Analyze
Compute metrics, run diagnostics, write plots. Save results/metrics.json, results/diagnostics.json, results/plots/*.json.

### Report
Use compile_notebook tool. Write results/manifest.json.

### Iterate
On user variant request: snapshot results/ to results/iter_N/, determine minimum re-run phase (see Variant Scope table), append to ITERATIONS.md, re-run. If diagnostics.should_suggest is true, propose ONE variant as a question — do not run without confirmation.

---

## Variant Scope

| Change | Re-run from |
|--------|-------------|
| Signal parameter (SMA 20 -> 50) | Backtest |
| Sizing / execution config | Backtest |
| Date subset of cached range | Backtest |
| Date range extending beyond cache | Data |
| New instrument | Data |
| New signal type entirely | Intake |
| Reporting tweak only | Report |

---

## STRATEGY.yaml Schema

```yaml
meta:
  name: str                          # REQUIRED
  description: str
  author: "agent"
  created: "YYYY-MM-DD"

universe:
  - instrument_id: str               # REQUIRED (e.g., "IND_SP_500", "ETF_SPY")
    asset_class: INDEX | ETF | FUND | FOREX | FUTURE | OPTION
    role: tradable | benchmark | filter

date_range:
  start: int                         # REQUIRED, YYYYMMDD
  end: int                           # REQUIRED, YYYYMMDD

execution:
  fees_bps: 5
  slippage_bps: 5
  fill_timing: "next_open"
  look_ahead_shift: 1
  risk_free_rate: 0.0

signals:
  type: indicator-based | option_strategy | composite
  legs:
    - id: str
      input_id: str                  # instrument from universe
      indicator: str                 # sma | ema | rsi | rolling_vol | custom
      params: {window: 50}
      direction: long_only | short_only | long_short

sizing:
  method: fixed_fraction | equity_compound | inverse_vol | kelly_capped
  fraction: 1.0

benchmark:
  instrument_id: str
```

### Option Strategy Legs (signals.type: "option_strategy")

```yaml
signals:
  type: option_strategy
  legs:
    - leg_id: str
      side: long | short
      qty_units: 1
      option_type: C | P
      multiplier: 100
      contract_selector:
        kind: atm | delta | pct_offset | moneyness
        # atm: offset_strikes (default 0)
        # delta: target_delta, tolerance (default 0.05)
        # pct_offset: pct_offset (e.g., 0.05 = 5% OTM)
        # moneyness: moneyness (e.g., 0.95 = 5% OTM put)
      expiry_selector:
        kind: dte | weekly | monthly | fixed
        # dte: target_dte, tolerance_days (default 5)
        # weekly: DTE [3,10]
        # monthly: DTE [25,45]
        # fixed: expiration (YYYYMMDD)
      entry_signal: "primary"
      exit_rule:
        kind: hold_to_expiration | days_to_hold | exit_signal | trailing_stop
```

When user says "weekly expiries" -> kind: weekly (NOT kind: dte with target_dte=7).

---

## Default Ladder

| Field | Default | Confidence |
|-------|---------|------------|
| date_range.end | last business day | high |
| date_range.start | end - 5 years | medium |
| execution.fees_bps | 5 | high |
| execution.slippage_bps | 5 | high |
| execution.fill_timing | "next_open" | high |
| execution.look_ahead_shift | 1 | high |
| execution.risk_free_rate | 0.0 (or 0.04 if post-2022) | medium |
| sizing.method | "fixed_fraction" | medium |
| sizing.fraction | 1.0 (single instrument) | medium |
| benchmark.instrument_id | underlying spot for options; SPX otherwise | medium |

Assumption record format for write_assumptions:
```json
{"field": "execution.fees_bps", "value": 5, "source": "default", "confidence": "high",
 "rationale": "Standard default.", "group": "execution", "editable": true}
```
source: "default" | "inferred" (from context) | "user" (explicitly stated).

---

## MongoDB Collections

| Collection | Instrument IDs | Doc shape |
|-----------|---------------|-----------|
| YAHOO_INDEX | IND_SP_500, IND_VIX | {_id, eodDatas: {YAHOO: [{date, open, high, low, close, volume}]}} |
| YAHOO_ETF | ETF_SPY, ETF_QQQ | same shape as INDEX |
| FUT_VIX | root="VIX" contracts | {_id, root, contractMonth, eodDatas: {IVOLATILITY: [...]}} |
| OPT_SP_500 | SPX option chains | {_id: {internalSymbol, expirationCycle}, type, strike, eodDatas, eodGreeks} |

Always list_collections first. Then query_mongodb with limit=1 find to inspect actual doc structure before writing data-loading scripts.

---

## Probes (Run Mentally During Intake)

Fire at most 3 questions. Beyond that, take defaults with confidence "low".

Critical probes (check these always):
1. Is the date range valid and sensible? (not reversed, not future, not before data exists)
2. Does the indicator window fit within the date range? (window * 1.25 < available bars)
3. Signal direction vs spec direction consistent? (long_only but signal can go negative?)
4. Instruments in universe actually exist in the database?
5. Look-ahead bias? (signal uses close[t], fills at close[t] with shift=0)
6. Risk-free rate set if backtest overlaps post-2022?

Option-specific probes:
7. DTE window produces contracts? (sub-7 DTE with no weekly expirations available)
8. Short-dated options without roll rule over a long backtest?
9. Benchmark defined for equity comparison?

---

## Diagnostics Thresholds

| Diagnostic | Medium | High |
|-----------|--------|------|
| regime_concentration | >= 0.6 | >= 0.8 |
| trade_skew (top-5%) | >= 0.5 | top-1% >= 0.5 |
| sharpe_below_benchmark | gap >= 0.5 | — |
| max_drawdown_exceeds | <= -0.3 | <= -0.4 |
| turnover_excessive | >= 12.0 annual | — |

should_suggest = (fired_count >= 2) OR (any high severity fired)

---

## Error Recovery

| Situation | Action |
|-----------|--------|
| execute_python fails | Read stderr, fix the script, retry once. If still fails, write PROBLEMS.md. |
| Data not found for instrument | Report which instrument/collection was tried, ask user for correct ID. |
| Validation FAIL | Report the failure reason, ask if user wants to proceed with caveats. |
| Ambiguous user request | Ask ONE clarifying question about the strategy. Do not guess. |
| Rate limit / timeout | Wait, retry. If persistent, write PROBLEMS.md. |

---

## Iteration Log Format (ITERATIONS.md)

```markdown
## Iteration <N> -- <YYYY-MM-DD HH:MM>

Request: <user prompt verbatim>
Scope: <phases re-run>
Spec diff:
  <field>: <old> -> <new>
Result delta:
  sharpe_ratio: 1.20 -> 1.45
  max_drawdown: -0.18 -> -0.14
Notes: <one line>
```

---

## Data Summary Schema (data/data_summary.json)

```json
{
  "series": [{"id": "IND_SP_500", "kind": "INDEX", "provider": "YAHOO",
    "start": "2020-01-02", "end": "2024-12-31", "n_bars": 1259,
    "n_gaps": 0, "n_nan_close": 0, "cache_path": "data/IND_SP_500.npz"}],
  "loaded_at": "ISO datetime"
}
```
"""
