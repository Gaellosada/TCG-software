# Database v2 drill-down — follow-ups from the filters/pagination branch

Triaged by the final whole-branch review of `feat/datav2-filters-pagination` (31 commits).
Everything here was found, measured and deliberately **not** fixed in that branch. Items are
grouped by whether they change behaviour, and each carries the evidence that justifies it so the
next person does not have to re-derive it.

Numbers were measured on 2026-08-04/05 against the live `dwh`, which is being actively backfilled —
treat them as a floor, not a forecast.

## Blocking on a decision that is not ours

**Integrate `main`.** The branch was cut from `6ce74ae`; `main` is at `d59b677`, **38 commits
ahead**. Five files overlap; four auto-merge. `frontend/src/pages/DataV2/DataV2Page.test.jsx` is a
**semantic** conflict: the branch asserts `deltaRadio.disabled === true` plus a
`greeks unavailable in v2` tooltip, while `main` asserts `disabled === false` with no tooltip —
`main` un-greyed Delta because `fact_greeks` is populated (11 580 236 rows, live-probed 2026-07-27).
`main`'s component survives the merge, so the branch's test must be resolved in **`main`'s** favour
and the branch's new `DataV2Page` tests hand-merged around `main`'s two delta tests. `main` also
added a per-run v1/v2 data-source selector (`42c01bb`) touching `frontend/src/pages/DataV2/`.

**Index `serie(object_id)`** — needs the schema owner's sign-off, so it is not ours to create.
`tcg_instruments_v2.serie` has only `pk_serie(serie_id)` plus two *partial* uniques
(`… WHERE contract_id IS NOT NULL` / `IS NULL`), neither of which serves an unqualified `object_id`
predicate. Every `/facets` and `/series` call therefore seq-scans ~963 588 rows, so cost grows with
**total warehouse size** rather than with the object queried. Latency already drifted from
1.56 s / 1.31 s to 1.69 s / 2.13 s during the branch's own execution. `contract` needs nothing — it
already has a usable btree (`uq_contract_code(object_id, contract_code)`), and its seq scan on the
largest object is the *correct* plan at ~25 % selectivity.

## Correctness and user-visible

**Sanitise the three expiration URL keys.** `frontend/src/pages/DataV2/ObjectDetail.jsx:165-167`
reads `expiration`, `expiration_min` and `expiration_max` raw, while strike bounds go through
`parseStrikeBound` and the enums through `urlEnum`. `?expiration_min=notadate` issues a request,
the backend answers **HTTP 400 `Invalid date format`**, and the panel shows "Expiration: Any" beside
the error banner — because a `<select>` whose value is not among its options falls back to the first
one. Every other dimension degrades to a working page. It is untested **by construction**: the test
that closed the enum gap supplies a *valid* expiration alongside the hostile enums.

**Correct the `fact_greeks is empty` message.** `tcg/core/api/data_v2.py:172-174` tells the user
"the v2 warehouse has no greeks (fact_greeks is empty)". Measured `reltuples` for
`tcg_instruments_v2.fact_greeks`: **19 247 120**, and object 12 alone carries 91 989 `greeks:daily`
series with real rows. `main` has already made this message reachable by un-greying the control.
One line — and note the integration test at
`tests/integration/data/test_instruments_v2_integration.py:840` is
`pytest.raises(ValidationError, match="greeks")`, which tolerates any truthful rewording containing
that word. It pins *that* delta is rejected, not *why*, so it does not block the fix.

**Support delta selection properly.** Separate, larger work than the message: the continuous-options
resolver would need to read `fact_greeks`. Now that `main` has enabled the control, a user can pick
Delta and get an error.

**Put the object in the URL.** With no `object_id` in the query string, one back press after an
object switch re-applies the old filter to the new object, reproducing the panel-misreports-its-own
-state bug in a form nothing tests. Measured: back restores `serie_type=bbba&strike_min=6000`
against index 5, the panel reads "any" and hides the strike control. `{ replace: true }` on the
clear only pushes it one entry deeper.

**Debounce the panel's two number inputs.** Would remove a pre-existing fetch-per-keystroke.
History depth is already bounded by shape comparison, so this is about request volume, not history.

## Latent, no current trigger

**The `enabled`-before-spread hazard in 13 hooks.** `frontend/src/hooks/marketQueries.js:109, 121,
147, 168, 181, 204, 227, 252, 285, 342, 367, 380, 395` compute `enabled:` *before* spreading
`...options`, so a caller's `enabled` **replaces** the guard instead of narrowing it. Measured: 7 of
7 probed hooks fired with a null guard plus `{enabled: true}`. No production caller passes `enabled`
today, so it is latent — but the file now documents its own 13 defects. The two hooks added by this
branch (`:304`, `:331`) use the correct after-spread form and are the pattern to copy.

**A dated test exposure, before 2026-09-19.** `tests/integration/data/test_instruments_v2_integration.py`
picks `expired[-1]` inside a `[expiration - 7d, expiration]` window. The warehouse runs ~5 calendar
days behind. At each quarterly roll `expired[-1]` flips to a newly expired contract, and if staleness
then reaches ~7 days the window holds no rows. Headroom is ~2 days, exercised four times a year.
Widening the window to 21 days removes the exposure at no cost.

