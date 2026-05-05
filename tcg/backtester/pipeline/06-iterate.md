# P6 — Iterate

Goal: handle variant requests cheaply by re-running the smallest valid phase set, and append a single iteration entry to `ITERATIONS.md`.

## Trigger conditions

P6 runs when EITHER:

A. The user requests a variant ("what if SMA 50 instead of 20", "try fees 1bp", "extend to 2010").
B. `results/diagnostics.json.should_suggest == true` AND the user is still in the session AND the previous iteration in `ITERATIONS.md` is older than the current `manifest.json`.

In case B, propose ONE variant, framed as a question. Do not run it without confirmation.
If `should_suggest == false`, **do not propose anything** — wait for the user.
The gate is computed in P4 (see `pipeline/04-analyze.md` -> Combiner) and is
the single source of truth for whether suggestions fire. P6 never re-derives
it from the raw diagnostic values.

## Variant scope decision

Use the table in `pipeline/00-pipeline.md`. Examples:

- "Try SMA 50 instead of 20" -> edit `STRATEGY.yaml.signals.params.sma_window`; re-run P3, P4, P5.
- "Add VIX as a regime filter" -> edit signals; verify VIX series in `data/`; if missing, add it to universe and re-run P2 first.
- "Use 1% fees" -> edit `STRATEGY.yaml.execution.fees_bps`; re-run P3, P4, P5.
- "Extend to 2010" -> edit `date_range.start`; re-run P2, P3, P4, P5.

## ITERATIONS.md entry format

Append one block per variant:

```
## Iteration <N> — <YYYY-MM-DD HH:MM>

Request: <user prompt verbatim, or auto-suggest text>
Scope: P<a>,P<b>,...
Spec diff:
  <field>: <old> -> <new>
Result delta:
  sharpe_ratio: 1.20 -> 1.45
  max_drawdown: -0.18 -> -0.14
Notes: <one line>
```

The numeric `Result delta` block is mandatory and uses `metrics.json` from the previous iteration vs the new one.

## Result preservation

Snapshots are MANDATORY on every iteration cycle. Skipping the snapshot is a phase-failure — write to `PROBLEMS.md` and halt rather than overwrite without archiving.

**Timing.** Take the snapshot BEFORE phase 5 (report) starts in any iteration cycle. Concretely: as soon as P6 has finalized the variant scope and the spec edit is committed to `STRATEGY.yaml`, copy the existing `results/` artifacts into `results/iter_<N>/`, then proceed to re-run the scoped phases. The current `results/` is the latest run; `iter_<N>/` directories are immutable archives.

**Numbering.** The first run-on-disk (the initial backtest, lives at the top of `results/`) is implicitly iter_0. The first archived snapshot is `iter_1/`. Determine the next N by listing `results/iter_*/` and adding 1 to the highest existing index (or starting at 1 if none exist).

**Snapshot contents** (the full reportable artifact set):

- `manifest.json`
- `metrics.json`
- `plots/` (entire directory)
- `notebook.ipynb`

**`results/iter_log.md`** — append-only one-line ledger of snapshots. Create on first archive, append on every subsequent one. Format:

```
iter_<N>: <YYYY-MM-DD HH:MM> — <one-line spec diff>, <one-line metric delta>
```

Example:

```
iter_1: 2026-05-02 10:30 — sma_fast 20->50, sharpe 1.2->1.5
```

The manifest links to past iterations via the `iterations` array; `iter_log.md` is a quick human index.

## Auto-suggest rules (case B)

Run only when `results/diagnostics.json.should_suggest == true`. Pick ONE
fired diagnostic by the priority order below (top first), and frame the
suggestion as a question. Never run the variant without user confirmation.

| Priority | Diagnostic              | Suggest                                                |
|----------|-------------------------|--------------------------------------------------------|
| 1        | max_drawdown_exceeds    | "Drawdown reached <pct>%. Try a vol-target overlay or a max-DD kill at <X>%?" |
| 2        | regime_concentration    | "MDD concentrated in <period>. Try a VIX>20 (or analogous) regime filter?" |
| 3        | trade_skew              | "Top <k>% of trades carried <pct>% of PnL. Recheck stability without those trades?" |
| 4        | turnover_excessive      | "Annual turnover ~<X>x. Worth widening signal smoothing or rebalancing less often?" |
| 5        | sharpe_below_benchmark  | "Strategy underperforms buy-and-hold by Sharpe gap of <X>. Want to compare risk-adjusted differently?" |

A `high`-severity diagnostic always wins over a `medium`-severity one within
the same priority level. Never auto-suggest more than one variant per
session.

## Output contract

- `ITERATIONS.md` appended.
- Updated `results/` directory.
- Updated `manifest.json.iterations`.
