# P2 — Data

Goal: load every series the spec needs, validate it, cache it, and produce `data/data_summary.json`. No backtesting yet.

## Required-data table by strategy class

| `signals.type`   | Required series                                      |
|------------------|------------------------------------------------------|
| indicator-based, asset_class=INDEX | `INDEX/<id>` daily OHLCV                  |
| indicator-based, asset_class=ETF   | `ETF/<id>` daily OHLCV                    |
| indicator-based, asset_class=FUTURE| continuous front-month + per-contract     |
| option-leg                         | `OPT_<root>` chain over date range + per-contract series for each leg the agent will trade |
| composite                          | union of the above per leg                |
| (any)                              | benchmark series (`benchmark.instrument_id`) |

## Loading

Use snippets first. The agent maps `(asset_class, instrument_id)` to a snippet:

- INDEX -> `snippets/fetch_index_bars.py`
- ETF -> `snippets/fetch_etf_bars.py`
- FUTURE continuous -> `snippets/fetch_futures_continuous.py`
- OPTION chain -> `snippets/fetch_options_chain.py`
- OPTION single contract -> `snippets/fetch_option_contract.py`

Always cache results to `data/<series-id>.npz` (NumPy `np.savez` with arrays `dates`, `open`, `high`, `low`, `close`, `volume` for bars; chain caches use `data/chain_<root>_<startYYYYMMDD>_<endYYYYMMDD>.npz`).

## Integrity checks

For every loaded bar series:

1. `len(dates) >= 2`. Else fail with `INSUFFICIENT_DATA`.
2. dates are strictly increasing int64 YYYYMMDD.
3. Compute gap count: number of trading days between `dates[0]` and `dates[-1]` (use `pandas_market_calendars` XNYS or the relevant exchange) minus `len(dates)`.
4. Compute NaN count per OHLCV column.
5. If `gaps > 0.05 * expected_days` OR `nan_close > 0`, treat as a data-quality probe; log to `PROBLEMS.md` and ask the user whether to proceed, narrow the date range, or change provider.

For options chains: validate per-contract `expiration` ordering and that the requested DTE window contains at least one contract per requested date.

## Focused option queries: don't load all strikes

The OPT_SP_500 collection has ~4,000 unique strikes. A 10-delta short-put strategy only ever picks contracts within ~30% of spot, so loading all strikes wastes 90% of the wire bytes and ~10x the cursor time. A 4-year SPX backtest with no strike band loads in ~21 min; with a ±30% strike band around spot it loads in ~2 min.

**Principle:** every multi-year option chain load must push a server-side strike band (and, when known, an `expiration_cycle`).

`chain_args_from_spec` derives both automatically when you pass a `spot_hint`:

```python
# Get a representative spot ONCE per workspace.
bars = data_load.load_index_bars_sync(db, "SPX", start=20230101, end=20230102)
spot_hint = float(bars.close[-1])

# chain_args_from_spec auto-derives:
#   - strike_min / strike_max from contract_selector + spot_hint
#       delta selector  -> ±30% around spot       (most generous)
#       atm selector    -> ±15% around spot
#       pct_offset      -> ±15% around spot*(1+pct)
#       moneyness       -> ±15% around spot*moneyness
#   - expiration_cycle from expiry_selector.kind
#       weekly  -> "W"
#       monthly -> "M"
#       dte     -> None (any cycle)
kwargs = options.chain_args_from_spec(SPEC, spot_hint=spot_hint)
chain = options.load_chain(db, **kwargs)
```

Without `spot_hint`, `chain_args_from_spec` emits a `UserWarning` and leaves the strike band unbounded. That is the documented escape hatch when you genuinely need every strike — but for delta/ATM/moneyness/pct-offset strategies it is a slow-load smell. The canonical `snippets/fetch_options_chain.py` always supplies one.

For specs whose `expiry_selector.kind: dte` is known to track standard third-Friday monthlies (e.g. SPX 30-DTE), pass `expiration_cycle="M"` explicitly to `load_chain` for the extra ~4-5x narrowing.

### Severity contract — PASS / WARN / FAIL

`lib.validate.IntegrityReport` carries a three-level `severity` property and a
`summary_line()` that prints one of three prefixes:

| Severity | `ok` | `warnings` | Pipeline action | Triggers |
|----------|------|------------|-----------------|----------|
| **`PASS`** | True | empty | Proceed silently | All checks clean. |
| **`WARN`** | True | non-empty | **Proceed with caveat.** Surface the warning in agent status and (optionally) in `PROBLEMS.md`, but do NOT abort. | Calendar-day gaps within tolerance (`gaps / expected_n <= 10%`). The 10% cap is a hard ceiling above the 5% data-quality probe trigger — a 5%-9% gap chain is operationally fine. |
| **`FAIL`** | False | (irrelevant) | **Abort.** Log to `PROBLEMS.md`, do not advance to P3. | Empty series, NaN closes, non-monotone dates, schema/serialization breaks, or calendar gap fraction > 10%. |

When you read `report.summary_line()`:
- Starts with `OK:` -> `PASS`, proceed.
- Starts with `WARN:` -> `WARN`, proceed but flag the caveat to the user.
- Starts with `FAIL:` -> `FAIL`, abort and log.

Snippet pattern (canonical):

```python
report = validate.chain_integrity(chain, ...)
print(report.summary_line())
if report.severity == "FAIL":
    raise SystemExit(f"data integrity failed: {report.failures}")
elif report.severity == "WARN":
    print(f"[chain] proceeding with caveats: {list(report.warnings)}")
# PASS: silent.
```

**Do not treat WARN as FAIL.** Prior to this contract, calendar-gap rows were always pushed into `failures`; a 5%-gap chain (operationally fine) was emitted as `FAIL`, confusing fresh agents into aborting healthy pipelines. WARN is the soft path; FAIL is the abort path.

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
