# Design Decisions

## YYYYMMDD Integers for Dates

**Decision:** All dates in the system are `int64` YYYYMMDD integers (e.g., `20240115`), not `datetime` objects.

**Rationale:**
- Fast comparison: integer `<`, `>`, `==` is faster than datetime comparison.
- No timezone issues: integer dates have no timezone, no DST ambiguity.
- NumPy friendly: `NDArray[int64]` is a first-class NumPy type. Date arithmetic on int arrays is trivial (sorting, masking, `searchsorted`).
- Compact: 8 bytes per date vs. 28+ bytes for a Python `datetime`.
- Legacy compatible: the MongoDB `eodDatas` already stores dates as YYYYMMDD integers in many collections.

**Trade-off:** Converting to ISO strings for display requires explicit conversion (`int_to_iso` in `_utils.py`). Calendar-day arithmetic requires round-tripping through `datetime.date` (see `int_to_date`/`date_to_int`). The YYYYMMDD integer distance is non-uniform across month boundaries (e.g., `20240131` to `20240201` = distance 70 as integers, but 1 calendar day). This is documented in `adjustment.py` and does not cause meaningful errors in practice.

**Locations:** `tcg/types/market.py` (PriceSeries.dates), `tcg/data/_utils.py` (converters), used throughout engine and rolling modules.

## Columnar PriceSeries

**Decision:** Price data is stored as parallel NumPy arrays (dates, open, high, low, close, volume) rather than as a list of row objects.

**Rationale:**
- Vectorized operations: NumPy array math on close prices, returns, masking. No per-row iteration needed.
- Memory locality: each field is contiguous in memory, which is cache-friendly for column-wise operations (e.g., computing returns from close prices).
- Performance: the engine computes daily returns, equity curves, and metrics purely with NumPy array operations. Row-based would require unpacking into arrays first.

**Trade-off:** Slicing by row index requires `PriceSeries(dates[mask], open[mask], ...)` on each field. This pattern is repeated in `_rolling/calendar.py`, `_utils.py`, `service.py`. A helper method on PriceSeries could reduce this boilerplate.

**Locations:** `tcg/types/market.py`, used everywhere.

## LRU Cache at Service Layer

**Decision:** A simple `OrderedDict`-based LRU cache (200 entries) sits in `DefaultMarketDataService`, caching both individual instrument prices and continuous series.

**Rationale:**
- Avoids repeated MongoDB queries for the same data within a session (e.g., when a user adjusts portfolio weights, the underlying price data does not need to be re-fetched).
- Service layer is the right boundary: cache keys include all parameters (collection, instrument, provider, date range, roll config), and the service already does date filtering and NaN sanitization -- caching the clean result avoids re-processing.
- Simple implementation: no external dependency (Redis, etc.), no thread-safety needed (Motor is single-threaded per event loop).

**Trade-off:** No TTL, no invalidation. Stale data persists until eviction or server restart. Acceptable because the underlying data (historical prices) is append-only and rarely corrected.

**Locations:** `tcg/data/_cache.py`, `tcg/data/service.py`.

## NaN Sanitization at Adapter Boundary

**Decision:** All NaN sanitization happens in `tcg/data/_mongo/helpers.py`, before data enters the rest of the system.

**Rationale (architecture guide section 3.10):**
- Single point of defense: downstream code (engine, API, frontend) never encounters NaN from the data source.
- Clear contract: if close is NaN, the entire bar is dropped (with a warning log). Non-critical fields (open, high, low, volume) get NaN replaced with 0.0.
- Prevents NaN propagation: a single NaN in a price series can corrupt an entire equity curve computation via `cumprod`.

**Trade-off:** Information loss -- NaN bars are silently dropped. But NaN close prices in historical data are almost always data quality issues (missing data from the provider), not meaningful zeros.

**Locations:** `tcg/data/_mongo/helpers.py` (`extract_price_data`, `_sanitize_non_critical`).

## Legacy _id Polymorphism

**Decision:** Support three `_id` types (ObjectId, string, composite dict) via a serialize/deserialize pattern, rather than migrating the database.

**Rationale:**
- The legacy Java platform created documents with different `_id` types depending on the collection and insertion logic. Migrating would require touching every document in every collection and updating any code that references them.
- The serialize/deserialize pattern is self-contained in `helpers.py` -- the rest of the system sees string IDs.
- When querying, `deserialize_doc_id` produces a priority-ordered list of candidates and `_find_document` tries each until a match is found. This handles all legacy cases without schema migration.

