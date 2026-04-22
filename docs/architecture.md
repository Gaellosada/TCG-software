# Architecture

## Backend Module Layout

### `tcg/types/` -- Domain types (zero dependencies within tcg)

Pure data definitions. Nothing in types/ imports from other tcg modules.

- `market.py` -- Core market types: `PriceSeries` (columnar OHLCV with YYYYMMDD int dates), `InstrumentId`, `ContractPriceData`, `ContinuousRollConfig`, `ContinuousSeries`, `ContinuousLegSpec`. Enums: `AssetClass`, `RollStrategy`, `AdjustmentMethod`.
- `portfolio.py` -- `PortfolioSpec`, `PortfolioComputeResult`, `RebalanceFreq` enum (none/daily/weekly/monthly/quarterly/annually).
- `metrics.py` -- `MetricsSuite` dataclass: total return, annualized return, Sharpe, Sortino, max drawdown, Calmar, CVaR 5%, volatility, time underwater, trade count, win rate.
- `simulation.py` -- `SimConfig`, `SimulationRequest`, `Trade`, `EquityCurve`, `SimResult`. Types for future strategy simulation engine.
- `strategy.py` -- `StrategyDefinition`, `StrategyMeta`, `StrategyStage` lifecycle enum (trial/validation/prod/archive).
- `provenance.py` -- `Provenance`, `DataVersion`, `ResultSource`. Every result tracks its origin (legacy/precomputed/on-the-fly).
- `errors.py` -- `TCGError` base with `error_type` field. Subclasses: `DataNotFoundError`, `DataAccessError`, `ValidationError`, `StrategyExecutionError`, `SimulationError`.
- `config.py` -- `MongoConfig` (uri + db_name).
- `common.py` -- `PaginatedResult[T]` generic.

### `tcg/data/` -- Data access layer

Encapsulates all MongoDB complexity. Public interface is three protocols (`MarketDataService`, `StrategyStore`, `ResultStore`) plus a `create_services()` factory.

- `protocols.py` -- Protocol definitions. Consumers depend only on these.
- `service.py` -- `DefaultMarketDataService`: composes `MongoInstrumentReader`, `CollectionRegistry`, `LRUCache`, and `ContinuousSeriesBuilder`. Implements all `MarketDataService` methods including `get_aligned_prices()` for multi-instrument date intersection (inner join).
- `_cache.py` -- `LRUCache` (OrderedDict-based, default 200 entries). Thread-safety not needed (Motor is single-threaded per event loop).
- `_utils.py` -- Date conversion utilities: `date_to_int`, `int_to_date`, `int_to_iso`, `filter_date_range`.
- `__init__.py` -- `create_services()` factory. Discovers collections from MongoDB at startup, builds `CollectionRegistry`, wires `DefaultMarketDataService`.

#### `tcg/data/_mongo/` -- MongoDB adapter

- `client.py` -- Connection management (if present).
- `registry.py` -- `CollectionRegistry`: classifies collection names by prefix (FUT_, OPT_, INDEX, ETF, FUND, FOREX). Discovered dynamically at startup from `db.list_collection_names()`.
- `instruments.py` -- `MongoInstrumentReader`: listing instruments (with pagination), reading prices, fetching futures contracts (sorted by expiration), fetching available expiration cycles. Handles legacy `_id` polymorphism via `_find_document()`.
- `helpers.py` -- Document parsing: `serialize_doc_id` / `deserialize_doc_id` (handles ObjectId, string, composite dict), `extract_price_data` (parses `eodDatas` map), `parse_instrument_id`. NaN sanitization at adapter boundary: bars with NaN close are dropped, NaN in non-critical fields (open, high, low, volume) replaced with 0.0.

#### `tcg/data/_rolling/` -- Continuous futures rolling

- `calendar.py` -- `compute_roll_dates()` (FRONT_MONTH: roll at expiration minus offset), `trim_overlaps()` (truncate each contract at its roll boundary, strip zero-close rows).
- `adjustment.py` -- `adjust_ratio()` (ratio at each roll, cascading backward), `adjust_difference()` (additive, cascading backward). Both skip zero-price contracts with a warning.
- `stitcher.py` -- `ContinuousSeriesBuilder.build()`: orchestrates the three-phase pipeline (compute roll dates, trim overlaps, apply adjustment). Handles deduplication (later contract wins on date conflicts).

### `tcg/engine/` -- Computation engine

- `metrics.py` -- Pure NumPy computation:
  - `compute_daily_returns()` -- normal or log returns from close prices.
  - `compute_equity_curve()` -- compound returns into equity values.
  - `compute_weighted_portfolio()` -- main entry point. Supports buy-and-hold, daily rebalance, or periodic rebalance (weekly/monthly/quarterly/annually). Handles short positions (negative weights). Returns `PortfolioComputeResult`.
  - `compute_metrics()` -- `MetricsSuite` from an equity curve (CAGR, Sharpe, Sortino, max drawdown, Calmar, CVaR 5%, volatility).
  - `aggregate_returns()` -- bucket daily returns into monthly or yearly periods.
