# Database v2 — series filtering, pagination & intraday grain

**Date:** 2026-08-04
**Status:** approved design, not yet implemented
**Scope:** the Database v2 page (`/api/data-v2`, `frontend/src/pages/DataV2`)

## Problem

Opening an option object on the Database v2 page is unusable for the `EW*` roots. Measured
against the live `dwh`:

| Object | Contracts | Series | API time | Payload |
|---|---|---|---|---|
| `OPT_SP_500_EW2` (id 12) | 96 106 | 200 672 | **33.9 s** | **38.2 MB** |
| `OPT_SP_500_E1A` (id 16) | 4 982 | 9 964 | 3.7 s | 1.8 MB |

Two independent causes stack:

1. **No pagination.** `GET /api/data-v2/objects/{id}` calls `list_contracts` and `list_series`,
   both plain `SELECT … WHERE object_id = %s ORDER BY …` with no `LIMIT`
   (`tcg/data/_sql/instruments_v2.py:125` and `:158`). The whole object ships in one response.
2. **No virtualisation.** `ObjectDetail.jsx:118` renders `seriesList.map(...)` over the full
   list — React attempts 200 672 buttons. No windowing library is present in the project.
   This is what freezes the tab on "Loading object…"; the browser must also parse 38 MB of JSON.

A third defect blocks the actual goal (visualising v2 data). `_ts_to_int`
(`tcg/data/_sql/instruments_v2.py:57`) collapses every `ts` to a `YYYYMMDD` integer. Verified on
serie 1116679 (`bbba·1m`, object 16): the endpoint returns `ts: [20260601, 20260601]` — two
points on the same abscissa, time discarded. Since `fact_bbba` and `fact_bar` are minute grain,
charting any `1m` series today produces a wrong plot. The comment above `_bounds` still asserts
"all v2 ts are 00:00Z today", which stopped being true when the minute facts landed.

Pagination alone would fix the technical failure but not usability: paging 200 672 series 50 at
a time is 4 013 pages. Filtering is what makes the page navigable; pagination is the guardrail
that keeps it from collapsing regardless of what is displayed.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| When filters are chosen | **Filter form first; nothing heavy loads before submit** | No unbounded request can ever be issued |
| Form inputs | **Pre-filled from a cheap aggregate** (expirations, strike bounds) | Keeps the gate while removing guesswork about what exists |
| Filter dimensions | expiration, strike range, call/put, serie type + freq | All four requested |
| After the first submit | **Auto-apply on every change** | Every query is now bounded by `limit` and sub-second, so the gate is only needed for the unfiltered first load |
| Intraday grain | **In scope** | Without it, the `1m` series that hold most of the data still chart wrongly |
| Wire format | **ISO 8601 for intraday, `YYYYMMDD` ints kept for daily** | Preserves the v1-parity contract `get_continuous_future` documents ("flat mirrors (v1 parity) so a v1-style Chart consumer works unchanged") |
| `fetch_future_front_closes` `freq` fix | **In scope** | Same root cause, one SQL line, and it currently yields a silently wrong number |

## API contract

### `GET /api/data-v2/objects/{id}/facets` — new

Populates the filter form. Two aggregates over `contract`; measured 0.37 s on EW2.

```json
{ "object_id": 12, "kind": "option",
  "totals": { "contracts": 96106, "series": 200672 },
  "expirations": [ { "expiration": "2026-09-11", "contracts": 146 } ],
  "strike_min": 15.0, "strike_max": 10600.0,
  "option_types": ["call", "put"],
  "serie_types": [ { "type": "bar", "freq": "1m" }, { "type": "bbba", "freq": "1m" },
                   { "type": "greeks", "freq": "daily" }, { "type": "value", "freq": "daily" } ] }
```

`option_types` uses the `contract.option_type` domain (`call` / `put`), **not** v1's `C` / `P`.
This is deliberate — v2 endpoints speak the v2 schema's vocabulary, as `get_continuous_options`
already does with its `option_type=call|put` parameter.

Objects without contracts (index, rate) return `expirations: []` and `null` strike bounds — a
normal response, not an error. The form renders only the dimensions present.

### `GET /api/data-v2/objects/{id}/series` — new