**Trade-off:** Query overhead: up to 3 queries per document lookup (composite, ObjectId, string). In practice, the first candidate usually matches.

**Locations:** `tcg/data/_mongo/helpers.py` (`serialize_doc_id`, `deserialize_doc_id`), `tcg/data/_mongo/instruments.py` (`_find_document`).

## Protocol-Based Data Access

**Decision:** Consumers depend on `Protocol` types (`MarketDataService`, `StrategyStore`, `ResultStore`), not concrete implementations.

**Rationale:**
- Testability: unit tests can provide mock implementations without touching MongoDB.
- Replaceability: the MongoDB adapter could be swapped for a file-based or in-memory implementation.
- Clean module boundary: `tcg.data.__init__` exports only protocols and the `create_services()` factory. The `_mongo/` and `_rolling/` directories are implementation details.

**Locations:** `tcg/data/protocols.py`, `tcg/data/__init__.py`.

## Collection Discovery at Startup

**Decision:** Available MongoDB collections are discovered dynamically via `db.list_collection_names()` at startup, not hardcoded.

**Rationale:**
- The legacy database has many collections (FUT_VIX, FUT_SP_500, FUT_ES, etc.) and new ones may be added. Hardcoding would require code changes for each new collection.
- `CollectionRegistry` classifies by prefix convention at discovery time.

**Locations:** `tcg/data/__init__.py` (`create_services`), `tcg/data/_mongo/registry.py` (`CollectionRegistry`).

## CSS Custom Properties + data-theme for Theming

**Decision:** The frontend uses CSS custom properties (`--var-name`) with a `data-theme` attribute on `<html>` for light/dark mode switching. Plotly charts read theme from a JS palette object, not CSS vars.

**Rationale:**
- Plotly.js cannot read CSS custom properties -- it needs explicit color values in its layout config.
- The `useTheme()` hook observes `data-theme` via `MutationObserver` and triggers re-renders.
- `chartTheme.js` defines `DARK_PALETTE` and `LIGHT_PALETTE` as JS objects, and `buildBaseLayout(overrides, theme)` merges the correct palette into the Plotly layout.
- This keeps CSS-based components and Plotly charts in sync with a single source of truth (`data-theme` attribute).

**Locations:** `frontend/src/hooks/useTheme.js`, `frontend/src/utils/chartTheme.js`, `frontend/src/index.css`.

## Backward Processing for Roll Adjustments

**Decision:** Roll adjustments (ratio and difference) process backward from the last roll to the first.

**Rationale:**
- When adjusting by ratio, earlier prices must accumulate ALL subsequent adjustment ratios. Processing forward would require tracking cumulative factors; processing backward naturally cascades: each adjustment multiplies everything before the current roll date, which already includes prior adjustments.
- Same logic applies to additive (difference) adjustment.

**Locations:** `tcg/data/_rolling/adjustment.py`.

## Weight Normalization by Sum of Absolute Values

**Decision:** Portfolio weights are normalized by `sum(|w|)` internally, not `sum(w)`.

**Rationale:**
- Supports long/short portfolios where weights can be negative. `sum(w)` could be zero or negative for a dollar-neutral or net-short portfolio.
- User-provided weights like `{SPX: 0.6, VIX: -0.4}` are normalized to `{SPX: 0.6, VIX: -0.4}` (sum of abs = 1.0, already normalized). Weights like `{SPX: 3, VIX: -2}` become `{SPX: 0.6, VIX: -0.4}`.

**Locations:** `tcg/engine/metrics.py` (`compute_weighted_portfolio`).

## Default indicator library pruning (2026-04)

**Decision:** The default indicator registry was restructured to 5 canonical logical indicators (10 JS entries) + 13 legacy Java ports = 23 entries total. The previous library (22 entries, many near-duplicates) is superseded. Four Java indicator classes were audited and documented as explicitly not-shipped.

**Rationale:**
- The previous library had heavy overlap: `rolling-min` and `rolling-max` are one `trailing-extreme` with a direction flag; `log-return` and `simple-return` are one-liners the user can write directly in the sandbox; `rolling-zscore` is a user-derivable composition of mean and stddev; `dema`, `tema`, `kama` are niche EMA variants that belong in a user's own library, not in the default starting set.
- The legacy Java simulator contains 17 hand-tuned indicators that encode domain experience (swing-pivot rules, engulfment breakout heuristics, percentile bands) that are genuinely hard to rederive from scratch. Shipping these as defaults surfaces decades of accumulated signal-design choices to every new user without them needing to port Java.
- The two-tier split (canonical + legacy port) is explicit in `docs/indicators.md` so users understand the provenance and the behavioural guarantees.

