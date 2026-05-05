# Behavioural Probes

## Policy

Probes run AFTER the backtest — they need a realised `BacktestResult`, not a spec. They do NOT gate intake (unlike the old 22-probe catalog, which validated a YAML spec before running anything). The contract is: run the backtest, then run probes; if one fires, surface it and ask one focused question.

Ask only the first fired probe per round; re-run after the user answers. Maximum 3 questions per session before taking defaults and logging low-confidence assumptions.

---

## Probes catalog

Call `lib.validate.run_probes(strategy_module, bars, result, workspace_path=...)` after every backtest. This runs all six probes and returns an `IntegrityReport`. Surface the first failure via `first_fired(report)`.

```python
from lib.validate import run_probes, first_fired

report = run_probes(strategy, bars, result, workspace_path=WS)
fired = first_fired(report)
if fired is not None:
    # ask the user, quoting fired
    ...
```

### Probe: `meta_schema`

Checks that `strategy.META` is present and contains the required keys (`slug`, `dates`, `universe`, `benchmark`) with well-formed values.

- Fires when any required key is absent or malformed.
- Ask: "META is missing `<key>`. Add it to `strategy.py` and re-run?"

### Probe: `signal_finite_past_warmup`

For `compute_signal`-shape strategies: the first N rows may be NaN (indicator warmup); after that no NaN and no ±inf.

- Fires when a non-NaN finite value appears at bar `i < warmup` AND then NaN appears at bar `j > warmup`, or when any inf appears anywhere post-warmup.
- Ask: "Signal has NaN or inf values outside the warmup window (bars {j}). Check the indicator computation?"

### Probe: `no_lookahead`

Samples 5 mid-range indices and asserts `compute_signal(bars[:-1], ctx)[i] == compute_signal(bars, ctx)[i]` for `i < len-1`. A mismatch means `compute_signal` peeked at future bars.

- Fires when any sampled index mismatches.
- Skipped for `run`-shape strategies (the strategy drives its own engine call; look-ahead discipline is the strategy author's responsibility — document it in the `run` body).
- Ask: "Signal at bar {i} changes when future bars are appended — look-ahead leak? Check that `compute_signal` only reads `bars[:i+1]`."

### Probe: `position_bounded`

`|position[t]|` ≤ `position_bound_factor × capital_base` (default: 10×). Catches blow-ups from broken sizing or unit mismatches.

- Fires when any bar violates the bound.
- Ask: "Position at bar {t} reaches {v:.0f} — {X}× capital. Is the sizing configuration correct?"

### Probe: `deterministic`

Two calls with the same inputs return arrays with `np.array_equal(...)`. Seeds from `META.get("seed")` if present.

- Fires when the second call returns a different signal array.
- Ask: "Signal is nondeterministic (two identical calls differ). If the strategy uses randomness, set `META['seed']` to fix it."

### Probe: `dependency_recorded`

If `strategy.py` imports a non-stdlib non-`numpy` package not in `pyproject.toml`, it must appear in `requirements.txt`.

- Fires when a third-party import is found in `strategy.py` that is not declared in `requirements.txt` (and is not a standard library module or numpy).
- Skipped when `requirements.txt` is absent.
- Ask: "`strategy.py` imports `<package>` but it is not in `requirements.txt`. Add it so pre-flight can install it?"

---

## Severity contract

`IntegrityReport` carries:

- `ok: bool` — `True` if no failures.
- `severity: str` — `"PASS"` / `"WARN"` / `"FAIL"`.
- `failures: tuple[str, ...]` — probe ids that failed.
- `warnings: tuple[str, ...]` — probe ids that warned.

`first_fired(report)` returns the first probe id in failure order, or `None` if all pass.

| severity | ok | Action |
|----------|----|--------|
| PASS     | True | Proceed silently. |
| WARN     | True | Proceed; surface warning to user via status message. |
| FAIL     | False | Ask user; do not advance to P4 until resolved or explicitly dismissed. |

Dismissal: if the user confirms they understand the probe finding and want to proceed, advance and log the dismissal in `ASSUMPTIONS.json` with `confidence: "low"`.
