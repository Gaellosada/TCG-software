# P1 — Intake

Goal: convert the user's natural-language prompt into a fully populated `STRATEGY.yaml` validated against `templates/strategy-spec.yaml`, with every inferred field logged to `ASSUMPTIONS.json`. Ask the user a question only when an inconsistency probe fires.

## Probe firing rule

Before any inferred default is finalized, run all 22 probes via
`lib.validate.validate_strategy_spec(spec, data_summary=..., realised_signal=...)`
and surface the first fired finding to the user. Only the first probe in firing
order produces a question per intake round; re-run the cascade after the user
answers.

**If no probe fires on a full pass of the catalog, finalize without asking.** Inferred defaults stand; no clarifying question, no confirmation prompt. Move directly to P2.

## Probe execution — call the lib

Use `lib.validate.validate_strategy_spec` rather than re-implementing the probe
catalog inline. The catalog text in `pipeline/probes.md` is the spec; the lib
function is the executable form, kept in sync.

```python
from tcg_backtester.lib.validate import validate_strategy_spec, first_fired

findings = validate_strategy_spec(spec_dict, data_summary=ds, realised_signal=sig)
fired = first_fired(findings)
if fired is not None:
    # ask the user, quoting fired.message and fired.suggested_resolution
    ...
```

`findings` is always 22 entries in priority order. Probes that need data
(`data_summary`) or a realised signal (`realised_signal`) and receive `None`
return a deferred entry (`fired=False, severity="low"`); re-run the cascade
after later phases populate those inputs.

## Algorithm

1. Parse user prompt. Extract candidate values for the schema groups: `meta`, `universe`, `date_range`, `execution`, `signals`, `sizing`, `benchmark`, `reporting`.
2. For every schema field not extracted, apply the default below and emit an assumption record.
3. Run the inconsistency probes via `validate_strategy_spec` (see "Probe execution" above). Stop at the first firing probe via `first_fired(findings)`; ask one focused question; integrate the answer; re-run probes from the top.
4. When all probes pass, write `STRATEGY.yaml` and final `ASSUMPTIONS.json`. Print a 3-line summary to stdout.

## Expiry-selector kinds (option strategies)

`expiry_selector.kind` accepts one of:

| kind      | parameters                                | DTE band (engine + chain load) |
|-----------|-------------------------------------------|--------------------------------|
| `dte`     | `target_dte: int`, `tolerance_days: int`  | `[target-tol, target+tol]`     |
| `weekly`  | (none)                                    | `[3, 10]`                      |
| `monthly` | (none)                                    | `[25, 45]`                     |
| `fixed`   | `expiration: int` (YYYYMMDD)              | computed manually              |

When the user says "weekly expiries" or "weeklies", encode `kind: weekly` —
not a hand-rolled `kind: dte` with `target_dte=7, tolerance_days=6`. Both
`chain_args_from_spec` and the engine selectors agree on the band, so the
chain load and the in-bar expiry pick stay aligned.

For "fixed" / "absolute" expirations (e.g. "the April 2024 monthly"), call
`load_chain` with explicitly-computed `dte_min` / `dte_max` rather than going
through `chain_args_from_spec`.

## Option strategies — multi-leg is first-class

Multi-leg option strategies (verticals, calendars, iron condors, butterflies,
straddles, strangles, custom N-leg structures) are fully supported. Express
them in `STRATEGY.yaml` by setting `signals.type: option_strategy` and
populating `signals.legs` with N entries — one per leg. The full leg schema
(side, qty_units, option_type, contract_selector, expiry_selector,
entry_signal, exit_rule, multiplier) is documented in
`templates/strategy-spec.yaml`, with worked examples for iron condor,
calendar spread, and vertical spread.

When the user describes a named multi-leg structure (e.g., "iron condor",
"bull put spread", "calendar"), expand it to the relevant N legs at intake
time. Use `lib.options` constructors as starting templates if helpful, then
hand the fully-populated `signals.legs` list to P3.

## Default ladder (apply in order)

| Field                                | Default                                  | Confidence |
|--------------------------------------|------------------------------------------|------------|
| `meta.author`                        | "agent"                                  | high       |
| `meta.created`                       | today's date YYYY-MM-DD                  | high       |
| `universe.provider`                  | "YAHOO" for INDEX/ETF, native for FUT/OPT| high       |
| `date_range.end`                     | last business day                        | high       |
| `date_range.start`                   | end - 5 years                            | medium     |
| `execution.fees_bps`                 | 5                                        | high       |
| `execution.slippage_bps`             | 5                                        | high       |
| `execution.fill_timing`              | "next_open"                              | high       |
| `execution.look_ahead_shift`         | 1                                        | high       |
| `execution.risk_free_rate`           | 0.0 if range pre-2022 else 0.04          | medium     |
| `sizing.method`                      | "fixed_fraction"                         | medium     |
| `sizing.fraction`                    | 1.0 if single instrument                 | medium     |
| `benchmark.instrument_id`            | underlying spot for option strategies; SPX otherwise | medium |
| `reporting.notebook_template_section_overrides` | {}                          | high       |

## Assumption record format

Every default OR inferred field becomes one entry in `ASSUMPTIONS.json["assumptions"]`:

```json
{
  "field": "execution.fees_bps",
  "value": 5,
  "source": "default",
  "confidence": "high",
  "rationale": "Day-1 default per project policy.",
  "editable": true,
  "group": "execution"
}
```

Use `source: "inferred"` when the value is derived from prompt context (e.g., user said "trade SPY" -> universe.instrument_id = "SPY"). Use `source: "user"` when the prompt explicitly stated the value. `confidence` is high/medium/low.

## Probe-driven asking

Each probe in `pipeline/probes.md` has: id, predicate, message template, suggested resolution. Example: probe `fees_dominate_edge` fires when round-trip costs exceed half the per-trade target edge. Question: "Round-trip costs are about 20 bps but your target per-trade edge is only 30 bps. Costs will eat at least half the edge — confirm fees, raise the target, or trade less often?"

## Spec validation

After filling, validate against the `strategy-spec.yaml` schema (use `pyyaml` + manual schema check; we do not introduce a new validation lib). Required fields per schema MUST be set. If a field cannot be inferred and has no default, that is itself an inconsistency: ask the user.

## Research callout

If the strategy class is unfamiliar (e.g., user mentions a paper or a niche indicator), invoke P7 (research) before validating. Write a one-page summary to `research/<strategy-class>.md` with citations. Use that to set sensible defaults; record source as `inferred` with rationale citing the research note.

## Output contract

- `STRATEGY.yaml` — fully populated, valid against schema.
- `ASSUMPTIONS.json` — every non-user field logged.
- `PROBLEMS.md` — empty unless validation failed.
- stdout — 3 lines: instrument, date range, signal class.

Move to P2 immediately on success.
