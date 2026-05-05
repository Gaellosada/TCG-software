# P4 — Analyze

Goal: compute the metrics suite, build the fixed Plotly figures, and write `results/metrics.json` and `results/plots/*.json`.

## Metrics

Use `lib.metrics.compute_metrics(result) -> MetricsSuite`. Field names match the production MetricsSuite exactly:

```
total_return, annualized_return, sharpe_ratio, sortino_ratio,
max_drawdown, calmar_ratio, cvar_5, time_underwater_days,
annualized_volatility, num_trades, win_rate
```

`num_trades` and `win_rate` MUST be populated from `result.trades`. Use 252 trading days for annualization.

`cvar_5` collapses to 0.0 when `n_returns < 20`. Document this in the assumption log with confidence "high" only when triggered.

Save to `results/metrics.json` as an object keyed by the field names above plus an `as_of` ISO timestamp.

## Plots (baseline set)

The baseline plot set is encoded in `lib.plotting.BASELINE_PLOTS` — every strategy that produces an equity curve gets these automatically. Use the relevant snippet for each:

| Plot id              | Source data                        | Snippet                            |
|----------------------|------------------------------------|------------------------------------|
| `equity`             | dates, equity, benchmark_equity    | `snippets/plot_equity.py`          |
| `drawdown`           | dates, equity                      | (same script, second figure)       |
| `returns_heatmap`    | dates, daily_returns               | `snippets/plot_returns_heatmap.py` |
| `yearly_bars`        | dates, daily_returns               | (same script, optional figure)     |
| `trade_markers`      | dates, close, trades               | `snippets/plot_trade_markers.py`   |
| `hold_time_hist`     | trades                             | (in trade_markers script)          |
| `stats_panel`        | metrics, benchmark_metrics         | `snippets/plot_stats_panel.py`     |

Each plot is saved as a Plotly figure JSON via `fig.write_json(path)` to `results/plots/<plot_id>.json`. The frontend and the compile step consume these directly.

For strategy-specific diagnostics, declare `EXTRA_PLOTS` in `strategy.py` — the compile pipeline writes `results/plots/<id>.json` for each `PlotJob` and the auto-render safety net inserts render cells. Do NOT invent new plot ids in P4.

## Compute monthly_returns and yearly_returns

These are part of the report manifest, not a separate plot. Format matches the production portfolio response:

```json
[{"period":"2024-01","portfolio":0.034,"benchmark":0.012}, ...]
```

`lib/compile.py` populates the manifest's `monthly_returns` / `yearly_returns` rows via `lib.metrics.monthly_returns_table` / `yearly_returns_table`.

## Actionable insight detection (feeds P6 auto-suggest)

Compute the diagnostics below and persist to `results/diagnostics.json`. P4 computes raw values, fires each diagnostic against a principled threshold, tags severity (`medium` or `high`), and emits a single boolean gate `should_suggest`. P6 reads the gate; if `False`, P6 does NOT propose a variant.

### Diagnostic table

| Diagnostic                | Raw value                                                              | Fires (medium) when            | High-severity escalation         |
|---------------------------|------------------------------------------------------------------------|--------------------------------|----------------------------------|
| `regime_concentration`    | top-1-period share of max-DD bars / total bars in worst single calendar period | `>= 0.6`               | `>= 0.8`                         |
| `trade_skew`              | sum(pnl of top-5%-by-pnl trades) / sum(all pnl)                        | top-5% share `>= 0.5`          | top-1% share `>= 0.5`            |
| `sharpe_below_benchmark`  | benchmark_sharpe − portfolio_sharpe                                    | gap `>= 0.5`                   | (no high tier on its own)        |
| `max_drawdown_exceeds`    | metrics.max_drawdown                                                   | `<= -0.3` (drawdown ≥ 30%)     | `<= -0.4` (drawdown ≥ 40%)       |
| `turnover_excessive`      | annualized turnover (trades_per_year × avg_position_change_fraction)   | `>= 12.0`                      | (no high tier on its own)        |

`trade_skew` requires the top-5%-share AND the top-1%-share computed separately. The `high` tier fires when the top-1% share alone is `>= 0.5`.

### Combiner — `should_suggest`

```
fired_diagnostics = [d for d in all_diagnostics if d.fired]
high_severity     = [d for d in fired_diagnostics if d.severity == "high"]

should_suggest = (
    len(fired_diagnostics) >= 2
    or len(high_severity) >= 1
)
```

Two independent medium-severity signals OR one high-severity signal clears the bar. A single medium signal alone is not enough — that's the spam guard.

### `diagnostics.json` shape

```json
{
  "as_of": "2026-05-02T14:30:00Z",
  "should_suggest": true,
  "diagnostics": [
    {
      "id": "regime_concentration",
      "value": 0.72,
      "threshold_medium": 0.6,
      "threshold_high": 0.8,
      "fired": true,
      "severity": "medium",
      "context": {"worst_period": "2022", "max_dd_bars": 181, "total_bars": 252}
    },
    {
      "id": "max_drawdown_exceeds",
      "value": -0.34,
      "threshold_medium": -0.3,
      "threshold_high": -0.4,
      "fired": true,
      "severity": "medium"
    },
    {
      "id": "trade_skew",
      "value": {"top_5pct_share": 0.31, "top_1pct_share": 0.12},
      "threshold_medium": 0.5,
      "threshold_high": 0.5,
      "fired": false,
      "severity": null
    }
  ]
}
```

`diagnostics[]` is exhaustive (one entry per diagnostic in the table, even when not fired) so the file is self-documenting.

## Output contract

- `results/metrics.json`
- `results/plots/equity.json`, `drawdown.json`, `returns_heatmap.json`, `yearly_bars.json`, `trade_markers.json`, `hold_time_hist.json`, `stats_panel.json`
- `results/diagnostics.json`

Move to P5 immediately on success.
