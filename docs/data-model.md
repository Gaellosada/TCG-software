# Data Model and MongoDB Schema

## Core Python Types

### PriceSeries

Columnar OHLCV data. All arrays have identical length.

```python
@dataclass(frozen=True)
class PriceSeries:
    dates: NDArray[int64]      # YYYYMMDD integers
    open: NDArray[float64]
    high: NDArray[float64]
    low: NDArray[float64]
    close: NDArray[float64]
    volume: NDArray[float64]
```

Dates are YYYYMMDD integers (e.g., `20240115`), not datetime objects. See [design-decisions.md](design-decisions.md) for rationale.

### InstrumentId

```python
@dataclass(frozen=True)
class InstrumentId:
    symbol: str              # Serialized _id from MongoDB
    asset_class: AssetClass  # equity, index, future
    collection: str          # MongoDB collection name
    exchange: str | None
```

`asset_class` is inferred from collection prefix at `parse_instrument_id` time.

### ContractPriceData

Single futures contract with its expiration and price data:

```python
@dataclass(frozen=True)
class ContractPriceData:
    contract_id: str         # Serialized _id
    expiration: int          # YYYYMMDD integer
    prices: PriceSeries
```

### ContinuousRollConfig

Configuration for building a continuous futures series:

```python
@dataclass(frozen=True)
class ContinuousRollConfig:
    strategy: RollStrategy          # Currently only FRONT_MONTH
    adjustment: AdjustmentMethod    # none, ratio, difference
    cycle: str | None               # e.g., "HMUZ" for quarterly
    roll_offset_days: int           # Days before expiration to roll
```

### ContinuousSeries

Output of the rolling engine:

```python
@dataclass(frozen=True)
class ContinuousSeries:
    collection: str
    roll_config: ContinuousRollConfig
    prices: PriceSeries
    roll_dates: tuple[int, ...]    # YYYYMMDD at each roll boundary
    contracts: tuple[str, ...]     # Ordered contract IDs used
```

### ContinuousLegSpec

Pairs a `ContinuousRollConfig` with its collection, used when constructing multi-instrument portfolios:

```python
@dataclass(frozen=True)
class ContinuousLegSpec:
    collection: str
    roll_config: ContinuousRollConfig
```

## MongoDB Document Structure

### Database

Target database: `tcg-instrument` (legacy name, configured via `MONGO_DB_NAME` env var).

### Collections

Collection names follow a prefix convention from the legacy Java platform:

| Prefix   | Asset Class | Examples                |
|----------|-------------|-------------------------|
| `INDEX`  | INDEX       | Single collection       |
| `ETF`    | EQUITY      | Single collection       |
| `FUND`   | EQUITY      | Single collection       |
| `FOREX`  | EQUITY      | Single collection       |
| `FUT_*`  | FUTURE      | FUT_VIX, FUT_SP_500     |
| `OPT_*`  | Options     | OPT_VIX (deferred, not active) |

Unknown collections (e.g., MongoDB system collections) are silently ignored by `CollectionRegistry`.

### Document `_id` Types

Legacy polymorphism -- the Java platform stored `_id` as different types depending on the collection:

- **ObjectId** -- Standard MongoDB auto-generated ID.
- **String** -- Human-readable symbol (e.g., `"SPX"`).
- **Composite dict** -- Compound keys like `{"symbol": "VIX", "expiry": "2024-01"}`.

The `serialize_doc_id()` function normalizes all types to strings:
- ObjectId: `str(oid)`
- Dict: `"key1=val1|key2=val2"` (sorted keys for determinism)
- Other: `str(value)`

The `deserialize_doc_id()` function produces candidate `_id` values to try when querying:
1. Composite dict (if string matches `key=val|key=val` pattern)
2. ObjectId (if string is a valid ObjectId)
3. Raw string

`MongoInstrumentReader._find_document()` tries each candidate until a match is found.

### `eodDatas` Format

Price data is stored in an embedded `eodDatas` field, structured as:

```json
{
  "_id": "SPX",
  "eodDatas": {
    "YAHOO": [
      {"date": 20240115, "open": 4780.0, "high": 4802.0, "low": 4756.0, "close": 4783.0, "volume": 3200000000},
      {"date": 20240116, "open": 4783.0, "high": 4791.0, ...}
    ],
    "IVOLATILITY": [
      {"date": 20240115, ...}
    ]
  }
}
```

- Top-level keys are provider names (e.g., `"YAHOO"`, `"IVOLATILITY"`).
- Each provider maps to a list of bar objects with `date`, `open`, `high`, `low`, `close`, `volume`.
- `date` is a YYYYMMDD integer.
- If no provider is specified in a query, the first available provider is used.
- Bars are sorted by date (ascending) during extraction.

### Futures-Specific Fields

Futures documents additionally have:

- `expiration` -- Contract expiration date. Stored as datetime, ISO string, or YYYYMMDD integer (legacy inconsistency). Parsed by `_parse_expiration()` which handles all three formats.
- `expirationCycle` -- String identifying the contract cycle (e.g., `"HMUZ"` for quarterly Mar/Jun/Sep/Dec, `"FGHJKMNQUVXZ"` for monthly). Used to filter contracts when building continuous series.

### Excluded Heavy Fields

Listing queries exclude these fields for performance:

- `eodDatas` -- Can be very large (years of daily bars).
- `intradayDatas` -- Intraday bars (not used yet).
- `eodGreeks` -- Options Greeks data (not used yet).

## NaN Sanitization

All data leaving the `_mongo/` adapter is guaranteed NaN-free:

1. **Critical field (close):** Bar is dropped entirely if close is NaN or missing. A warning is logged.
2. **Non-critical fields (open, high, low, volume):** NaN/None replaced with `0.0`.

This follows the architecture design guide (section 3.10): sanitize at the adapter boundary so downstream code never encounters NaN from the data source.

## Cache Layer

`LRUCache` sits in `DefaultMarketDataService`, keyed by `"collection:instrument_id:provider:start:end"` for regular prices and `"continuous:collection:strategy:adjustment:cycle:roll_offset:start:end"` for continuous series. Default capacity: 200 entries. Avoids redundant MongoDB queries within a session.