Filtered, paginated series list. Query params: `expiration_min`, `expiration_max`,
`strike_min`, `strike_max`, `option_type` (`call|put|both`, default `both`), `serie_type`
(`bar|bbba|greeks|value|any`, default `any`), `freq` (`1m|daily|any`, default `any`), `skip`
(default 0), `limit` (default 50, max 500 — v1's cap). Every filter is optional; omitting all of
them is a valid (if broad) query, which is why the `limit` cap is the load-bearing guardrail.

Results are ordered **`expiration, strike NULLS FIRST, option_type, serie_id`** — a total order
over the result set. This matters for correctness, not presentation: with a non-deterministic
`ORDER BY`, `LIMIT/OFFSET` paging can repeat or skip rows between pages. `serie_id` is the
unique tiebreaker that makes the order total.

```json
{ "items": [ { "serie_id": 1433194, "type": "bbba", "freq": "1m",
               "contract_code": "EW2H6 P6260.20260313",
               "expiration": "2026-03-13", "strike": 6260.0, "option_type": "put" } ],
  "total": 195, "skip": 0, "limit": 50 }
```

Shaped like the existing `PaginatedResult` (`tcg/types/common.py`). Contract metadata arrives
**joined**, which removes the client-side `contractsById` map `ObjectDetail.jsx` builds today.

### `GET /api/data-v2/objects/{id}` — changed

Returns object metadata only; no longer `contracts` / `series`. This is what ends the 38 MB
payload.

### `GET /api/data-v2/series/{serie_id}` — changed

Gains a `grain` field and adapts `ts`:

```json
{ "serie_id": 1433194, "type": "bbba", "grain": "intraday",
  "points": { "ts": ["2026-03-12T14:31:00Z", "2026-03-12T14:32:00Z"] } }
```

Daily series return `grain: "daily"` with `ts` unchanged as `YYYYMMDD` integers.

Series volume needs no cap: a mature intraday serie is small. Measured on EW2 expiration
2026-03-13, `EW2H6 P6700` holds 2 121 `bbba` points over two months. The intraday problem is
representation, not volume — no downsampling, no read limit.

## Data layer — `tcg/data/_sql/instruments_v2.py`

- **`fetch_object_facets(object_id)`** — new; the two aggregates above.
- **`list_series_filtered(object_id, filters, skip, limit)`** — new; joins `serie × contract`
  with every predicate pushed into SQL plus `LIMIT/OFFSET`, returning `(rows, total)`. Replaces
  `list_series` on this path.
- **`list_contracts`** — no longer used by the page; contract metadata comes from the join.
- **Grain dispatch** — `_ts_to_int` stays for daily; add `_ts_to_iso`. One dispatch point keyed
  on `serie.freq`, not a grain test scattered across call sites.
- **`fetch_future_front_closes`** — add `AND s.freq = 'daily'`.

Only `daily` and `1m` exist in `serie.freq` today (761 039 and 244 324 series respectively).
The rule is `daily` → integers, **anything else** → ISO 8601. The fallback to ISO is deliberate:
emitting a full timestamp loses nothing, whereas collapsing it to a date destroys information —
exactly today's bug. A future `5m` or `1h` frequency therefore cannot reintroduce it silently.

## Frontend

The filter panel **persists** beside the results rather than disappearing after submit, so
changing a dimension costs one interaction and never requires re-entering the others.

```
┌─ Filters ──────────────┐  ┌─ 195 series  (1-50) ──────────────┐
│ Expiration 2026-03-13 ▾│  │ EW2H6 P6255   bbba·1m             │
│ Strike     6000 → 7000 │  │ EW2H6 P6260   bbba·1m  ← selected │
│ Type       ( )C (•)P   │  │ EW2H6 P6265   bbba·1m             │
│ Series     bbba ▾ 1m ▾ │  │ …                                 │
│            [ Reset ]   │  │        ‹ Prev    Next ›           │
└────────────────────────┘  └───────────────────────────────────┘
```

- **`SeriesFilterPanel`** — new; driven by `/facets`, renders only existing dimensions. `freq`
  is a **user-facing control**, not an internal detail: it is how the user chooses between the
  minute quotes and the daily marks, which is the most consequential choice on this page.
- **`SeriesResultList`** — new; paginated. No virtualisation needed at 50 rows per page.
- **`ObjectDetail.jsx`** — drops the 200 672-entry `map()` and the `contractsById` map.
- **`SeriesChartV2.jsx`** — passes ISO strings straight to Plotly (which handles a datetime
  axis natively); keeps `formatDateInt` for integers. Dispatch on `grain`.
- **Hooks** — `useObjectFacetsV2`, `useSeriesListV2(objectId, filters, page)`.

**Filter state lives in the URL** (`?expiration=2026-03-13&type=put&serie_type=bbba`): the back
button works, a filter state is shareable by link, and a reload loses nothing. React Router is
already in use.

**Changing a filter never discards the chart.** If the plotted serie falls outside the new
filter the chart stays (the `serie_id` remains valid), flagged as outside the current filter.
Erasing a user's work because they moved a bound would make the tool tiresome.

## Error handling

| Case | Response | Rationale |
|---|---|---|
| Filter matches nothing | **200**, `items: []`, `total: 0` | A narrow filter is a result, not an error |
| Object not found | 404 `DataNotFoundError` | Existing hierarchy |
| `strike_min > strike_max`, `expiration_min > expiration_max` | 400 `ValidationError` | Matches `data_v2.py`'s handling of bad `strategy`/`adjustment` |
| `serie_type` / `freq` / `option_type` out of domain | 400 `ValidationError` listing valid values | Same |
| `limit` out of range | **400** via `Query(ge=1, le=500)` | Same as v1. Not 422 — `tcg/core/app.py:265` remaps `RequestValidationError` to 400 |
| Object without contracts | 200, empty `expirations`, `null` strike bounds | The form adapts |

Timeout headroom: every measured query is under 0.6 s against a 60 s `statement_timeout`. The
`COUNT(*)` behind `total` is the only one not bounded by `limit`; its worst case — largest
object, no expiration filter — measured 0.58 s.

## Testing

**Unit (no database):** filter-predicate construction; grain dispatch (`daily`→int, `1m`→ISO,
unknown→ISO); pagination arithmetic (`total`/`skip`/`limit`); the four validation errors.

**Integration (`integration` marker, live `dwh`):** facets on EW2 return 196 expirations; the
filtered list respects `limit` and its `total` agrees with the predicate; and the regression
test for the original defect — read a known `1m` serie and assert timestamps are distinct
(`len(set(ts)) == len(ts)`). That assertion is what would have caught the `_ts_to_int` collapse.

**Frontend (vitest):** the panel renders only dimensions present in `/facets`; changing a
dimension triggers a refetch (auto-apply); the chart forwards ISO strings unreformatted; filter
state round-trips through the URL.

**Module boundaries:** `import-linter` must still pass — no new dependencies, all SQL stays in
`tcg.data`, no access to `tcg.engine`.

## Out of scope

The continuous options resolver is untouched and stays limited to the 5 roots that have `value`
series (`EW1`, `EW2`, `EW3`, `EW4`, `ES`); the other 21 roots hold only `bar:1m` and `bbba:1m`.
Delta-based selection is not added — note that the 400 it returns cites "fact_greeks is empty",
which is no longer true (19.2M rows, 5 roots carry `greeks:daily`), but correcting that is
separate work. The delta-hedging backtest module is not addressed. The only deliberate overflow
is the `freq` filter on `fetch_future_front_closes`.

## Measured baselines (live `dwh`, 2026-08-04)

| Query | Time |
|---|---|
| Facets: expirations grouped, object 12 | 0.33 s |
| Facets: strike bounds + counts, object 12 | 0.37 s |
| Filtered page of 50 (expiration + type + strike + serie type) | 0.49 s |
| `COUNT(*)` same predicate | 0.53 s |
| `COUNT(*)` worst case (no expiration filter) | 0.58 s |

**Re-measured during implementation (2026-08-04, later the same day): 2–3× the figures above.**
The filtered 50-row page is 1.04–1.44 s and the unfiltered worst case 3.39–3.51 s, on an object
that grew to ~96 194 contracts / ~201 027 series mid-execution. Still roughly 50× inside the 60 s
`statement_timeout` by execution time. The `COUNT(*)` behind `total` costs **about 31 % of the page**
(medians 0.44 s vs 1.43 s over four trials) — an earlier draft of this note claimed the two were
roughly equal, which measurement refuted; skipping the count would save ~24 % of a combined round
trip, not half of it. Treat the table as a floor, not a forecast:
the warehouse is being backfilled and `serie` has no usable index for `WHERE object_id = %s` (both
its non-PK indexes are partial), so this cost grows with total warehouse size rather than with the
object queried. A `serie(object_id)` index is the standing recommendation and needs the schema
owner's sign-off.

Data shape backing the design: `fact_bbba` 14.8M rows and `fact_bar` 13.7M rows are both minute
grain (`DATABENTO:GLBX.MDP3:bbo-1m` / `ohlcv-1m`), covering 2024-07-22 → 2026-07-27;
`fact_greeks` (19.2M) and `fact_value` (21.3M) are daily. On one full day of object 16,
`fact_bar` and `fact_bbba` hold exactly 14 012 rows each across the same 198 contracts — trade
bars are as dense as quotes, not sparse.
