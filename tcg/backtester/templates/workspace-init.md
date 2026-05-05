# Per-strategy workspace layout

Every strategy gets a folder under `workspaces/<slug>/`. Layout:

```
workspaces/<slug>/
  strategy.py               # code-first contract, copied from templates/strategy.py.template
  requirements.txt          # pip deps not in pyproject.toml (often empty)
  ASSUMPTIONS.json          # live assumption log
  ITERATIONS.md             # append-only iteration history
  PROBLEMS.md               # append-only failure log (created on demand)
  data/
    <id>.npz                # cached bar series
    chain_<root>_<start>_<end>.pkl   # cached option chains (when applicable)
    data_summary.json
  scripts/
    01_intake.py            # optional, mostly P1 is no-code
    02_data.py              # built from snippets/fetch_*
    03_backtest.py          # calls lib.run_strategy(strategy) + lib.validate.run_probes
    04_analyze.py           # built from snippets/compute_metrics + plot_*
    05_compile.py           # built from snippets/compile_notebook
  results/
    raw_result.pkl
    raw_result.json
    metrics.json
    diagnostics.json
    plots/
      equity.json
      drawdown.json
      yearly_bars.json
      stats_panel.json
      trade_markers.json
      hold_time_hist.json
    notebook.ipynb
    manifest.json
    iter_log.md             # append-only ledger of past snapshots
    iter_1/                 # MANDATORY snapshot of prior run
      manifest.json
      metrics.json
      plots/
      notebook.ipynb
  research/
    <topic>.md
    _log.md
```

## Bootstrap a new workspace

```bash
mkdir -p workspaces/<slug>/{data,scripts,results/plots,research}
cp templates/strategy.py.template workspaces/<slug>/strategy.py
cp templates/requirements.txt.template workspaces/<slug>/requirements.txt
```

Then fill in `META` (slug, dates, universe, benchmark, asset_class) and
replace the `compute_signal` stub. The three canonical examples under
`templates/examples/` cover the simple, options-multi-leg, and
novel-dependency shapes.

## Hygiene rules

- Never delete files in `workspaces/<slug>/`. To discard work, archive
  the whole slug folder elsewhere.
- `data/*.npz` is reusable across iterations — do not regenerate unless
  dates / instrument change.
- `results/` reflects the latest run. Past runs live in `results/iter_<N>/`
  (see `pipeline/06-iterate.md`).
- `PROBLEMS.md` exists only when something failed. Empty file = success.
