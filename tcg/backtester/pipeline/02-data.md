# P2 — Data

Goal: load every series the strategy needs, validate it, cache it, and produce `data/data_summary.json`. No backtesting yet.

## What series to load

Read `strategy.py`'s `META` to determine what to load:

- `META["universe"]` — list of instrument ids to load as `PriceSeries`.
- `META["benchmark"]` — benchmark instrument id (often the same as `universe[0]`).
- `META["asset_class"]` — drives snippet choice (see table below).
- For options strategies (run-shape, `lib.options.build_legs` in `run`): also load the relevant option chain.

| `META.asset_class`   | Required series                                      |
|----------------------|------------------------------------------------------|
| `INDEX`              | `INDEX/<id>` daily OHLCV from MongoDB                |
| `ETF`                | `ETF/<id>` daily OHLCV from MongoDB                  |
| `FUT`                | continuous front-month + per-contract series         |
| `OPT` (run-shape)    | `OPT_<root>` chain over date range + underlying bars |
| (any)                | benchmark bars (`META["benchmark"]`)                  |

## Loading — use snippets first

The agent maps `(asset_class, instrument_id)` to a snippet:

- INDEX → `snippets/fetch_index_bars.py`
- ETF → `snippets/fetch_etf_bars.py`
- FUT (continuous) → `snippets/fetch_futures_continuous.py`
- OPT chain → `snippets/fetch_options_chain.py`
- OPT single contract → `snippets/fetch_option_contract.py`

Always cache results to `data/<id>.npz` (NumPy `np.savez` with arrays `dates`, `open`, `high`, `low`, `close`, `volume` for bars; option chains cache to `data/chain_<root>_<startYYYYMMDD>_<endYYYYMMDD>.pkl`).

## Integrity checks

For every loaded bar series:

1. `len(dates) >= 2`. Else fail with `INSUFFICIENT_DATA`.
2. dates are strictly increasing int64 YYYYMMDD.
3. Compute gap count: trading days between `dates[0]` and `dates[-1]` minus `len(dates)`.
4. Compute NaN count per OHLCV column.
5. If `gaps > 0.05 * expected_days` OR `nan_close > 0`, treat as a data-quality probe; log to `PROBLEMS.md` and ask the user whether to proceed, narrow the date range, or change provider.

For options chains: validate per-contract `expiration` ordering and that the requested DTE window contains at least one contract per requested date.

## Focused option queries: don't load all strikes

The OPT_SP_500 collection has ~4,000 unique strikes. Loading all strikes wastes ~90% of wire bytes and ~10x cursor time. A 4-year SPX chain with no strike band loads in ~21 min; with a ±30% strike band it loads in ~2 min.

**Principle:** every multi-year option chain load must push a server-side strike band.

The canonical snippet (`snippets/fetch_options_chain.py`) handles this. Supply a `SPOT_HINT` — a representative spot price for the mid-window period — and the snippet derives `strike_min` / `strike_max` automatically from the leg's strike specification and calls `options.load_chain` with the band.

Derive the spot hint once per workspace from the underlying bar series:

```python
bars = data_load.load_index_bars_sync(db, "SPX", start=20230101, end=20230102)
spot_hint = float(bars.close[-1])
```

For strategies that load all strikes by design (e.g. vol surface fitting), set `SPOT_HINT = None` — but document the slow-load reason in `ASSUMPTIONS.json`.

For strategies where the DTE band is known to track standard third-Friday monthlies (e.g. SPX 30-DTE), pass `expiration_cycle="M"` explicitly to `load_chain` for an additional ~4-5x narrowing.

### Severity contract — PASS / WARN / FAIL

`lib.validate.IntegrityReport` carries a three-level `severity` property and a `summary_line()` that prints one of three prefixes:

| Severity | `ok` | `warnings` | Pipeline action | Triggers |
|----------|------|------------|-----------------|----------|
| **`PASS`** | True | empty | Proceed silently | All checks clean. |
| **`WARN`** | True | non-empty | **Proceed with caveat.** Surface the warning in agent status and optionally in `PROBLEMS.md`, but do NOT abort. | Calendar-day gaps within tolerance (≤ 10%). |
| **`FAIL`** | False | (irrelevant) | **Abort.** Log to `PROBLEMS.md`, do not advance to P3. | Empty series, NaN closes, non-monotone dates, schema/serialization breaks, or calendar gap fraction > 10%. |

When you read `report.summary_line()`:
- Starts with `OK:` → `PASS`, proceed.
- Starts with `WARN:` → `WARN`, proceed but flag the caveat to the user.
- Starts with `FAIL:` → `FAIL`, abort and log.

Snippet pattern (canonical):

```python
report = validate.bar_integrity(bars)
print(report.summary_line())
if report.severity == "FAIL":
    raise SystemExit(f"data integrity failed: {list(report.failures)}")
elif report.severity == "WARN":
    print(f"[bars] proceeding with caveats: {list(report.warnings)}")
# PASS: silent.
```

**Do not treat WARN as FAIL.** A 5-9% gap bar series is operationally fine; WARN is the soft path; FAIL is the abort path.

## `data_summary.json` schema

```json
{
  "series": [
    {
      "id": "SPX",
      "kind": "INDEX",
      "provider": "YAHOO",
      "start": "2020-01-02",
      "end": "2024-12-31",
      "n_bars": 1259,
      "n_gaps": 0,
      "n_nan_close": 0,
      "cache_path": "data/SPX.npz"
    }
  ],
  "loaded_at": "2026-05-02T14:00:00Z"
}
```

## Output contract

- `data/<id>.npz` files for every series.
- `data/data_summary.json`.
- Append assumption entries for any provider fallbacks or date-range narrowings applied.

Move to P3 immediately on success.
