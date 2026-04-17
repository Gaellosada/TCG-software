# Continuous Futures Rolling System

## Overview

Builds a single continuous price series from multiple individual futures contracts. Located in `tcg/data/_rolling/`.

Entry point: `ContinuousSeriesBuilder.build()` in `stitcher.py`.

## Three-Phase Algorithm

### Phase 1: Compute Roll Dates (`calendar.py`)

`compute_roll_dates(contracts, strategy, roll_offset_days)` determines when to switch from one contract to the next.

**FRONT_MONTH strategy** (currently the only one):
- Roll date = expiration date of the outgoing contract.
- If `roll_offset_days > 0`, the roll date is shifted earlier by that many calendar days.
- Returns one date per roll boundary (len = len(contracts) - 1).
- Contracts must be sorted by expiration (ascending).

```
roll_offset_days=0:  roll at expiration
roll_offset_days=5:  roll 5 calendar days before expiration
```

### Phase 2: Trim Overlaps (`calendar.py`)

`trim_overlaps(contracts, roll_dates)` ensures each contract contributes only its designated date range:

1. **Strip zero-close rows** -- Unlisted/untraded dates often have close=0 in legacy data.
2. **Apply roll boundary** -- Contract `i` keeps dates `<= roll_dates[i]`. The last contract keeps all its data.
3. **Drop empty contracts** -- Contracts with no remaining data after filtering are excluded.

### Phase 3: Concatenate and Adjust (`stitcher.py`, `adjustment.py`)

#### Concatenation with Deduplication

`ContinuousSeriesBuilder._concatenate()`:
- Merges all trimmed contracts into a single series.
- If two contracts have data on the same date, the **later contract's data wins**.
- Tracks which contracts actually contribute rows ("surviving contracts") -- some may be entirely subsumed.
- Returns: concatenated `PriceSeries`, actual roll dates (first date of each new contract segment), surviving contract indices.

#### Adjustment Methods

Applied after concatenation, processing **backward** from the last roll to the first so adjustments cascade correctly.

**NONE** (`AdjustmentMethod.NONE`):
- Raw concatenation. No price adjustment. Gaps appear at roll boundaries.

**PROPORTIONAL** (`adjust_proportional` in `adjustment.py`):
- At each roll date, compute `ratio = new_close / old_close`.
- Multiply all OHLC prices **before** the roll date by this ratio.
- Processing backward means earlier prices accumulate all subsequent ratios.
- Skips roll boundaries where either close is 0 (logs a warning, leaves gap unadjusted).
- Volume is never adjusted.

**DIFFERENCE** (`adjust_difference` in `adjustment.py`):
- At each roll date, compute `diff = new_close - old_close`.
- Add this difference to all OHLC prices **before** the roll date.
- Same backward processing, same zero-price skip behavior.
- Volume is never adjusted.

## Roll Offset Parameter

`roll_offset_days` in `ContinuousRollConfig` shifts the roll date earlier by N calendar days before expiration.

Use case: avoid trading the contract in its final days when liquidity dries up and bid-ask spreads widen.

Implementation: `date_to_int(int_to_date(expiration) - timedelta(days=roll_offset_days))`. This performs calendar-day arithmetic via Python `datetime.date`, then converts back to YYYYMMDD integer.

Note: the distance calculation in `_find_closest_date_idx` operates on YYYYMMDD integers, which is non-uniform across month boundaries. The comment in `adjustment.py` documents this known approximation -- in practice the price difference between adjacent candidates is negligible.

## Roll Strategy: FRONT_MONTH

The only currently implemented strategy. Rolls at (or before) each contract's expiration to the next contract by expiration order.

Contracts are filtered by `expirationCycle` if specified in the config (e.g., `"HMUZ"` keeps only quarterly contracts). This filtering happens at the MongoDB query level in `MongoInstrumentReader.fetch_futures_contracts()`.

## Integration with Service Layer

`DefaultMarketDataService.get_continuous()`:
1. Validates collection is a futures collection (`FUT_` prefix).
2. Checks LRU cache.
3. Fetches contracts from MongoDB (filtered by cycle, sorted by expiration).
4. Calls `ContinuousSeriesBuilder.build()`.
5. Applies optional date range filter to the result.
6. Caches and returns.

## File Locations

| File | Purpose |
|------|---------|
| `tcg/data/_rolling/__init__.py` | Re-exports `ContinuousSeriesBuilder` |
| `tcg/data/_rolling/calendar.py` | `compute_roll_dates()`, `trim_overlaps()` |
| `tcg/data/_rolling/adjustment.py` | `adjust_proportional()`, `adjust_difference()` |
| `tcg/data/_rolling/stitcher.py` | `ContinuousSeriesBuilder` (orchestrator) |
| `tcg/data/_mongo/instruments.py` | `fetch_futures_contracts()`, `fetch_available_cycles()` |
| `tcg/data/service.py` | `get_continuous()`, `get_available_cycles()` |
| `tcg/core/api/data.py` | `/api/data/continuous/{collection}` endpoint |
| `tests/unit/test_rolling.py` | Unit tests for the rolling pipeline |

## Test Coverage

`tests/unit/test_rolling.py` covers:
- Single contract (no rolling needed)
- Multi-contract concatenation (no adjustment)
- Proportional and difference adjustment
- Roll offset behavior
- Zero-close row stripping
- Empty contract handling
- Contract ordering validation
- Date deduplication (later contract wins)