### Dropped entries (12)

Twelve entries from the previous library were removed:

- `wma` — linear-weighted MA; derivable, not canonical.
- `dema` — niche EMA variant (Mulloy 1994).
- `tema` — niche EMA variant.
- `kama` — specialist adaptive MA; not in the universal starting set.
- `roc` — rate-of-change; subsumed by `slope-acceleration`.
- `momentum` — `x_t - x_{t-n}`; subsumed by `weighted-impetus` (telescoping identity).
- `rolling-stddev` — subsumed partially by `slope-statistics` (stddev of returns).
- `rolling-zscore` — user-derivable from mean + stddev.
- `rolling-min` — subsumed by `trailing-extreme` with `use_min = True`.
- `rolling-max` — subsumed by `trailing-extreme` with `use_min = False`.
- `log-return` — one-line sandbox expression; `centred-slope` is the closest symmetric variant.
- `simple-return` — channel 1 of `slope-acceleration`.

### Not-shipped Java ports (4) — rationale

Four Java indicator classes from `trajectoirecap.platform.parent/simulator/src/main/java/com/simulator/indicator/` were audited and deliberately not shipped as JS defaults:

- **`IndicatorSimple` (passthrough)** — identity adapter in the Java two-stage Filter/Indicator architecture. Not practitioner-facing; shipping it as a JS default would be visual clutter. The JS sandbox contract takes raw series directly so no adapter is needed.
- **`IndicatorOperation` (scalar-transform: add / sub / mul / div by constant)** — higher-order composition over an upstream indicator. Does not fit the JS `compute(series, ...)` contract which consumes raw OHLCV only, not upstream indicators. Trivially expressed inline in any sandbox cell (`out = my_indicator + k`).
- **`IndicatorBollingerBands` (single-class multi-channel port)** — redundant with the canonical 4-file Bollinger bundle already shipped. The 4-file form is required by the scalar-per-bar contract; porting the Java single-class form adds maintenance cost with no new capability.
- **`IndicatorFilterHistory` (rolling-window history)** — emits the whole rolling window of values as an array per bar. Non-scalar output violates the `compute` contract (`compute` must return a 1-D array aligned to the input length, not a 2-D lookback buffer). Infrastructure primitive, not an end-user indicator.

### Spec corrections surfaced to user at delivery

Three terminology / semantics corrections were made during the rework and are worth flagging:

- **ATR uses arithmetic mean, not Wilder smoothing.** `AtrSequential.java:114` is `currentValue = sum / numPeriods`. The textbook Wilder ATR is an exponential smoothing, but the legacy Java engine is SMA-of-TR. The port preserves the Java truth; the `atr.js` `doc` field flags the divergence and points to the Java line. Users preferring Wilder's recursion can swap the source (one-line change).
- **"Weighted Impetus", not "Volume-Weighted Impetus".** The Java class `IndicatorWeightedImpetus` does not reference volume anywhere; the "weighting" is by signed magnitude of the price change, not by volume. A "Volume-Weighted Impetus" would mislead. The user-suggested name during Wave 1 research was therefore rejected.
- **"Swing Pivots", not "Donchian Channel".** The Java class `IndicatorMinMax` detects discrete local extrema with a confirmation delay — a zig-zag / swing-high-low detector. A Donchian Channel is a continuous rolling-high / rolling-low envelope (which is what `trailing-extreme` provides). Naming `IndicatorMinMax` "Donchian Channel" would mislead.

### `absolute-mean` corrects a Java latent bug

`IndicatorAvg.java` seeds `sum += Math.abs(currentValue)` in init but the streaming step runs `sum += currentFilterValue; sum -= queue.poll()` on signed values. The result is an inconsistent mixing of abs and signed behaviour that only matches the user's mental model when the input is positive. The port applies `abs` in both phases so the indicator is consistently "rolling mean of the absolute value", which is what practitioners reading the class name expect.

**Locations:** `frontend/src/pages/Indicators/defaults/*.js`, `frontend/src/pages/Indicators/defaultIndicators.js`, `docs/indicators.md`.

## Default indicator library further prune (2026-05)

