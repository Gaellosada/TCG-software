# MongoDB schema reference (`tcg-instrument` database)

Read this once at intake. This is the canonical doc layout per collection,
the canonical provider per collection, and the gotchas that have bitten
strategies before.

Every claim is anchored to `lib/data_load.py` (line numbers as of HEAD
`e5dfa27`) or marked `# UNVERIFIED`. The "real-shape contract" block at the
top of `lib/data_load.py:5-12` is the live-Mongo verification record from
2026-05-02 — that's the source of truth for the per-collection provider.

## Conventions (apply to every collection)

- **All daily date arrays are `int64` `YYYYMMDD`** (e.g., `20240315`). Anchor:
  `lib/data_load.py:1` ("Date convention: YYYYMMDD int64.").
- **Provider auto-resolution**: the lib picks the first canonical provider
  present in `eodDatas`. If your explicit `provider="..."` is missing the
  loader raises `LookupError` listing what's available — anchor: `_pick_provider`
  at `lib/data_load.py:386-423`.
- **Read-only**: every query goes through `lib.mongo.sync_db()` which wraps
  the Motor / pymongo database in `_ReadOnlyDatabase`. Writes raise
  `MongoWriteForbiddenError`. Anchor: `lib/mongo.py:170-323`.

### Provider priority list (canonical defaults)

Anchor: `_PROVIDER_PRIORITY` at `lib/data_load.py:40-46` and
`_PROVIDER_PRIORITY_PREFIX` at `lib/data_load.py:48-52`.

| Collection / prefix | Default provider order |
|---------------------|------------------------|
| `INDEX`             | `YAHOO`                |
| `ETF`               | `YAHOO`                |
| `FUND`              | `BLOOMBERG`            |
| `FOREX`             | `BITSTAMP`, `COINGECKO` |
| `EQUITY`            | `YAHOO`                |
| `OPT_VIX`           | `CBOE`, `IVOLATILITY`  |
| `OPT_*` (other)     | `IVOLATILITY`          |
| `FUT_*`             | `IVOLATILITY`          |

---

## INDEX

Equity / volatility index spot daily bars.

### `_id` shape

Plain string `IND_<NAME>` (uppercase, with `_` separators). Anchor:
`lib/data_load.py:6` ("INDEX `_id=\"IND_SP_500\"`"). Convention-based variants
resolved by `_resolve_doc` (`lib/data_load.py:440-498`): bare `SPX` resolves
to `IND_SP_500`; `VIX` to `IND_VIX`; `NDX` to `IND_NDX_100`; `RUT` to
`IND_RUT_2000`.

Examples: `IND_SP_500`, `IND_VIX`, `IND_NDX_100`, `IND_RUT_2000`.

**VIX term structure:** the live DB ships several VIX maturities — `IND_VIX`
(30-day, the canonical "VIX"), `IND_VIX_9D`, `IND_VIX_3M`, `IND_VIX_6M`,
plus `IND_VVIX` (vol-of-vol). Term-structure / regime strategies usually
pair the standard `IND_VIX` against one of the longer-dated entries.

**Canonical "what's actually in the DB" lookup.** The `KNOWN_*_ROOTS`
tuples in `lib/data/__init__.py` are advisory and may lag the live set.
For the authoritative answer, call the live cached helpers:

  - `lib.data.live_index_roots()` — every `_id` in INDEX
  - `lib.data.live_etf_roots()` — every `_id` in ETF
  - `lib.data.live_option_roots()` — OPT_<ROOT> root suffixes (plug in to `list_option_expiries` / `load_option_chain`)
  - `lib.data.live_futures_roots()` — FUT_<ROOT> root suffixes

Each is `lru_cache`d, so the cost is one query per process.

### Top-level fields

```jsonc
{
  "_id": "IND_SP_500",
  "eodDatas": {
    "YAHOO": [
      {"date": 20240108, "open": 4700.0, "high": 4710.0, "low": 4690.0, "close": 4705.0, "volume": 3.0e9}
      // ... one row per business day, ordered by date ASC
    ]
  }
}
```

Shape verified by `tests/fixtures_real_shape.py:make_real_index_doc` (lines
27-56) which is the regression fixture seeded from the live audit.

### `eodDatas.<provider>` row schema

