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