**Decision:** The default indicator registry is reduced from 24 entries (post-2026-04 rework) to 9. Most legacy-port entries and the Bollinger family are dropped; the canonical SMA / EMA / RSI / MACD-triple block plus `historical-vol`, `swing-pivots`, and `percentile-filtered-return` survive. The dropped indicators remain available to users as custom indicators (they can be re-implemented in the sandbox by anyone who needs them) — they are simply no longer shipped pre-loaded in the default library.

**Survivors (9):** `sma`, `ema`, `rsi`, `macd-line`, `macd-signal`, `macd-histogram`, `historical-vol`, `swing-pivots`, `percentile-filtered-return`.

**Dropped (15):** `absolute-mean`, `atr`, `bollinger-{lower,middle,upper,percent-b}`, `centred-slope`, `engulfment-{pattern,exit}`, `impetus`, `rolling-percentile-bands`, `slope-acceleration`, `slope-statistics`, `trailing-extreme`, `weighted-impetus`.

**Rationale:**
- The post-2026-04 library was designed under a "ship the legacy Java domain knowledge to every new user" mandate. The trade-off (a thicker default surface in exchange for embedded heuristics) was rejected on review: a new user benefits more from a small, instantly-recognisable starting set than from a long list of niche ports each requiring its own `doc` field to explain.
- The Bollinger quad is dropped along with the legacy ports for consistency. SMA + sample-stddev is a few lines in the sandbox; users who need bands can ship their own.
- `percentile-filtered-return` is kept intentionally even though `rolling-percentile-bands` is dropped — the two are not duplicates. The bands compute a percentile *of the close series itself*; `percentile-filtered-return` computes a rolling percentile *of a derived mean-reversion residual* `(close - SMA) / SMA`. The latter is a non-trivial composition that is genuinely useful as a default.
- `historical-vol` is added back into the documented library (the 2026-04 doc had it shipped but undocumented).

**Locations:** `frontend/src/pages/Indicators/defaults/*.js`, `frontend/src/pages/Indicators/defaultIndicators.js`, `frontend/src/pages/Indicators/defaultIndicators.test.js`, `tests/engine/test_default_indicators_library.py`, `docs/indicators.md`.

## Composed portfolios: the fund-of-funds model (2026-07)

**Decision:** A composed portfolio is a *fund of funds*. Each referenced
sub-portfolio is a self-contained strategy that rebalances internally over its
**own full history**; its equity curve is a fixed, cacheable object. The composed
portfolio treats each child equity curve as a synthetic price series and
rebalances only the **allocations** across them at the parent frequency. It does
**not** re-run each child over the parent's narrowed (intersection) window.

**Mechanism.** A composed leg's child is computed over the child's OWN resolved
range — the frontend inlines that range (the child's `overlapRange`, exactly what
a standalone compute of the child would send) into `leg.portfolio.start/end`, and
the backend `_evaluate_portfolio_leg` builds the child sub-request from
`child.start`/`child.end` via `_child_request` (never the parent range). Because
the child sub-body is then **byte-identical** to a standalone compute of that
child, `_portfolio_cache_key` collides and the two share the on-disk cache entry:
a composed portfolio whose sub-portfolios were each already computed is served
entirely from cache (instant, zero heavy recompute). This is the **key-parity
invariant** and is asserted by a dedicated test.

**Why this is more correct.** The engine aggregates children as synthetic close
series and works in returns, which are scale- and start-invariant, so using each
child's real ongoing path (rather than re-anchoring it to the composed start)
reflects what actually happened to each strategy. The previous behaviour
recomputed every child over the parent's intersection range, which (a) re-anchored
each child at the composed start — a subtly different, less faithful curve when
children have differing coverage — and (b) produced a child body that never
matched a standalone compute, so no child was ever reused from cache.

**Invalidation.** The change is compute-affecting for composed portfolios, so
`COMPUTE_VERSION` was bumped `0.1.11 → 0.1.12`; composed entries cached under the
old re-anchor model are namespaced out and can never be served.

**A read-only cache-get endpoint** (`POST /api/portfolio/cache/get`) returns a
cached result without ever computing (miss → `{result: null}`), backing an
auto-display UX: selecting a portfolio whose current config is cached shows its
result with no Compute click and no risk of triggering a long compute.

**Locations:** `tcg/core/api/portfolio.py` (`_child_request`,
`_evaluate_portfolio_leg`, `/cache/get`, `COMPUTE_VERSION`),
`frontend/src/pages/Portfolio/computeBodyBuilder.js`,
`frontend/src/pages/Portfolio/resolvePortfolioRange.js` (`resolveChildRanges`),
`frontend/src/pages/Portfolio/usePortfolio.js` (auto-display).