- No dependency on `tcg.data` or `tcg.core` -- only `tcg.types`.

### `tcg/core/` -- Application layer

- `app.py` -- FastAPI app factory (`create_app()`). Lifespan connects to MongoDB, builds services, stores on `app.state`. CORS middleware (dev-only). Error handler maps `TCGError` to JSON responses. Includes data and portfolio routers.
- `config.py` -- `load_config()`: reads `.env` file, falls back to environment variables, then defaults.
- `api/data.py` -- Data router (`/api/data/`): `GET /collections`, `GET /continuous/{collection}`, `GET /continuous/{collection}/cycles`, `GET /{collection}`, `GET /{collection}/{instrument_id}`. Validates enums, parses dates, delegates to `MarketDataService`.
- `api/portfolio.py` -- Portfolio router (`/api/portfolio/`): `POST /compute`. Accepts `PortfolioRequest` (legs, weights, rebalance freq, return type, date range). Resolves legs to `InstrumentId` or `ContinuousLegSpec`, fetches aligned prices, computes portfolio, returns equity curves + metrics + aggregated returns + rebalance dates + full date range.
- `api/errors.py` -- `tcg_error_handler`: converts `TCGError` exceptions to structured JSON error responses.

## Frontend Structure

### Pages

- **Data** (`/data`) -- Browse collections by category (INDEX, ETF, FUND, FOREX, FUT_*). View instrument price charts (OHLC candlestick + volume). View continuous futures series with roll strategy, adjustment method, cycle filter, and roll offset controls.
- **Portfolio** (`/portfolio`) -- Build weighted portfolios from multiple instruments/continuous series. Configure rebalance frequency and return type. View equity curves, per-leg breakdown, metrics table, monthly/yearly return grids. Time range slider for date filtering.
- **Research** (`/research`) -- Placeholder for future strategy research features.
- **Settings** (`/settings`) -- Theme toggle (light/dark).
- **Help** (`/help`) -- Usage documentation.

### Component Architecture

- `Sidebar` -- Collapsible navigation. State persisted in localStorage.
- `PageContainer` -- Layout wrapper for page content.
- `ErrorBoundary` -- React error boundary wrapping each page.
- `Chart` -- Plotly.js wrapper with theme-aware layout building.
- `PillToggle` -- Segmented control for mutually exclusive options.
- `TimeRangeSlider` -- Date range selector for filtering chart data.

### Hooks

- `useAsync(asyncFn, deps)` -- Generic data fetching with loading/error states and cancellation.
- `useTheme()` -- Tracks `data-theme` attribute on `<html>` via MutationObserver. Returns `'dark'` or `'light'`.
- `useChartPreference()` -- Persists chart display preferences (e.g., OHLC vs. line).
- `usePortfolio()` -- Portfolio page state management (legs, weights, computation).

### API Layer

- `client.js` -- `fetchApi(path, options)`: base fetch wrapper targeting `/api`. Maps HTTP errors to `ApiError` with `errorType` and `message`.
- `data.js` -- `listCollections`, `listInstruments`, `getInstrumentPrices`, `getContinuousSeries`, `getAvailableCycles`.
- `portfolio.js` -- `computePortfolio({ legs, weights, rebalance, returnType, start, end })`.

## Data Flow

```
MongoDB (tcg-instrument)
  |
  v
MongoInstrumentReader (helpers.py: NaN sanitization, ID deserialization)
  |
  v
DefaultMarketDataService (LRU cache, date filtering, alignment)
  |
  v  (for continuous)
ContinuousSeriesBuilder (calendar + adjustment + stitcher)
  |
  v
FastAPI routers (data.py, portfolio.py)
  |
  v  (JSON over HTTP)
React API client (client.js -> data.js / portfolio.js)
  |
  v
React hooks (useAsync, usePortfolio)
  |
  v
Plotly.js charts (theme-aware via useTheme + chartTheme.js)
```

## Key Architectural Decisions

See [design-decisions.md](design-decisions.md) for full rationale on each decision.

- YYYYMMDD integers for dates (not datetime objects)
- Columnar PriceSeries (NumPy arrays, not row-based)
- LRU cache at service layer (not at MongoDB or API layer)
- NaN sanitization at the MongoDB adapter boundary
- Protocol-based data access (consumers depend on protocols, not implementations)
- Collection discovery at startup (not hardcoded)
- CSS custom properties + data-theme for theming (Plotly reads JS palette objects, not CSS vars)
