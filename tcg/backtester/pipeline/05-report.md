# P5 — Report

Goal: compile the work scripts and computed artifacts into one notebook and one manifest. The notebook is the human-readable deliverable; the manifest is the machine deliverable.

## Compile call

The pipeline is two sibling steps: `compile_workspace` builds the notebook by
executing `scripts/*.py` via jupytext+nbclient; `emit_manifest` then writes the
schema-compliant JSON manifest from the BacktestResult and MetricsSuite. The
two functions do NOT chain internally — the agent calls both.

### No recursive compile

`compile_workspace` MUST NOT be called from inside `scripts/05_*.py`. The
inner call would spawn a second nbclient kernel inside an already-running
asyncio loop, leading to a re-entry crash. `compile_workspace` enforces this
by setting the `TCG_BACKTESTER_COMPILE_ACTIVE` env var when its kernel runs;
a recursive call raises `RuntimeError` with a clear message.

The canonical pattern:

- `scripts/05_compile.py` (presentation script — runs INSIDE the compiled
  notebook): only emits the manifest via `cc.emit_manifest(...)`. No
  `compile_workspace` call.
- A top-level driver OUTSIDE `scripts/` (the agent's REPL session, or
  `snippets/compile_notebook.py`): calls `cc.compile_workspace(...)` once.

```python
import json
import pickle
from pathlib import Path
from tcg_backtester.lib import compile as cc

WORKSPACE = Path(".").resolve()

# 1) Compile scripts into the notebook.
notebook_path = cc.compile_workspace(WORKSPACE, execute=True)

# 2) Reload the BacktestResult + metrics produced earlier.
with open(WORKSPACE / "results" / "raw_result.pkl", "rb") as f:
    result = pickle.load(f)
metrics = json.loads((WORKSPACE / "results" / "metrics.json").read_text())

# 3) Gather plot paths (results/plots/*.json) and emit the manifest.
plot_paths = {p.stem: str(p.relative_to(WORKSPACE))
              for p in (WORKSPACE / "results" / "plots").glob("*.json")}
manifest_path = cc.emit_manifest(WORKSPACE, result, metrics, plot_paths=plot_paths)
```

`compile_workspace(workspace_dir, *, template_path=None, execute=True, on_error="abort", timeout_per_cell_s=120, auto_render_plots=True)` returns the path to `results/notebook.ipynb`. `emit_manifest(workspace_dir, result, metrics, plot_paths=...)` returns the path to `results/manifest.json`. Internally it derives `monthly_returns`, `yearly_returns`, and `rebalance_dates` from the BacktestResult; populates the schema; writes the file.

### Atomic write contract

`compile_workspace` writes the notebook to `results/notebook.ipynb.tmp` first
and renames atomically on success. On execution failure: `notebook.partial.ipynb`
is dropped for inspection, the `.tmp` is removed, and the canonical
`results/notebook.ipynb` is left untouched (or absent on first run). Reruns
never observe a half-built notebook at the canonical path.

### Inline plot rendering

Auto-injection: when `auto_render_plots=True` (default), every plot file in
`results/plots/*.json` not already referenced via a `<!-- PLOT:<id> -->`
marker or relative path string in the presentation scripts gets an
auto-injected `pio.from_json(...).show()` cell appended to the notebook.
This means: even if `scripts/05_*.py` only emits the manifest, the
deliverable notebook still shows every saved plot inline. The agent SHOULD
still write explicit render cells when section ordering matters
(`templates/notebook-template.py` has the canonical layout); the
auto-injection is the safety net.

`emit_manifest` validates the manifest against `templates/report-schema.json`
before write; malformed manifests raise `ValueError` and the file is not
created. Agents need not call a separate validator — the schema check is
built in.

## Notebook section order (locked)

1. Strategy Description
2. Assumptions
3. Data Summary
4. Backtest Setup
5. Equity Curve + Benchmark
6. Drawdown
7. Returns Tables
8. Metrics
9. Trade Statistics
10. Free-Form Analysis (the only section the agent may extend)
11. Iteration Log

The agent MAY add markdown cells inside section 10, and only there. Adding cells outside section 10 is a guardrail violation.

## Manifest schema

`templates/report-schema.json`. Extends the production `/api/portfolio/compute` shape with:

- `trades: array<{date, side, qty, price, cost, pnl, leg}>`
- `benchmark_equity: array<float>`
- `assumptions_ref: string` — relative path to `ASSUMPTIONS.json`
- `notebook_path: string` — relative path to the notebook
- `plot_paths: object<name, path>` — relative paths
- `iterations: array<{timestamp, summary, scope}>`

## Output contract

- `results/notebook.ipynb`
- `results/manifest.json`

Print exactly two lines:

```
notebook: <abs path>
manifest: <abs path>
```

Then go to P6 unless the user has not asked for iteration. By default, after P5 stop and wait.
