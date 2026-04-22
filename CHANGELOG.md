# Changelog

All notable changes to TCG-software are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] — 2026-04-22

### Changed — Signals page refactor v4

The Signals page has been restructured around a unified Entries / Exits
model with signed-weight percentages. This is a **breaking** change: the
stored signal schema and the `POST /api/signals/compute` wire shape are
incompatible with prior versions, and no migration code is provided.

- **Two sections instead of four directions.** The long/short split is
  gone. Signals now have two sections — **Entries** and **Exits** — with
  a two-tab UI replacing the former four direction tabs. The active tab
  drives which section's blocks are rendered.
- **Signed weight in `[-100, +100]`.** Each entry block's `weight` is a
  signed percentage. `+100` = full long, no leverage. `-100` = full
  short, no leverage. Block headers show a dynamic `long` (green) /
  `short` (red) / neutral badge driven by the weight sign. The weight
  input now has a `%` suffix glyph.
- **Exits target a specific entry block.** Each exit block carries a
  `target_entry_block_name` referencing an entry by its user-editable
  name. The exit editor has a picker listing existing entries.
  Entries and exits now have stable UUID ids, generated on creation
  and persisted through save/load. When an entry is deleted, every
  referencing exit is cascade-deleted (not flagged).
- **Inline indicator params in signal blocks.** With zero params, a
  non-clickable "No parameters" tag is shown. With one param, the param
  renders inline as `<name>: <value>` — no dropdown. The existing
  "Parameters" dropdown is preserved for two or more params.
- **Signals list: hover-reveal icons.** Edit / delete icons on each
  signal row now fade in on `:hover` and stay visible on
  `:focus-within`, matching the IndicatorsList convention.
- **Indicators page: DEFAULT and CUSTOM sections collapsed by default.**
  Only the initial default changed; user-toggled expanded/collapsed
  state is still preserved across sessions.
- **New signals default to "don't repeat entries/exits" = true.** Newly
  created signals initialise `settings.dont_repeat` to `true`; existing
  saved signals keep their stored value verbatim.
- **Effective-only results when "don't repeat" is active.** The Results
  view now hides redundant markers when the flag is on: entry markers
  only appear on bars where the block actually opened a position, and
  exit markers only on bars where the exit actually closed a position
  on its target entry. Underlying computation is unchanged.
- **BREAKING — storage:** the Signals localStorage key bumped from
  `tcg.signals.v3` to `tcg.signals.v4`. Any previously stored signals
  (v2, v3, or earlier) are discarded on first load with a single
  console warning. No migration is provided.
- **BREAKING — API:** `POST /api/signals/compute` now takes
  `rules: { entries: [...], exits: [...] }` (not the four direction
  keys). Each entry carries a signed `weight`, and each exit carries a
  `target_entry_block_name` referencing an existing entry name in the
  same signal. Dangling references, weight outside `[-100, +100]`, or
  `weight == 0` on entries are rejected with HTTP 400.
- **BREAKING — Exit blocks no longer carry `input_id` at the block
  level.** The operating input is derived from the target entry's
  `input_id` (single source of truth). Payloads that include a non-empty
  `input_id` on an exit block are rejected with HTTP 400 at validation
  time; empty strings are treated as absent. The stored schema drops
  `input_id`/`weight` from exit blocks on load, and the frontend UI
  hides the input picker on exit blocks (showing a read-only derived
  label instead).
- **BREAKING — Exit blocks now reference their target entry by name
  instead of by stable id.** Field renamed from `target_entry_block_id`
  to `target_entry_block_name`. The reference is by value: renaming an
  entry breaks any exit that still references the old name (no automatic
  cascade). Two entries sharing a name invalidate the run until
  disambiguated. Backend rejects dangling names, duplicate entry names,
  and legacy `target_entry_block_id` payloads with HTTP 400.
- **BREAKING — Rolling adjustment method renamed from "proportional" to
  "ratio".** The API parameter, stored schema value, and UI label all
  change from `proportional` to `ratio`. Existing saved instruments
  using the old value will need their adjustment field updated.
- **BREAKING — portfolio-leg math / signal-as-holding scaling.** Signal
  block `weight` is now a **signed fractional position contribution**:
  `+100` means a full-long unleveraged position (1.0× the underlying
  return), `-100` means full-short unleveraged (−1.0× underlying
  return), and intermediate values scale linearly. The engine
  normalises weights to fractional position (`weight / 100`) before
  computing `realized_pnl`, so the synthetic price series emitted by
  `_evaluate_signal_leg` mirrors the underlying instrument's returns at
  the requested fraction. Prior releases passed unsigned weights
  through raw, producing a signal leg that over-amplified underlying
  returns by ~100×; any portfolio composed of a signal leg plus a
  regular instrument leg under that model produced numerically
  inconsistent equity curves. **Historical portfolio results that used
  signal legs are not reproducible under v4.** No migration is
  provided; v3 localStorage signals are discarded on first load.

### Changed

#### Sortino ratio: denominator switched to full-sample count (PR #23)

The Sortino ratio denominator now uses the **total number of daily returns**
in the sample (same count as used for the Sharpe ratio numerator), matching
the Sortino & Price (1994) convention:

```
Sortino = mean(r) / sqrt( sum(r_neg^2) / N )
```

Previously the denominator used only the count of negative returns, which
produces a smaller (more pessimistic) downside deviation and therefore a
higher Sortino ratio than the published formula.

**Impact on existing reports:** Sortino values computed before this change
will differ from values computed after it. Portfolios with few negative
returns will see the largest differences. Values from prior backtests are
not automatically recomputed.

Implementation: `tcg/engine/metrics.py`, function `sortino_ratio`.

---

#### CVaR-5%: returns 0.0 when fewer than 20 daily returns are available (PR #23)

Conditional Value at Risk at the 5th percentile (`cvar_5`) now returns
`0.0` when the return series contains fewer than **20 observations**.

This is a **behavior change**, not just documentation: previously, CVaR was
computed on whatever data was available (even a handful of points), producing
statistically unreliable tail estimates. The 20-observation floor avoids
returning a misleading extreme quantile from a tiny sample.

**Impact:** Portfolios or date ranges with fewer than 20 daily returns will
now show `cvar_5 = 0.0` in API responses and the frontend metrics panel.
This is intentional and indicates insufficient data for a reliable estimate.

Implementation: `tcg/engine/metrics.py`, function `cvar_5pct` (or equivalent
CVaR helper). Check the `if len(returns) < 20` guard at the top of that
function.

---

## [Prior releases]

No formal changelog was maintained before 2026-04-22.
