# P1 — Intake

Goal: bootstrap a `strategy.py` from the user's natural-language prompt, log every inferred default to `ASSUMPTIONS.json`, and populate `META` with a valid set of fields. Ask the user a question only when a behavioural probe fires after the first backtest — probes do not fire at intake time (they need a realised result).

## What intake produces

The single output of P1 is a `strategy.py` at the workspace root. The file has two logical sections:

1. **`META` dict** — above heavy imports so `ast.literal_eval` could parse it without executing anything.
2. **Signal logic** — either `def compute_signal(bars, ctx)` (canonical) or `def run(ctx)` (escape hatch).

Bootstrap from the template:

```bash
cp templates/strategy.py.template workspaces/<slug>/strategy.py
```

Then fill in `META` with values inferred from the user prompt, and replace the `compute_signal` stub with actual signal logic (or uncomment the `run`-shape block for options / multi-leg strategies).

## META keys

| Key             | Required | Notes                                                          |
|-----------------|----------|----------------------------------------------------------------|
| `slug`          | yes      | Workspace identifier, kebab-case                               |
| `description`   | yes      | One-line free text                                             |
| `dates`         | yes      | `{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}`                |
| `universe`      | yes      | List of instrument ids; `universe[0]` is canonical             |
| `benchmark`     | yes      | Instrument id or `{"symbol": ..., "asset_class": ...}` dict   |
| `asset_class`   | no       | `"INDEX"` (default) / `"ETF"` / `"FUT"` / `"OPT"`            |
| `sizing`        | no       | `{"method": "fixed_fraction", "fraction": 1.0}` (default)     |
| `execution`     | no       | `{"fees_bps": 5.0, "slippage_bps": 5.0, "fill_timing": "next_open"}` |
| `capital_base`  | no       | float, default 100_000.0                                       |
| `tags`          | no       | Advisory list, never gatekeeping                               |
| `seed`          | no       | int — for strategies with `np.random` calls                    |

## Algorithm

1. Parse user prompt. Extract candidate values for: `slug`, `description`, `dates`, `universe`, `benchmark`, `asset_class`, `sizing`, `execution`.
2. For every field not extracted, apply the default below and emit an assumption record.
3. Bootstrap the workspace: copy template, fill `META`, sketch the signal stub.
4. If the strategy is unfamiliar (user cites a paper or an obscure indicator), invoke P7 before sketching — write a research note to `research/<strategy-class>.md` and use it to set sensible parameter defaults.
5. Print a 3-line summary: instrument + date range, signal shape chosen, any assumption of note.

Move directly to P2 on completion. Do NOT ask the user to confirm inferred defaults — log them to `ASSUMPTIONS.json` and proceed.

## Default ladder (apply in order)

| Field                        | Default                                                  | Confidence |
|------------------------------|----------------------------------------------------------|------------|
| `meta.slug`                  | kebab-cased prompt summary, truncated to 40 chars        | medium     |
| `meta.description`           | paraphrased prompt, ≤ 80 chars                           | medium     |
| `dates.end`                  | last business day                                        | high       |
| `dates.start`                | end − 5 years                                            | medium     |
| `asset_class`                | `"INDEX"` unless prompt says ETF/futures/options         | high       |
| `execution.fees_bps`         | 5                                                        | high       |
| `execution.slippage_bps`     | 5                                                        | high       |
| `execution.fill_timing`      | `"next_open"`                                            | high       |
| `sizing.method`              | `"fixed_fraction"`                                       | medium     |
| `sizing.fraction`            | 1.0 if single instrument, else 1/N equal-weight          | medium     |
| `benchmark`                  | underlying spot for options strategies; SPX otherwise    | medium     |

## Signal shape decision

Use `compute_signal` (canonical) unless the strategy clearly requires:
- Loading multiple series inside the strategy (multi-instrument).
- Building option legs via `lib.options.build_legs`.
- Running an optimiser or fitting a model (ML / Kalman / HMM).
- Constructing `BacktestSpec` manually with custom field values.

In those cases, use the `run`-shape escape hatch and set `META.sizing.fraction = 0.0` when there is no direct underlying exposure.

## Multi-leg / options strategies

For options strategies, sketch the `run`-shape escape hatch using `lib.options.build_legs` (or one of the named helpers: `iron_condor`, `vertical`, `calendar`, `straddle`, `strangle`). Each leg is a dict with `side`, `option_type`, `strike` (float, `("offset_pct", x)`, `("moneyness", m)`, or `("atm", offset)`), optional `leg_id`, optional `exit_rule`. Pass `expiry_selector=DteSelector(target_dte=<N>)` for DTE-targeted strategies.

See `templates/examples/complex_iron_condor/strategy.py` for a worked 4-leg example.

## Assumption record format

`ASSUMPTIONS.json` is a **bare JSON array** of entry objects (not an object with an `"assumptions"` key). Every default OR inferred field becomes one entry:

<!-- canonical-shape -->
```json
[
  {
    "field": "execution.fees_bps",
    "value": 5,
    "source": "default",
    "confidence": "high",
    "rationale": "Day-1 default per project policy.",
    "editable": true,
    "group": "execution"
  }
]
```

Use `source: "inferred"` when the value is derived from prompt context (e.g., user said "trade SPY" → `universe = ["SPY"]`). Use `source: "user"` when the prompt explicitly stated the value.

## Output contract

- `strategy.py` — `META` populated, signal stub or `run` body sketched.
- `ASSUMPTIONS.json` — every non-user field logged.
- `PROBLEMS.md` — empty unless instrument lookup failed.
- stdout — 3 lines: instrument + date range, signal shape, notable assumptions.

Move to P2 immediately on success.

### Question gate

Default behaviour stays infer-and-log: if you have a reasonable answer, write it to `ASSUMPTIONS.json` with `source: "inferred"` and continue.

**Exception — at intake only.** Before any data fetch / backtest / compile, if something is genuinely ambiguous and inferring would be reckless, batch ALL open questions into a single `AskUserQuestion` call. From that point through delivery, no questions — mid-run discoveries get logged in `ASSUMPTIONS.json` and `PROBLEMS.md`, never interrupt.

Don't force questions. The bar is reckless inference, not "any uncertainty."
