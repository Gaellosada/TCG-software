# P3 — Backtest

Goal: produce `results/raw_result.pkl` (and a JSON twin) by running the strategy end-to-end via `lib.run_strategy`. Do not analyse yet.

## The strategy contract

`strategy.py` defines `META` plus either:

- **`compute_signal(bars, ctx) -> NDArray[np.float64]`** — canonical shape. The lib loads bars per `META`, calls `compute_signal`, applies the one-bar look-ahead shift internally, sizes, and runs the engine. The returned array must be the same length as `bars.dates`.
- **`run(ctx) -> BacktestResult`** — escape hatch. The strategy loads its own series, builds legs, runs its own `ctx.run_backtest(spec)` call, and returns the result. `ctx.bars` is `None` in this shape.

The lib detects which shape is present. If both are defined, `run` wins (lib logs a warning).

## Generate `scripts/03_backtest.py`

Start from `snippets/run_basic_backtest.py`. The script MUST:

1. Import and load the strategy module: `import importlib.util; strategy = importlib.util.spec_from_file_location(...)` or simply `import strategy` when CWD is the workspace root.
2. Call `lib.run_strategy(strategy, workspace_path=Path.cwd())` — single call, returns `BacktestResult`.
3. Call `lib.validate.run_probes(strategy, bars, result, workspace_path=Path.cwd())` and surface the first failure via `first_fired(report)`.
4. `pickle.dump` the result to `results/raw_result.pkl` and write `result.to_json_dict()` to `results/raw_result.json`.

```python
from pathlib import Path
import pickle, json, importlib.util

from lib import run_strategy
from lib.validate import run_probes, first_fired

WS = Path.cwd()

spec = importlib.util.spec_from_file_location("strategy", WS / "strategy.py")
strategy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(strategy)

result = run_strategy(strategy, workspace_path=WS)

# Run behavioural probes; ask user only if one fires.
report = run_probes(strategy, getattr(result, "_bars", None), result, workspace_path=WS)
fired = first_fired(report)
if fired:
    print(f"[probe] {fired}")

WS.joinpath("results").mkdir(parents=True, exist_ok=True)
with open(WS / "results" / "raw_result.pkl", "wb") as f:
    pickle.dump(result, f)
with open(WS / "results" / "raw_result.json", "w") as f:
    json.dump(result.to_json_dict(), f)
print(f"backtest done: equity[-1]={result.equity[-1]:.4f}, trades={len(result.trades)}")
```

## Execution config

`META["execution"]` carries fees, slippage, and fill timing. `run_strategy` reads these and builds `ExecutionConfig` internally. To override a field without changing `META`, pass it explicitly in `META` with an assumption record in `ASSUMPTIONS.json`.

Default values (applied if `META["execution"]` is absent):
- `fees_bps=5.0`, `slippage_bps=5.0`, `fill_timing="next_open"`.

## Options strategies (run-shape)

For strategies that use `lib.options.build_legs`, the `run` function handles everything: it loads bars, builds the `OptionLegSpec` tuple via `build_legs`, attaches it to `BacktestSpec`, and calls `ctx.run_backtest(spec)`. No special P3 scaffolding needed — `run_strategy` dispatches to `strategy.run(ctx)` and returns the result.

For pure-options strategies with no underlying directional exposure, set `sizing.fraction=0.0` in `BacktestSpec`. Use `lib.indicators.daily_pulse(n)` as the entry trigger so the engine fires a fresh entry on every bar.

See `templates/examples/complex_iron_condor/strategy.py` for a worked 4-leg example.

## Look-ahead policy

The engine applies `positions = np.roll(positions, look_ahead_shift); positions[:look_ahead_shift] = 0` internally. This is the only acceptable look-ahead handling — never compute signals using `close[t]` and fill at `close[t]` in the same bar.

The `no_lookahead` probe in `run_probes` samples 5 mid-range indices and asserts `compute_signal(bars[:-1], ctx)[i] == compute_signal(bars, ctx)[i]` for `i < len-1`. It runs automatically in P3; surface any firing via `first_fired`.

## Daily-rebalance signal semantics (important for options)

The engine fires entry only when the signal **transitions** from 0 to nonzero, OR changes sign. A constant `signal=np.ones(N)` opens exactly one position over the entire run.

To re-enter on every bar:

```python
from lib.indicators import daily_pulse

signal = daily_pulse(n_bars=len(bars.dates))  # alternating +1, -1, +1, -1, ...
```

For long-only daily rebalance, wrap with `apply_direction(signal, "long_only")`.

## `__file__` in compiled notebooks

`scripts/*.py` are concatenated into `results/notebook.ipynb` as cells with no inherent `__file__`. Patterns like `Path(__file__).resolve().parent` crash with `NameError` inside the notebook.

**Use `Path.cwd()` instead.** The compile step chdirs to the workspace dir before any user cell runs, so `Path.cwd() / "data"` is always the workspace data directory.

```python
# Canonical pattern (works in scripts AND in compiled notebook cells):
from pathlib import Path
WS = Path.cwd()
DATA = WS / "data" / "SPX.npz"
```

## BacktestResult contents (defined in `lib.engine`)

The frozen dataclass exposes:

- `dates: NDArray[int64]` — YYYYMMDD ints, one per bar.
- `equity_curve: NDArray[float64]` — primary equity curve. The `equity` property is an alias.
- `benchmark_curve: NDArray[float64] | None` — None when no benchmark was set.
- `drawdown_curve: NDArray[float64]` — fractional drawdown vs running max.
- `trades: list[Trade]` — `Trade(date, side, qty, price, cost, pnl, leg)`.
- `positions: NDArray[float64]` — target weights per bar (post look-ahead shift).
- `cash: NDArray[float64]` — book-keeping cash curve.
- `gross_exposure: NDArray[float64]` — |target| + leg notionals / capital_base.
- `meta: dict` — flat snapshot; contains `n_bars`, `instrument_id`, `benchmark_id`, `n_option_legs_*`, etc.

The JSON-safe view `result.to_json_dict()` returns `{"dates": [...iso...], "equity": [...], "benchmark_equity": [...] | null, "drawdown": [...], "positions": [...], "cash": [...], "gross_exposure": [...], "trades": [...dicts...], "meta": {...}}`.

## Output contract

- `results/raw_result.pkl` — pickled `BacktestResult`.
- `results/raw_result.json` — `result.to_json_dict()` written via `json.dump`.

Move to P4 immediately on success.
