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

**Decision:** Roll adjustments (proportional and difference) process backward from the last roll to the first.

**Rationale:**
- When adjusting proportionally, earlier prices must accumulate ALL subsequent adjustment ratios. Processing forward would require tracking cumulative factors; processing backward naturally cascades: each adjustment multiplies everything before the current roll date, which already includes prior adjustments.
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
