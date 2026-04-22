# Changelog

All notable changes to TCG-software are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] — 2026-04-22

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
