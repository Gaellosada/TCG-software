# Master pipeline

This file defines the state machine. Every other `pipeline/*.md` is a phase implementation. Read this once at session start, then jump to the active phase.

## State files (per workspace)

| File                       | Owner phase | Lifecycle                     |
|----------------------------|-------------|-------------------------------|
| `strategy.py`              | intake      | created in P1, edited in P6   |
| `ASSUMPTIONS.json`         | intake      | created in P1, appended P2-P6 |
| `data/data_summary.json`   | data        | rewritten on every P2 run     |
| `data/<series>.npz`        | data        | cached, reused across iters   |
| `scripts/03_backtest.py`   | backtest    | regenerated on every P3 run   |
| `results/raw_result.pkl`   | backtest    | rewritten in P3               |
| `results/metrics.json`     | analyze     | rewritten in P4               |
| `results/plots/*.json`     | analyze     | rewritten in P4               |
| `results/diagnostics.json` | analyze     | rewritten in P4               |
| `results/notebook.ipynb`   | report      | rewritten in P5               |
| `results/manifest.json`    | report      | rewritten in P5               |
| `ITERATIONS.md`            | iterate     | append-only                   |
| `research/<topic>.md`      | research    | append-only                   |
| `PROBLEMS.md`              | any         | append-only                   |

## Phase entry conditions

Run a phase iff its declared inputs exist and are newer than its output, OR the user's request invalidates the prior output.

| Phase | Inputs                                              | Outputs                              |
|-------|-----------------------------------------------------|--------------------------------------|
| P1    | user prompt                                         | strategy.py, ASSUMPTIONS.json        |
| P2    | strategy.py (META.universe, META.dates)             | data/*.npz, data/data_summary.json   |
| P3    | strategy.py (compute_signal or run), data/*         | results/raw_result.{pkl,json}        |
| P4    | results/raw_result.pkl                              | results/metrics.json, plots/*.json, results/diagnostics.json |
| P5    | scripts/, results/metrics.json, plots/              | notebook.ipynb, manifest.json        |
| P6    | user variant request OR P4 actionable insight       | ITERATIONS.md entry, re-run subset   |
| P7    | invoked from P1 or P6                               | research/<topic>.md                  |

## Variant scope decision (P6)

Given a variant request, identify the smallest valid re-run set:

| Change in spec                              | Re-run                  |
|---------------------------------------------|-------------------------|
| signal logic (e.g., SMA window tweak)       | P3, P4, P5              |
| sizing rule                                 | P3, P4, P5              |
| execution config (fees, slippage, fill)     | P3, P4, P5              |
| date_range subset of cached range           | P3, P4, P5              |
| date_range extending beyond cached range    | P2, P3, P4, P5          |
| new instrument                              | P2, P3, P4, P5          |
| new signal shape entirely                   | P3-P5 (no P1 re-run needed — strategy.py is the spec) |
| reporting tweak (section override only)     | P5                      |

Always append to `ITERATIONS.md` before re-running. One iteration entry per variant request. Never delete prior results.

Snapshots are MANDATORY on every iteration cycle. See `pipeline/06-iterate.md` § Result preservation for timing, numbering, contents, and the `iter_log.md` ledger format.

## Decision tree at session start

```
exists(strategy.py)?
  no  -> P1
  yes -> exists(results/manifest.json)?
           no  -> resume at first phase whose output is missing
           yes -> user message implies a variant?
                    yes -> P6
                    no  -> answer questions about the existing report; do not silently re-run
```

## Phase boundary contract

| # | Rule |
|---|------|
| 1 | Read declared inputs only. |
| 2 | Write outputs atomically (`.tmp` then rename). |
| 3 | Append to `ASSUMPTIONS.json` for every applied default. |
| 4 | Log one-line status: `[P<N>] <verb> <object> ok/failed: <reason>`. |
| 5 | On failure: append to `PROBLEMS.md` (phase, timestamp, error, recovery) then halt. |

No phase reads or writes outside the workspace.

## Determinism rule

Same `strategy.py` + same `ASSUMPTIONS.json` user-confirmed values + same data snapshot MUST produce the same `results/manifest.json` modulo timestamps. The pipeline path through phases is fully determined by the state-file presence table above. No agent freelancing.

## Failure messages

For each phase, when it cannot proceed, the agent emits one of these canonical messages followed by the specific cause:

- P1 intake: "Cannot finalise strategy — <reason>. See `PROBLEMS.md`."
- P2 data: "Data load failed for <instrument>:<range> — <reason>. The MongoDB collection returned <N> bars."
- P3 backtest: "Backtest aborted — <reason>. Inputs: <bars=N, signal=N, benchmark=N>."
- P4 analyze: "Analysis aborted — <reason>."
- P5 report: "Report compile failed — <reason>. Notebook output may be incomplete."

Always: explain in plain English, do not surface stack traces, write detail to `PROBLEMS.md`.