| Field   | Type    | Notes                                                                |
|---------|---------|----------------------------------------------------------------------|
| date    | int64   | `YYYYMMDD`. Anchor: `_doc_to_price_series` at `lib/data_load.py:276`. |
| open    | float   | Filled to `0.0` if NaN (anchor: `_doc_to_price_series` `_col` at `lib/data_load.py:283-286`). |
| high    | float   | Same NaN-fill rule.                                                  |
| low     | float   | Same.                                                                |
| close   | float   | NaN rows are **dropped** (not zero-filled). Anchor: `lib/data_load.py:278-281`. |
| volume  | float   | Same NaN-fill rule.                                                  |

### Gotchas

- **`type` field is absent.** INDEX docs do NOT carry a `type` key —
  contrast with OPT docs which require it. Anchor: `tests/fixtures_real_shape.py:34-36`.
- **NaN-close rows are dropped silently.** When auditing a series whose
  length is shorter than the calendar window, expect closed-market days to
  appear in raw data with `close=NaN` and be elided. Anchor: `_doc_to_price_series`
  at `lib/data_load.py:278-281`.

---

## ETF

Single-ETF spot daily bars (currently only `ETF_SPY` is verified live).

### `_id` shape

Plain string `ETF_<TICKER>`. Anchor: `lib/data_load.py:6` ("ETF `_id=\"ETF_SPY\"`").

### Document layout

Same shape as INDEX (provider key `YAHOO`, no `type` field). Anchor:
`tests/fixtures_real_shape.py:make_real_etf_doc` (lines 59-68): "Build an
ETF doc shaped like real-Mongo: provider=YAHOO, no `type` field."

### Gotchas

- **`type` field absent**, same as INDEX.
- **EQUITY routes here when ETF collection is the right home** — but the
  lib now exposes a separate `EQUITY` collection (anchor: `_BARS_DISPATCH`
  at `lib/data_load.py:1208-1217`). When EQUITY is missing in production,
  `load_equity_bars` raises a clear `ValueError("collection 'EQUITY' not
  present in DB")`; do NOT silently route equity requests to ETF.

---

## FUND

Internal fund NAV daily series (e.g., `FUND_TRAJECTOIRE_BLACK_TAIL_FEEDER_FUND`).

### `_id` shape

Plain string `FUND_<NAME>`. Anchor: `lib/data_load.py:7` ("FUND `_id=\"FUND_...\"`"
with `providers=[BLOOMBERG]`).

### `eodDatas.BLOOMBERG` row schema

Rows carry **only** `{date, close}` — no OHLV.

Anchor: `tests/fixtures_real_shape.py:make_real_fund_doc` (lines 71-91):
"Build a FUND doc shaped like real-Mongo: provider=BLOOMBERG, rows carry
only {date,close}."

### Gotchas

- **Open / high / low / volume are missing** in the live shape. The loader
  fills them with `0.0` via `_col` (`lib/data_load.py:283-286`); strategies
  that look at intraday range over FUND data are operating on artificial zeros.

---

## FOREX

Crypto / fiat FX pair daily bars. Currently only `BTC_USD` is verified live.

### `_id` shape