**Fix the two options-continuous integration timeouts.** `test_options_continuous_strike_live` and
`test_options_continuous_moneyness_live` sit at the 60 s `statement_timeout` because
`fetch_option_settlements` bulk-loads ~1.09 M settlement rows for a one-year window. Every `value`
serie on `OPT_SP_500_EW3` is `daily`, so no frequency pin helps — the resolver needs to narrow the
chain per date instead of fetching a year at once. Five runs produced four distinct outcome patterns
(strike-only, moneyness-only, both, strike-only, both), because the two share buffer warming. When
one times out it can drop the SSM tunnel and skip whatever follows.

## Coverage gaps on correct behaviour

- The **candlestick** trace's x values are untested (`SeriesChartV2.jsx`): `useChartPreference`
  returns `'line'` under jsdom always, so rewiring that trace to raw `ts` kills no test. A user with
  Candlestick selected would see every candle at 1970-01-01. One line: set
  `document.documentElement.dataset.chartType = 'candlestick'` in one added test.
- Nothing pins that page 2 → page 3 **pushes** history (`skip` is absent from `boundShape`'s blank
  list). The shipped code does push; the suite never does two page changes in a row.
- Seeding `lastWrittenShape` from the mount query string instead of `null` is undetected: a shared
  link already carrying a strike bound would have its own history entry replaced by the recipient's
  first retype.
- `URL_ENUMS` in `ObjectDetail.jsx` is an unpinned **third** copy of the filter enum domain. The two
  Python copies are pinned against each other by `tests/unit/test_api_data_v2.py`.

## Consistency and hygiene

- **The SQL test doubles are triplicated with two divergent variants.**
  `tests/unit/data/sql/test_sql_instruments_v2_{grain,facets,series_page}.py` each define
  `_FakeCursor`/`_FakeConn`/`_FakePool`. Facets and series_page are byte-identical; grain is weaker —
  no result-set-exhaustion guard, no `fetchone`, and an `_mk` that returns no cursor, so grain tests
  cannot assert SQL or params. If `read_serie_facts` ever issues a second statement the grain fake
  silently serves the same rows twice and stays green. `tests/unit/data/sql/conftest.py` already
  exists and its docstring names the fake-cursor convention.
- `parseStrikeBound` is imported by a parent from its child (`ObjectDetail.jsx` from
  `SeriesFilterPanel.jsx`). A small `seriesFilters.js` is the right home for it, `URL_ENUMS` and
  `boundShape`.
- `'daily'` is a bare SQL literal at `instruments_v2.py:486, 678` rather than derived from
  `_DAILY_FREQS`, so adding a `1d` synonym to the set would not reach SQL.
- The row `source` (e.g. `DATABENTO:GLBX.MDP3:bbo-1m`) arrives on every series row and is displayed
  nowhere. `downloadFilename` is now `symbol-serieId`, so exported CSVs are no longer
  self-describing. The contracts count is gone from the header.
- Double-clicking Next during an in-flight page loses the second click (the server-echoed `skip` is
  still the old one under `keepPreviousData`).
- Accessibility in `SeriesFilterPanel`: "Filters" is a `<div>` rather than a heading with no
  landmark; each control is wrapped in a `<label>` whose text absorbs the control's value into the
  accessible name ("Series typeAnybar") where the repo convention is a sibling `<label htmlFor>`;
  the number inputs have no `step`, so a fractional bound sits in `:invalid` in a real browser
  (jsdom cannot see this).
- `contract.multiplier` is no longer reachable from any v2 HTTP endpoint. No consumer is broken —
  worth knowing before someone needs contract notionals in the v2 UI.
- The design spec `2026-08-04-datav2-filters-pagination-design.md` is stale in ways that mislead:
  it still says "not yet implemented"; cites line numbers that moved and a `list_contracts` that no
  longer exists; says two facets aggregates where three shipped; its perf table is 3-4× optimistic
  and is what justifies not worrying about scaling; and **its example shareable link contains
  `type=put`, which no code reads** (only `option_type`), so following the spec's own format
  silently drops the call/put filter.

## Deliberately dropped

- **Clamping a huge `?skip`.** Measured: `skip=0` → 2.13 s, `skip=200 000` → 2.24 s,
  `skip=1 000 000 000` → 2.18 s. The OFFSET is not the cost; the seq scan is. There is no hazard
  here distinct from the indexing item.
- **Un-tracking `frontend/package-lock.json`.** A committed lockfile is correct for an application.
  The constraint during this branch was to avoid committing *platform-pruned changes* to it — an
  `npm install` on macOS strips Linux-only optional dependencies that CI needs.
- **The dead `AND expiration IS NOT NULL` predicate** at `instruments_v2.py:192`, as a standalone
  change: it and the `sum(expirations[].contracts) == totals.contracts` assertion must move
  together, so removing it alone is net-negative.
