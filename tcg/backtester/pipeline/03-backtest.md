# P3 — Backtest

Goal: produce `results/raw_result.pkl` (and a JSON twin) by running `lib.engine.run_backtest` against the spec. Do not analyze yet.

## Generate `scripts/03_backtest.py`

Start from `snippets/run_basic_backtest.py`. Replace its placeholder edits with values from `STRATEGY.yaml`. The script MUST:

1. Load cached series from `data/`.
2. Build a `BacktestSpec` with explicit `execution` (`fees_bps`, `slippage_bps`, `fill_timing`, `look_ahead_shift`, `risk_free_rate`), `bars`, `signal`, `sizing`, `benchmark`, `option_legs` (when applicable). The bars and signal arrays go directly on the spec; there is no separate `data` argument.
3. Call `lib.engine.run_backtest(spec) -> BacktestResult`. Single argument; the spec carries everything the engine needs.
4. `pickle.dump` the result to `results/raw_result.pkl` and write the JSON-safe view via `result.to_json_dict()` to `results/raw_result.json`.

## Default execution config (do not deviate without an assumption record)

Execution defaults come from `STRATEGY.yaml.execution.*`, set at intake (see `pipeline/01-intake.md` § Default ladder). Any override MUST already be in `ASSUMPTIONS.json` with `source: "user"` or `source: "inferred"`.

## Pure-options strategies (no underlying exposure)

When `signals.legs` contains option legs and you do NOT want underlying exposure (typical for iron condors, vertical spreads, calendar spreads, covered structures), suppress the underlying with **either** of these patterns:

- **Preferred**: pass entry trigger via `BacktestSpec.secondary_signals={"entry": signal}` and set `BacktestSpec.signal=np.zeros(N)`. Each `OptionLegSpec` then references `entry_signal="entry"`.
- **Compact**: keep the entry trigger on `BacktestSpec.signal` and set `BacktestSpec.sizing=SizingConfig(method="fixed_fraction", fraction=0.0)`. The `fraction=0` makes underlying notional 0 while option legs retain their `qty_units`.

Mixing underlying directional exposure with option overlays (covered calls, collars) is supported by leaving sizing non-zero — but the `ASSUMPTIONS.json` must record the choice explicitly.

## Look-ahead policy

The engine applies `positions = np.roll(positions, look_ahead_shift); positions[:look_ahead_shift] = 0`. This is the only acceptable look-ahead handling. Never compute signals using `close[t]` and fill at `close[t]` in the same bar.

## Daily-rebalance signal semantics (IMPORTANT for option strategies)

The engine fires entry only when the entry signal **transitions** from `0`
to nonzero, OR changes sign. A constant `signal=np.ones(N)` opens exactly one
position over the entire run — even if the leg uses `exit_rule: days_to_hold`
with `n=1`. The opened-and-closed slot becomes idle the next bar because the
signal hasn't transitioned again.

To re-enter on every bar (daily rebalance, weekly rebalance with
`DaysToHold(n=5)`, etc.), the entry signal must change every bar. Two
canonical patterns:

1. **`lib.signals.daily_pulse(n_bars)`** — alternating `[+1, -1, +1, -1, ...]`.
   The leg side comes from `OptionLegSpec.side`, not the trigger sign, so PnL
   is invariant to the alternation. Preferred for "fire every bar" cases.

2. **Custom signal** — produce an array where `signal[t] != signal[t-1]` on
   every bar where you want a fresh entry.

```python
from tcg_backtester.lib.signals import daily_pulse

signal = daily_pulse(n_bars=len(bars.dates))
spec = BacktestSpec(..., signal=signal,
                    sizing=SizingConfig(method="fixed_fraction", fraction=0.0))
```

Pair with `exit_rule: {kind: days_to_hold, n: 1}` for daily-rebalance.

## `__file__` in compiled notebooks

`scripts/*.py` are concatenated into `results/notebook.ipynb` as cells with
**no inherent `__file__`**. Patterns like `Path(__file__).resolve().parent`
crash with `NameError` when run from inside the notebook.

**Use `Path.cwd()` instead.** The notebook bootstrap in `lib.compile` calls
`os.chdir` to the workspace dir (the directory containing `STRATEGY.yaml`)
before any user cell runs, so `Path.cwd() / "data"` is always the workspace
data directory. The bootstrap also injects a synthetic `__file__` fallback
pointing at `<workspace>/scripts/notebook_cell.py` so legacy patterns still
resolve, but new scripts should use `Path.cwd()`.

```python
# Canonical pattern (works in scripts AND in compiled notebook cells):
from pathlib import Path
WS = Path.cwd()
DATA = WS / "data" / "SPX.npz"
```

## Signal generation

For each leg in `signals`:
- indicator-based: build the indicator series via `snippets/compute_signals_<indicator>.py`. Output a signed `{-1,0,1}` array aligned to `dates`.
- option-leg: build the per-contract position via `snippets/option_strategy_<name>.py`.
- composite: combine leg signals with the declared weights; clip net position to `[-1, 1]` for fixed_fraction sizing.

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
- `meta: dict` — flat snapshot under `meta["spec"]`; also contains `n_bars`, `nan_bars`, `instrument_id`, `benchmark_id`, `n_option_legs_*`, etc.

There is no `daily_returns` field; compute it from `equity_curve` if needed (`metrics._bar_returns` does this internally). The JSON-safe view `result.to_json_dict()` returns `{"dates": [...iso...], "equity": [...], "benchmark_equity": [...] | null, "drawdown": [...], "positions": [...], "cash": [...], "gross_exposure": [...], "trades": [...dicts...], "meta": {...}}`.

## Output contract

- `results/raw_result.pkl` — pickled `BacktestResult`.
- `results/raw_result.json` — `result.to_json_dict()` written via `json.dump`.

Move to P4 immediately on success.