Plain string `<BASE>_<QUOTE>`. Anchor: `lib/data_load.py:8` ("FOREX
`_id=\"BTC_USD\"`" with `providers=[BITSTAMP, COINGECKO]`).

### Document layout

```jsonc
{
  "_id": "BTC_USD",
  "eodDatas": {
    "BITSTAMP":   [{"date": 20240108, "open": 43000.0, ...}, ...],
    "COINGECKO":  [{"date": 20240108, "open": 43002.0, ...}, ...]
  }
  // intradayDatas* fields may also be present — not consumed by the lib.
}
```

Anchor: `tests/fixtures_real_shape.py:11-13` ("FOREX `_id=\"BTC_USD\"`
providers=[BITSTAMP, COINGECKO] intradayDatas* fields present").

### Gotchas

- **Two providers**: `_pick_provider` raises `LookupError` if neither
  `BITSTAMP` nor `COINGECKO` is present and no explicit override is given;
  it picks the first present in priority order (`BITSTAMP` then `COINGECKO`).
  Anchor: `_PROVIDER_PRIORITY` at `lib/data_load.py:44`.
- **`intradayDatas*` fields exist on the doc.** The lib does NOT read them
  — strategies wanting intraday FX must query through `raw_db()` directly.

---

## EQUITY (single stocks)

Future-compatible loader. Anchor: `load_equity_bars` at `lib/data_load.py:565-576`.

### `_id` shape

`# UNVERIFIED` (provenance: production MongoDB at 10.0.5.10:27017 was
unreachable from the agent host on 2026-05-05; the convention is inferred
from the `load_equity_bars` docstring at `lib/data_load.py:565-576`).
The production MongoDB does NOT yet ship an `EQUITY` collection; the
loader exists ahead of the data. When called against a DB without that
collection the loader raises `ValueError("collection 'EQUITY' not present
in DB")` (anchor: `_load_simple` at `lib/data_load.py:510-511`).

### Recommended convention

When the collection ships, expect `_id="EQUITY_<TICKER>"` to mirror the
INDEX / ETF / FUND prefix scheme. `# UNVERIFIED` (provenance:
convention-based extrapolation from the existing INDEX/ETF/FUND prefix
scheme; re-verify against a real EQUITY document when the collection
ships).

---

## FUT_\<ROOT\>

Per-contract daily futures bars. One collection per root (e.g., `FUT_SP_500_EMINI`).

### `_id` shape

Plain string of the form `FUT_<ROOT>_<EXPIRATION_YYYYMMDD>` (e.g.,
`FUT_SP_500_EMINI_20250620`). Anchor: `tests/fixtures_real_shape.py:14`
("FUT_*  `_id=\"FUT_SP_500_EMINI_<YYYYMMDD>\"`  expiration:int
providers=[IVOLATILITY]").

### Top-level fields

```jsonc
{
  "_id": "FUT_SP_500_EMINI_20250620",
  "expiration": 20250620,             // int YYYYMMDD or ISO string; tolerated
  "rootUnderlying": "SP_500_EMINI",   // optional; not always present
  "eodDatas": {
    "IVOLATILITY": [{"date": 20240108, "open": ..., ...}, ...]
  }
}
// Note: real FUT_<ROOT> docs do NOT carry an `expirationCycle` field at
// the top level — the cycle letter is derived from the expiration month
// via `_CYCLE_MONTH` (lib/data_load.py:34) and the user-supplied `cycle`
// filter on `load_continuous_futures`. Verified live 2026-05-05 against
// FUT_SP_500.
```

### `eodDatas.IVOLATILITY` row schema

Same as INDEX/ETF (date, open, high, low, close, volume). Anchor:
`_doc_to_price_series` at `lib/data_load.py:237-301` is shape-agnostic over
the provider key.

### Cycle letters → month mapping

`FGHJKMNQUVXZ` = `Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov,
Dec`. Anchor: `CYCLE_LETTERS` at `lib/data_load.py:33` and `_CYCLE_MONTH`
at `lib/data_load.py:34`.

### Roll logic (`load_continuous_futures`)

Anchor: `lib/data_load.py:640-806`. The loader:

1. Lists all contracts via `list_futures_contracts` (`lib/data_load.py:582-598`).
2. Filters to expirations matching the supplied `cycle` string (default
   `"HMUZ"` = quarterly Mar/Jun/Sep/Dec).
3. Picks the front-month contract per date: first contract whose
   effective-roll-date (`expiration - roll_offset_days`) is `>= date` and
   whose series contains `date`. Anchor: `lib/data_load.py:738-754`.
4. Applies adjustment at roll boundaries: `none` (raw), `ratio`
   (multiplicative back-adjust), or `difference` (additive). Anchor:
   `lib/data_load.py:756-774`.

### Gotchas

- **`expiration` field is tolerated as int / ISO string / datetime.**
  Anchor: `_parse_expiration` at `lib/data_load.py:216-234`. Bad values are
  caught and the contract is skipped (NOT a hard fail).
- **Empty contract series**: contracts with no eod rows for the queried
  cycle yield an empty `PriceSeries`. The continuous-futures loader returns
  an empty series with `meta.rolls=[]` rather than raising. Anchor:
  `lib/data_load.py:692-702`.

---

## OPT_\<ROOT\>

Per-contract daily option bars + greeks. One collection per root (e.g.,
`OPT_SP_500`, `OPT_VIX`, `OPT_NASDAQ_100`). Note the collection-name
suffix is NOT the underlying-symbol ticker — the S&P 500 options live in
`OPT_SP_500` (root string `"SP_500"`), not `OPT_SPX`. See
`KNOWN_OPTION_ROOTS` in `lib/data/__init__.py` for the live-verified
list.

### `_id` shape — composite

Composite dict `{"internalSymbol": <str>, "expirationCycle": <str>}`.
Anchor: `lib/data_load.py:9-11` ("OPT docs use composite
`_id={\"internalSymbol\": ..., \"expirationCycle\": ...}`"). When
serialized for use in code paths that want a string id, format is
`internalSymbol=<v>|expirationCycle=<v>` (sorted keys). Anchor:
`_serialize_id` at `lib/data_load.py:304-312`.

Round-trip helpers:
`lib.data.serialize_doc_id({"internalSymbol": "X", "expirationCycle": "Y"})`
↔ `lib.data.deserialize_doc_id("internalSymbol=X|expirationCycle=Y")`.
These are public aliases of `_serialize_id` / `_deserialize_id` in
`lib/data_load.py:304-352` — use them when stashing dict `_id`s as
string keys (e.g., dict keys for caching, set membership). The
underscore-prefixed originals retain internal callers in `data_load.py`.

### Top-level fields

```jsonc
{
  "_id": {"internalSymbol": "SPX 240315 C 5000", "expirationCycle": "H"},
  "contractId": "SPX_20240315_C_5000",     // optional; used by some legacy fixtures
  "expiration": 20240315,                  // int or ISO; tolerated
  "strike": 5000.0,                        // float
  "type": "CALL",                          // "CALL"/"PUT"/"C"/"P"/"c"; canonical
  "optionType": "C",                       // legacy alias; either is accepted
  "rootUnderlying": "SPX",                 // optional
  "underlying": "IND_SP_500",              // optional
  "underlyingSymbol": "SPX",               // optional
  "contractSize": 100,                     // optional
  "currency": "USD",                       // optional
  "eodDatas": {
    "IVOLATILITY": [
      {"date": 20240108, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0,
       "openInterest": 1234, "bid": 12.50, "ask": 12.70}
    ]
  },
  "eodGreeks": {
    "IVOLATILITY": [
      {"date": 20240108, "impliedVolatility": 0.18, "delta": 0.55,
       "gamma": 0.002, "theta": -0.05, "vega": 0.30}
    ]
  },
  "eodDatasStart": {"IVOLATILITY": 20230108},   // window-overlap index
  "eodDatasEnd":   {"IVOLATILITY": 20240315}
}
```

Anchor: `tests/fixtures_real_shape.py:15-20` (the live-audit schema block).

### `eodDatas.<provider>` row schema (OPT-specific)

| Field        | Type    | Notes                                                     |
|--------------|---------|-----------------------------------------------------------|
| date         | int64   | `YYYYMMDD`.                                               |
| open/high/low/close | float | Often `0.0` on untraded days. See gotcha below.    |
| volume       | float   | `0.0` on untraded days.                                   |
| openInterest | float   | Optional; not currently consumed by the lib.              |
| bid          | float   | Real premium (carries the actual quote).                  |
| ask          | float   | Real premium (carries the actual quote).                  |

Anchor: `_row_from_doc` at `lib/data_load.py:914-944` builds an
`OptionDailyRow` from this schema.

### `eodGreeks.<provider>` row schema

| Field             | Notes                                                       |
|-------------------|-------------------------------------------------------------|
| date              | `YYYYMMDD`.                                                 |
| impliedVolatility | Mapped to `OptionDailyRow.iv`. (Legacy alias `iv` accepted.) |
| delta             | Mapped to `OptionDailyRow.delta`.                            |
| gamma             | Mapped to `OptionDailyRow.gamma`.                            |
| theta             | Mapped to `OptionDailyRow.theta`.                            |
| vega              | Mapped to `OptionDailyRow.vega`.                             |
| rho               | Mapped to `OptionDailyRow.rho` (often absent).               |
| spot              | Optional — legacy fixtures stash spot here. Anchor: `_greeks_for_date` at `lib/data_load.py:902-909`. Real-Mongo greeks rows do NOT carry spot. |

### `eodDatasStart` / `eodDatasEnd`

Server-side window-overlap index. `load_option_chain` issues the
fast-path query
`{"eodDatasStart.<provider>": {"$lte": asof}, "eodDatasEnd.<provider>":
{"$gte": asof}}` to prune the contract scan. Anchor:
`lib/data_load.py:1064-1083`. Fixtures missing these fields fall back
to a full scan via `query = {}` (anchor: `lib/data_load.py:1083-1086`).

### Gotchas (the load-bearing ones)

- **`close == 0.0` on untraded days is normal — not a data error.** Real
  options that didn't print on a given day come back with
  `close=high=low=open=volume=0.0` while `bid` and `ask` carry the actual
  premium. Use `OptionDailyRow.mark` (close-if-traded else mid else 0.0)
  for fills, NOT `close` directly. Anchor: `OptionDailyRow.mark` at
  `lib/data_load.py:140-148`; explanatory note at `lib/data_load.py:11-12`.
- **`type` field tolerates several shapes.** `_option_type_from_doc`
  accepts `"CALL"/"PUT"/"C"/"P"/"c"` (case-insensitive, prefix-match). If
  both `type` and `optionType` are missing the loader raises `ValueError`
  rather than defaulting to call. Anchor: `lib/data_load.py:833-852`.
- **OPT_VIX uses CBOE first, then IVOLATILITY.** Anchor:
  `_PROVIDER_PRIORITY_PREFIX[0]` at `lib/data_load.py:49`.
- **`eodGreeks: []` is a real-shape edge.** Some `OPT_VIX` docs ship
  `eodGreeks: []` (empty list, not dict). The loader treats this as "no
  greeks" and continues with the price row. Anchor:
  `_greeks_for_date` at `lib/data_load.py:870-873`.
- **Spot is loaded from the underlying, not the greeks.** Real
  `eodGreeks` rows do NOT carry `spot`; pass `underlying_id=...` to
  `load_option_chain` to populate `OptionChainSnapshot.spot` from the
  matching INDEX doc. Anchor: `lib/data_load.py:1142-1153`.
- **Composite `_id` in distinct queries**: when grouping over `_id`
  through `raw_db()`, expect a dict, not a string. Use
  `_serialize_id(_id)` (`lib/data_load.py:304-312`) to flatten to a
  stable string key when needed.

### Server-side filters (load_option_chain)

Anchor: `load_option_chain` at `lib/data_load.py:1025-1181`.

| Filter                     | Server-side?                            |
|----------------------------|-----------------------------------------|
| `expiration` (exact int)   | Yes — `{"$eq": int(expiration)}`. Anchor: `lib/data_load.py:1069-1070`. |
| `expiration` (>= asof)     | Yes — `{"$gte": asof}` when `expiration` arg is None. Anchor: `lib/data_load.py:1071-1072`. |
| `option_type` (`C`/`P`)    | Yes — case-insensitive regex. Anchor: `lib/data_load.py:1073-1076`. |
| `strike_filter` (lo, hi)   | Yes — `$gte` / `$lte` range. Anchor: `lib/data_load.py:1078-1079`. |
| Window overlap on provider | Yes — `eodDatasStart.<p>` / `eodDatasEnd.<p>`. Anchor: `lib/data_load.py:1066-1068`. |

---

## Cross-collection helpers

### `list_collections_for_root(db, root)`

Returns every collection whose name contains the given root (case-insensitive).
Anchor: `lib/data_load.py:1184-1188`. Useful for discovering the FUT_/OPT_
collections available for a given underlying without a hardcoded enumeration.

### `list_futures_contracts(db, root)`

Lists contract metadata (`contract_id`, `expiration`, `cycle`) sorted by
expiration, projection-pruned to drop `eodDatas` / `intradayDatas`. Anchor:
`lib/data_load.py:582-598`.

### `lib.data.list_instruments(asset_class=...)`

(New in this round.) Process-cached enumeration of `_id` values per
asset-class collection. Returns dicts of the form
`{"id": ..., "asset_class": ..., "providers": [...]}`. Cached per class
via `functools.lru_cache`. Sign 1: passing an unknown class raises
`LookupError` (advisory) but the dispatch loaders themselves accept any
string.

### `lib.data.list_option_expiries(underlying, dte_band=None, as_of=None)`

(New in this round.) Reads `db[OPT_<UNDERLYING>].distinct("expiration")`,
parses to YYYYMMDD ints, sorts ASC. **Hits Mongo on every call** (no
cache) — the live set changes as new contracts ship; `dte_band` is
relative to "today" or `as_of` and is meant to be re-evaluated each pass.

### `lib.data.raw_db()`

Escape hatch for queries the lib doesn't cover. Returns
`mongo.sync_db()` (a `_SyncDB` wrapping `_ReadOnlyDatabase`) — the
proxy stays load-bearing, so writes still raise
`MongoWriteForbiddenError`. Anchor: `lib/mongo.py:424-427`. If you
reach for this often for the same access pattern, the right fix is to
add a first-class `lib.data` helper rather than embedding the raw query
in strategy code.
