# Database v2 — Series Filtering, Pagination & Intraday Grain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Database v2 page usable on the large option roots by replacing the unbounded object payload with a filtered, paginated series list, and emit real intraday timestamps so minute-grain series chart correctly.

**Architecture:** Three new read methods in the existing v2 SQL adapter (`fetch_object_facets`, `list_series_filtered`, plus a grain-aware `read_serie_facts`), surfaced by two new routes on the existing `/api/data-v2` router. The frontend replaces a flat 200 672-entry list with a persistent filter panel fed by a cheap facets aggregate, plus a paginated result list. Backend additions land first and are non-breaking, so the app keeps working at every task boundary; the fat `/objects/{id}` payload is only slimmed once the frontend has stopped consuming it.

**Tech Stack:** Python 3.12+, FastAPI, psycopg3 (async, `dict_row`), pytest + pytest-asyncio (`asyncio_mode=auto`), React 18, Vite, React Query, Plotly.js, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-04-datav2-filters-pagination-design.md`

## Global Constraints

- All v2 SQL lives in `tcg/data/_sql/instruments_v2.py`. `lint-imports` must keep passing (`uv run lint-imports --config .import-linter.cfg` — the bare `import-linter` entry point only prints a banner, and `.import-linter.cfg` is not a filename it discovers by default): `tcg.data` must not import `tcg.engine` or `tcg.core`.
- Every fact query bounds `ts` with a constant `>= lower AND < upper` range (BRIN/partition pruning). Use the existing `_bounds()` helper.
- The v2 reader uses the shared read-only `tcg_read` pool (`DwhConnectionPool`); schema is bound per-statement via `V2_SCHEMA`, never a second pool.
- `limit` default 50, maximum **500** (v1's cap, `tcg/core/api/data.py:222`).
- Server-side `statement_timeout` is 60 s. Every query in this plan measured under 0.6 s on the largest object (`object_id=12`).
- `option_type` on v2 endpoints uses the schema's domain **`call` / `put`** — never v1's `C` / `P`.
- Filter enum values are whitelisted in Python before reaching SQL; filter *values* are always passed as query parameters, never interpolated.
- Decimal → float coercion happens at the SQL boundary via the existing `to_float` / `to_float_or`.
- Never commit `frontend/package-lock.json` changes as part of these tasks.

---

## File Structure

**Backend**

| File | Responsibility | Change |
|---|---|---|
| `tcg/data/_sql/instruments_v2.py` | all v2 SQL reads | add `_ts_to_iso`, `grain_for_freq`, `fetch_object_facets`, `list_series_filtered`; make `read_serie_facts` grain-aware; fix `fetch_future_front_closes` |
| `tcg/data/service_v2.py` | orchestration over the reader | `get_series` emits `grain`; add `get_object_facets`, `list_object_series`; slim `get_object_detail` |
| `tcg/data/protocols.py` | `MarketDataServiceV2` interface | add the two new methods |
| `tcg/core/api/data_v2.py` | HTTP routes + validation | add `/objects/{id}/facets`, `/objects/{id}/series`; slim `/objects/{id}` |
| `tcg/types/common.py` | `PaginatedResult` | unchanged (reused) |

**Frontend**

| File | Responsibility | Change |
|---|---|---|
| `frontend/src/api/dataV2.js` | HTTP client for `/api/data-v2` | add `getObjectFacetsV2`, `getObjectSeriesV2` |
| `frontend/src/hooks/marketQueries.js` | React Query hooks | add `useObjectFacetsV2`, `useObjectSeriesV2` |
| `frontend/src/pages/DataV2/SeriesFilterPanel.jsx` | the persistent filter panel | **new** |
| `frontend/src/pages/DataV2/SeriesResultList.jsx` | paginated result list | **new** |
| `frontend/src/pages/DataV2/ObjectDetail.jsx` | drill-down container | drop flat list + `contractsById`, mount panel + list, own filter state |
| `frontend/src/pages/DataV2/SeriesChartV2.jsx` | series chart | dispatch x-axis on `grain` |

**Tests**

| File | Change |
|---|---|
| `tests/unit/test_data_v2_service.py` | update the fake reader; add facets / filtered-list / grain cases |
| `tests/unit/test_api_data_v2.py` | add route tests for the two new endpoints + validation errors |
| `tests/integration/data/test_instruments_v2_integration.py` | add live-dwh coverage incl. the intraday regression test |
| `frontend/src/pages/DataV2/SeriesFilterPanel.test.jsx` | **new** |
| `frontend/src/pages/DataV2/SeriesResultList.test.jsx` | **new** |
| `frontend/src/pages/DataV2/SeriesChartV2.test.jsx` | **new** (grain dispatch) |
| `frontend/src/pages/DataV2/DataV2Page.test.jsx` | update for the new drill-down |

---

## Task 1: Grain-aware timestamps

The core fix. `_ts_to_int` collapses every `ts` to a `YYYYMMDD` integer, so a minute-grain
series returns repeated abscissae (verified: serie 1116679 returns `ts: [20260601, 20260601]`).

**Files:**
- Modify: `tcg/data/_sql/instruments_v2.py` (add helpers near `_ts_to_int` at :57; change `read_serie_facts` at :191)
- Modify: `tcg/data/service_v2.py:56-85` (`get_series`)
- Test: `tests/unit/test_data_v2_service.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `grain_for_freq(freq: str | None) -> str` returning `"daily"` or `"intraday"`
  - `_ts_to_iso(ts: datetime) -> str`
  - `SqlInstrumentReaderV2.read_serie_facts(serie_id: int, serie_type: str, *, freq: str | None = None, start: date | None = None, end: date | None = None) -> tuple[str, list[int] | list[str], dict[str, list[float | None]]]` returning `(grain, ts, cols)`
  - `get_series()` response gains `"grain": "daily" | "intraday"`

Only one production caller (`service_v2.py:72`) and one fake (`tests/unit/test_data_v2_service.py:425`) touch `read_serie_facts`, so the arity change is contained.

- [ ] **Step 1: Write the failing tests for the grain helper**

Add to `tests/unit/test_data_v2_service.py`:

```python
from datetime import datetime, timezone

from tcg.data._sql.instruments_v2 import _ts_to_iso, grain_for_freq


def test_grain_for_freq_daily_is_date_grain():
    assert grain_for_freq("daily") == "daily"


def test_grain_for_freq_minute_is_intraday():
    assert grain_for_freq("1m") == "intraday"


def test_grain_for_freq_unknown_defaults_to_intraday():
    # Deliberate: emitting a full timestamp is lossless, collapsing one to a
    # date destroys information. A future '5m'/'1h' must not silently collapse.
    assert grain_for_freq("5m") == "intraday"
    assert grain_for_freq(None) == "intraday"


def test_ts_to_iso_normalises_to_utc_z():
    ts = datetime(2026, 3, 12, 14, 31, tzinfo=timezone.utc)
    assert _ts_to_iso(ts) == "2026-03-12T14:31:00Z"


def test_ts_to_iso_treats_naive_as_utc():
    assert _ts_to_iso(datetime(2026, 3, 12, 14, 31)) == "2026-03-12T14:31:00Z"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k "grain_for_freq or ts_to_iso" -v`
Expected: FAIL with `ImportError: cannot import name '_ts_to_iso'`

- [ ] **Step 3: Implement the helpers**

In `tcg/data/_sql/instruments_v2.py`, immediately after `_ts_to_int` (:57-61):

```python
def _ts_to_iso(ts: datetime) -> str:
    """timestamptz → ISO 8601 in UTC, ``Z``-suffixed.

    Used for intraday series, where the time-of-day IS the data point. A naive
    ts is assumed UTC (the dwh stores timestamptz; psycopg returns aware
    datetimes, but be defensive).
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


#: ``serie.freq`` values that carry no intraday component. Only ``daily`` and
#: ``1m`` exist in v2 today (761 039 and 244 324 series respectively).
_DAILY_FREQS = frozenset({"daily"})


def grain_for_freq(freq: str | None) -> str:
    """Return ``"daily"`` (ts → YYYYMMDD int) or ``"intraday"`` (ts → ISO 8601).

    Anything that is not explicitly a daily frequency is treated as intraday.
    That default is deliberate: emitting a full timestamp loses nothing, while
    collapsing one to a date destroys information — precisely the defect this
    fixes. A future ``5m``/``1h`` frequency therefore cannot reintroduce it.
    """
    return "daily" if (freq or "").strip().lower() in _DAILY_FREQS else "intraday"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k "grain_for_freq or ts_to_iso" -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Write the failing test for grain-aware fact reads**

This file already has a single configurable fake, `_FakeReaderService(obj=…, serie=…, facts=…,
contracts=…, series=…)`, and a `_make_service(reader)` helper. Use them — do **not** add a new
fake class, and do **not** call `DefaultMarketDataServiceV2(reader)`: the real constructor takes a
**pool**, not a reader, which is exactly why `_make_service` bypasses `__init__`.

Add to `tests/unit/test_data_v2_service.py`:

```python
_BBBA_COLS = {
    "best_bid_value": [610.5, 608.5],
    "best_bid_volume": [15.0, 15.0],
    "best_ask_value": [612.0, 610.0],
    "best_ask_volume": [15.0, 1.0],
}


async def test_get_series_daily_returns_int_dates_and_daily_grain():
    reader = _FakeReaderService(
        serie={
            "serie_id": 1, "object_id": 16, "contract_id": 42,
            "type": "bbba", "freq": "daily", "source": "TEST",
        },
        facts=("daily", [20260601, 20260602], _BBBA_COLS),
    )
    out = await _make_service(reader).get_series(1)
    assert out["grain"] == "daily"
    assert out["points"]["ts"] == [20260601, 20260602]


async def test_get_series_intraday_returns_iso_timestamps():
    reader = _FakeReaderService(
        serie={
            "serie_id": 1, "object_id": 16, "contract_id": 42,
            "type": "bbba", "freq": "1m", "source": "TEST",
        },
        facts=(
            "intraday",
            ["2026-06-01T14:31:00Z", "2026-06-01T14:32:00Z"],
            _BBBA_COLS,
        ),
    )
    out = await _make_service(reader).get_series(1)
    assert out["grain"] == "intraday"
    assert out["points"]["ts"] == [
        "2026-06-01T14:31:00Z",
        "2026-06-01T14:32:00Z",
    ]
    # The regression guard: distinct minutes must stay distinct.
    assert len(set(out["points"]["ts"])) == 2


async def test_get_series_forwards_freq_to_the_reader():
    """The service must pass serie.freq down — that is what selects the grain."""
    reader = _FakeReaderService(
        serie={
            "serie_id": 1, "object_id": 16, "contract_id": 42,
            "type": "bbba", "freq": "1m", "source": "TEST",
        },
        facts=("intraday", ["2026-06-01T14:31:00Z"], {k: [v[0]] for k, v in _BBBA_COLS.items()}),
    )
    await _make_service(reader).get_series(1)
    assert reader.last_freq == "1m"
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k "get_series_daily or get_series_intraday" -v`
Expected: FAIL — `get_series` unpacks two values, the fake returns three (`ValueError: too many values to unpack`), and the response has no `grain` key.

- [ ] **Step 7: Make `read_serie_facts` grain-aware**

In `tcg/data/_sql/instruments_v2.py`, change the signature and the row loop of
`read_serie_facts` (:191). The SQL is unchanged; only the `ts` mapping and the return shape move.

```python
    async def read_serie_facts(
        self,
        serie_id: int,
        serie_type: str,
        *,
        freq: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[str, list[int] | list[str], dict[str, list[float | None]]]:
        """Read one serie's facts from the fact table its ``type`` dispatches to.

        Returns ``(grain, ts, {field: [values...]})``. ``grain`` is
        ``"daily"`` (``ts`` are ``YYYYMMDD`` ints) or ``"intraday"`` (``ts`` are
        ISO 8601 strings) as decided by :func:`grain_for_freq` from *freq*.
        Collapsing an intraday ts to a date is what made minute series plot on a
        single abscissa, so the grain is resolved here, once.
        """
```

Replace the accumulation loop at the end of the method with:

```python
        grain = grain_for_freq(freq)
        to_ts = _ts_to_int if grain == "daily" else _ts_to_iso
        ts_out: list[Any] = []
        cols: dict[str, list[float | None]] = {f: [] for f in fields}
        for r in rows:
            ts_out.append(to_ts(r["ts"]))
            for f in fields:
                cols[f].append(to_float(r[f]))
        return grain, ts_out, cols
```

- [ ] **Step 8: Pass `freq` through and emit `grain` in the service**

In `tcg/data/service_v2.py`, `get_series` (:56-85). Replace the read + response build:

```python
        serie_type = serie["type"]
        grain, ts_values, cols = await self._reader.read_serie_facts(
            serie_id, serie_type, freq=serie.get("freq"), start=start, end=end
        )
        fields = list(FACT_DISPATCH[serie_type][1])
        points: dict[str, list] = {"ts": ts_values}
        points.update({f: cols[f] for f in fields})
        return {
            "serie_id": serie_id,
            "type": serie_type,
            "grain": grain,
            "fields": fields,
            "points": points,
        }
```

Keep the rest of the method (the `get_serie` lookup and its `DataNotFoundError`) as-is.

- [ ] **Step 9: Update the pre-existing fake reader**

`tests/unit/test_data_v2_service.py` defines `read_serie_facts(self, serie_id, serie_type, *,
start, end)` returning the 2-tuple `self._facts`. Change it to accept `freq` and return the
3-tuple, recording the freq it saw:

```python
    async def read_serie_facts(
        self, serie_id, serie_type, *, freq=None, start=None, end=None
    ):
        self.last_freq = freq
        return self._facts
```

Add `self.last_freq = None` to that fake's `__init__`, and change its `facts` default from
`facts or ([], {})` to:

```python
        self._facts = facts or ("daily", [], {})
```

Every existing call site that passes `facts=(ts, cols)` must become
`facts=(grain, ts, cols)`. Grep for `facts=` in the file and update each one — a daily fixture
takes `"daily"`, and the field keys must match `FACT_DISPATCH[serie_type][1]` for the serie's
declared `type`, or `get_series` raises `KeyError`.

- [ ] **Step 10: Run the full v2 unit suite**

Run: `uv run pytest tests/unit/test_data_v2_service.py tests/unit/test_api_data_v2.py -v`
Expected: PASS. If a router test asserts the exact `get_series` response dict, add `"grain"` to its expectation.

- [ ] **Step 11: Commit**

```bash
git add tcg/data/_sql/instruments_v2.py tcg/data/service_v2.py tests/unit/test_data_v2_service.py tests/unit/test_api_data_v2.py
git commit -m "fix(data-v2): emit real timestamps for intraday series

read_serie_facts collapsed every ts to a YYYYMMDD int, so a 1m series
returned repeated abscissae and charted onto a single point. Grain is now
resolved once from serie.freq: daily keeps ints, everything else emits ISO
8601. Unknown frequencies default to intraday because collapsing is the
lossy direction."
```

---

## Task 2: Fix the `freq` filter on `fetch_future_front_closes`

`fetch_future_front_closes` filters `s.type = 'bar'` but not `s.freq`. `FUT_SP_500` now has both
`bar:daily` and `bar:1m` series, so the query scans minute bars — it times out (measured: HTTP 502
after 71 s), and if it completed, `_front_close_by_date` keeps the first row per date, making the
"front close" the **00:00 minute bar** rather than the daily close: a silently wrong number.

**Files:**
- Modify: `tcg/data/_sql/instruments_v2.py:432-479` (`fetch_future_front_closes`)
- Test: `tests/integration/data/test_instruments_v2_integration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no signature change — `fetch_future_front_closes` keeps returning `list[dict]` of `{ts_int, expiration_int, close}`.

- [ ] **Step 1: Write the failing integration test**

Add to `tests/integration/data/test_instruments_v2_integration.py`:

```python
@pytest.mark.integration
async def test_front_closes_are_daily_and_one_per_date(svc):
    """The moneyness spot must come from daily bars, not minute bars.

    Without an `s.freq = 'daily'` filter this query scans fact_bar's minute
    rows: it times out, and the per-date front close silently becomes the
    00:00 bar instead of the daily close.
    """
    rows = await svc._reader.fetch_future_front_closes(
        6, start=date(2026, 3, 1), end=date(2026, 3, 31)
    )
    assert rows, "expected daily front-close rows for FUT_SP_500 in March 2026"
    # One row per (date, expiration) at daily grain — minute rows would give
    # hundreds per date.
    per_date = {}
    for r in rows:
        per_date.setdefault(r["ts_int"], set()).add(r["expiration_int"])
    worst = max(len(v) for v in per_date.values())
    assert worst < 20, f"{worst} expirations on one date suggests minute rows"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/data/test_instruments_v2_integration.py -k front_closes --run-integration -v`
Expected: FAIL — either a `DataAccessError` mentioning `statement timeout`, or the density assertion trips.

Requires the SSM tunnel: `bash ~/.claude/skills/tcg-db/scripts/tunnel.sh`.

- [ ] **Step 3: Add the freq filter**

In `fetch_future_front_closes`, add one predicate to the `WHERE` clause:

```sql
                            WHERE s.object_id = %s
                              AND s.type = 'bar'
                              AND s.freq = 'daily'
                              AND c.expiration IS NOT NULL
                              AND f.close > 0
                              AND f.ts >= %s AND f.ts < %s
```

Update the docstring's first line to state the grain explicitly:

```python
        """Fetch every DAILY future bar row (ts, expiration, close) for spot lookup.

        Feeds the options-continuous *moneyness* spot: the resolver picks, per
        date, the front future (nearest expiration >= that date) close. Pinned to
        ``freq = 'daily'`` — FUT_SP_500 also carries ``bar:1m`` series, and
        without the pin this scans minute bars (timeout) and makes the per-date
        front close the 00:00 bar rather than the daily close. Only
        ``close > 0`` rows are returned (false-zero guard). ``ts``
        constant-bounded.
        """
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/integration/data/test_instruments_v2_integration.py -k front_closes --run-integration -v`
Expected: PASS

- [ ] **Step 5: Verify the Continuous (Options) moneyness path now completes**

Run:

```bash
curl -s -w "\nHTTP %{http_code} in %{time_total}s\n" --max-time 120 \
  "http://127.0.0.1:8000/api/data-v2/continuous/options/7?criterion=moneyness&target=1.0&option_type=put&start=2025-09-01&end=2025-09-30" \
  | tail -c 200
```

Expected: `HTTP 200`. Before this fix the same call returned 502 after ~71 s.

- [ ] **Step 6: Commit**

```bash
git add tcg/data/_sql/instruments_v2.py tests/integration/data/test_instruments_v2_integration.py
git commit -m "fix(data-v2): pin front-close lookup to daily bars

FUT_SP_500 gained bar:1m series, so a type-only filter scanned minute bars:
the moneyness path timed out, and the per-date front close would have been
the 00:00 bar rather than the daily close."
```

---

## Task 3: Facets endpoint

The cheap aggregate that populates the filter form. Measured 0.33 s + 0.37 s on `object_id=12`.

**Files:**
- Modify: `tcg/data/_sql/instruments_v2.py` (add `fetch_object_facets` after `list_series`)
- Modify: `tcg/data/service_v2.py` (add `get_object_facets`)
- Modify: `tcg/data/protocols.py:206-238` (`MarketDataServiceV2`)
- Modify: `tcg/core/api/data_v2.py` (add the route **before** `/objects/{object_id}`)
- Test: `tests/unit/test_data_v2_service.py`, `tests/unit/test_api_data_v2.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SqlInstrumentReaderV2.fetch_object_facets(object_id: int) -> dict[str, Any]` with keys `expirations` (`list[{"expiration": str, "contracts": int}]`), `strike_min`/`strike_max` (`float | None`), `option_types` (`list[str]`), `serie_types` (`list[{"type": str, "freq": str, "series": int}]`), `totals` (`{"contracts": int, "series": int}`)
  - `DefaultMarketDataServiceV2.get_object_facets(object_id: int) -> dict`
  - `GET /api/data-v2/objects/{object_id}/facets`

- [ ] **Step 1: Write the failing service test**

Add to `tests/unit/test_data_v2_service.py`:

Extend the existing `_FakeReaderService` with a `facets=` kwarg (store it as `self._facets_data`,
default `None`) and one method:

```python
    async def fetch_object_facets(self, object_id):
        return self._facets_data
```

Then the tests:

```python
_EW2_FACETS = {
    "expirations": [{"expiration": "2026-09-11", "contracts": 146}],
    "strike_min": 15.0,
    "strike_max": 10600.0,
    "option_types": ["call", "put"],
    "serie_types": [
        {"type": "bar", "freq": "1m", "series": 96106},
        {"type": "bbba", "freq": "1m", "series": 96106},
    ],
    "totals": {"contracts": 96106, "series": 200672},
}

_EW2_OBJECT = {
    "object_id": 12,
    "kind": "option",
    "symbol": "OPT_SP_500_EW2",
    "name": "S&P 500 E-mini EW2 Weekly Options (CME)",
    "cycle": "weekly",
    "underlying_object_id": 6,
}


async def test_get_object_facets_returns_object_kind_and_facets():
    reader = _FakeReaderService(obj=_EW2_OBJECT, facets=_EW2_FACETS)
    out = await _make_service(reader).get_object_facets(12)
    assert out["object_id"] == 12
    assert out["kind"] == "option"
    assert out["totals"]["series"] == 200672
    assert out["option_types"] == ["call", "put"]


async def test_get_object_facets_unknown_object_raises_not_found():
    reader = _FakeReaderService(obj=None, facets=_EW2_FACETS)
    with pytest.raises(DataNotFoundError):
        await _make_service(reader).get_object_facets(999)
```

`DataNotFoundError` comes from `tcg.types.errors`; add the import if the file lacks it.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k object_facets -v`
Expected: FAIL with `AttributeError: 'DefaultMarketDataServiceV2' object has no attribute 'get_object_facets'`

- [ ] **Step 3: Implement the reader method**

In `tcg/data/_sql/instruments_v2.py`, after `list_series` (:152-166):

```python
    async def fetch_object_facets(self, object_id: int) -> dict[str, Any]:
        """Aggregate the filterable dimensions of one object.

        Cheap by design — this is what the filter form is built from, so it must
        never scan a fact table. Three grouped reads over ``contract`` and
        ``serie`` only (measured 0.33 s + 0.37 s on object 12, the largest).
        Objects without contracts (index / rate) yield empty ``expirations`` and
        ``None`` strike bounds; that is a normal answer, not an error.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT expiration, COUNT(*) AS contracts
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = %s AND expiration IS NOT NULL
                            GROUP BY expiration
                            ORDER BY expiration DESC""",
                        (object_id,),
                    )
                    expirations = [
                        {
                            "expiration": r["expiration"].isoformat(),
                            "contracts": int(r["contracts"]),
                        }
                        for r in await cur.fetchall()
                    ]

                    await cur.execute(
                        f"""SELECT MIN(strike) AS strike_min,
                                   MAX(strike) AS strike_max,
                                   COUNT(*) AS contracts,
                                   ARRAY_AGG(DISTINCT option_type)
                                     FILTER (WHERE option_type IS NOT NULL)
                                     AS option_types
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = %s""",
                        (object_id,),
                    )
                    agg = await cur.fetchone() or {}

                    await cur.execute(
                        f"""SELECT type, freq, COUNT(*) AS series
                            FROM {V2_SCHEMA}.serie
                            WHERE object_id = %s
                            GROUP BY type, freq
                            ORDER BY type, freq""",
                        (object_id,),
                    )
                    serie_types = [
                        {
                            "type": r["type"],
                            "freq": r["freq"],
                            "series": int(r["series"]),
                        }
                        for r in await cur.fetchall()
                    ]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading facets for object {object_id}: {exc}"
            ) from exc

        return {
            "expirations": expirations,
            "strike_min": to_float(agg.get("strike_min")),
            "strike_max": to_float(agg.get("strike_max")),
            "option_types": sorted(agg.get("option_types") or []),
            "serie_types": serie_types,
            "totals": {
                "contracts": int(agg.get("contracts") or 0),
                "series": sum(s["series"] for s in serie_types),
            },
        }
```

- [ ] **Step 4: Implement the service method**

In `tcg/data/service_v2.py`, after `get_object_detail`:

```python
    async def get_object_facets(self, object_id: int) -> dict:
        """Return the filterable dimensions of one object (for the filter form).

        Raises ``DataNotFoundError`` if the object does not exist.
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        facets = await self._reader.fetch_object_facets(object_id)
        return {"object_id": object_id, "kind": obj["kind"], **facets}
```

- [ ] **Step 5: Add it to the protocol**

In `tcg/data/protocols.py`, inside `class MarketDataServiceV2` after `get_object_detail` (:208):

```python
    async def get_object_facets(self, object_id: int) -> dict: ...
```

- [ ] **Step 6: Run the service tests to verify they pass**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k object_facets -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Write the failing route test**

Add to `tests/unit/test_api_data_v2.py`, and extend the module's `client` fixture mock with:

```python
    mock.get_object_facets = AsyncMock(
        return_value={
            "object_id": 12,
            "kind": "option",
            "expirations": [{"expiration": "2026-09-11", "contracts": 146}],
            "strike_min": 15.0,
            "strike_max": 10600.0,
            "option_types": ["call", "put"],
            "serie_types": [{"type": "bbba", "freq": "1m", "series": 96106}],
            "totals": {"contracts": 96106, "series": 200672},
        }
    )
```

Then the test:

```python
async def test_facets_route_returns_dimensions(client):
    res = await client.get("/api/data-v2/objects/12/facets")
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "option"
    assert body["totals"]["series"] == 200672
    assert body["expirations"][0]["contracts"] == 146


async def test_facets_route_is_not_captured_by_object_id(client):
    """`/objects/{id}/facets` must not be swallowed by the catch-all id route."""
    res = await client.get("/api/data-v2/objects/12/facets")
    assert res.status_code == 200
    assert "expirations" in res.json()
```

- [ ] **Step 8: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_api_data_v2.py -k facets -v`
Expected: FAIL with 404 (route not registered)

- [ ] **Step 9: Add the route**

In `tcg/core/api/data_v2.py`, in the "Series facts" block — **above** the
`/objects/{object_id}` catch-all, which the module header warns must stay last:

```python
@router.get("/objects/{object_id}/facets")
async def get_object_facets(
    object_id: int,
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Filterable dimensions of one object — feeds the series filter form.

    Declared before the ``/objects/{object_id}`` catch-all so the literal
    ``facets`` segment is never captured as an id.
    """
    return await svc.get_object_facets(object_id)
```

- [ ] **Step 10: Run the route tests to verify they pass**

Run: `uv run pytest tests/unit/test_api_data_v2.py -k facets -v`
Expected: PASS (2 tests)

- [ ] **Step 11: Verify against the live warehouse**

```bash
curl -s -w "\nHTTP %{http_code} in %{time_total}s\n" --max-time 60 \
  "http://127.0.0.1:8000/api/data-v2/objects/12/facets" \
  | python3 -c "
import json,sys
raw=sys.stdin.read(); head,_,tail=raw.rpartition('HTTP')
d=json.loads(head)
print('expirations:', len(d['expirations']))
print('strikes:', d['strike_min'], '->', d['strike_max'])
print('serie_types:', d['serie_types'])
print('totals:', d['totals'])
print('HTTP'+tail)"
```

Expected: 196 expirations, strikes `15.0 -> 10600.0`, four serie types
(`bar:1m`, `bbba:1m`, `greeks:daily`, `value:daily`), totals 96 106 / 200 672, under 2 s.

- [ ] **Step 12: Commit**

```bash
git add tcg/data/_sql/instruments_v2.py tcg/data/service_v2.py tcg/data/protocols.py tcg/core/api/data_v2.py tests/unit/test_data_v2_service.py tests/unit/test_api_data_v2.py
git commit -m "feat(data-v2): add object facets endpoint

Aggregates the filterable dimensions (expirations with counts, strike
bounds, option types, serie type/freq pairs) from the dimension tables only,
so the filter form can be populated without touching a fact table."
```

---

## Task 4: Filtered, paginated series endpoint

**Files:**
- Modify: `tcg/data/_sql/instruments_v2.py` (add `list_series_filtered`)
- Modify: `tcg/data/service_v2.py` (add `list_object_series`)
- Modify: `tcg/data/protocols.py` (`MarketDataServiceV2`)
- Modify: `tcg/core/api/data_v2.py` (route + validation, before the catch-all)
- Test: `tests/unit/test_data_v2_service.py`, `tests/unit/test_api_data_v2.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SqlInstrumentReaderV2.list_series_filtered(object_id: int, *, expiration_min: date | None = None, expiration_max: date | None = None, strike_min: float | None = None, strike_max: float | None = None, option_type: str = "both", serie_type: str = "any", freq: str = "any", skip: int = 0, limit: int = 50) -> tuple[list[dict[str, Any]], int]` returning `(rows, total)`
  - `DefaultMarketDataServiceV2.list_object_series(object_id: int, **same kwargs) -> dict` with keys `items`, `total`, `skip`, `limit`
  - `GET /api/data-v2/objects/{object_id}/series`

- [ ] **Step 1: Write the failing service tests**

Add to `tests/unit/test_data_v2_service.py`:

Extend `_FakeReaderService` again: add a `filtered=` kwarg (a `(rows, total)` tuple, default
`([], 0)`), a `self.filter_calls = []` list in `__init__`, and one method:

```python
    async def list_series_filtered(self, object_id, **kwargs):
        self.filter_calls.append((object_id, kwargs))
        return self._filtered
```

Then the tests:

```python
_EW2_PAGE_ROWS = [
    {
        "serie_id": 1433194,
        "contract_id": 77,
        "type": "bbba",
        "freq": "1m",
        "source": "DATABENTO:GLBX.MDP3:bbo-1m",
        "contract_code": "EW2H6 P6260.20260313",
        "expiration": "2026-03-13",
        "strike": 6260.0,
        "option_type": "put",
    }
]


async def test_list_object_series_returns_paginated_shape():
    reader = _FakeReaderService(obj=_EW2_OBJECT, filtered=(_EW2_PAGE_ROWS, 195))
    svc = _make_service(reader)
    out = await svc.list_object_series(12, serie_type="bbba", skip=0, limit=50)
    assert out["total"] == 195
    assert out["skip"] == 0
    assert out["limit"] == 50
    assert len(out["items"]) == 1
    assert out["items"][0]["contract_code"] == "EW2H6 P6260.20260313"


async def test_list_object_series_forwards_every_filter():
    reader = _FakeReaderService(obj=_EW2_OBJECT, filtered=(_EW2_PAGE_ROWS, 195))
    svc = _make_service(reader)
    await svc.list_object_series(
        12,
        expiration_min=date(2026, 3, 1),
        expiration_max=date(2026, 3, 31),
        strike_min=6000.0,
        strike_max=7000.0,
        option_type="put",
        serie_type="bbba",
        freq="1m",
        skip=50,
        limit=100,
    )
    _, kwargs = reader.filter_calls[0]
    assert kwargs["expiration_min"] == date(2026, 3, 1)
    assert kwargs["strike_max"] == 7000.0
    assert kwargs["option_type"] == "put"
    assert kwargs["freq"] == "1m"
    assert kwargs["skip"] == 50
    assert kwargs["limit"] == 100


async def test_list_object_series_unknown_object_raises_not_found():
    reader = _FakeReaderService(obj=None, filtered=(_EW2_PAGE_ROWS, 195))
    with pytest.raises(DataNotFoundError):
        await _make_service(reader).list_object_series(999)


async def test_list_object_series_empty_result_is_not_an_error():
    """A narrow filter is a result, not an error."""
    reader = _FakeReaderService(obj=_EW2_OBJECT, filtered=([], 0))
    out = await _make_service(reader).list_object_series(12, strike_min=999_999.0)
    assert out["items"] == []
    assert out["total"] == 0
```

Ensure `from datetime import date` is imported in the test module.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k list_object_series -v`
Expected: FAIL with `AttributeError: … has no attribute 'list_object_series'`

- [ ] **Step 3: Implement the reader method**

In `tcg/data/_sql/instruments_v2.py`, after `fetch_object_facets`:

```python
    #: Whitelisted filter enum values. Validated before reaching SQL; the
    #: *values* are still bound as parameters, never interpolated.
    _SERIE_TYPES = frozenset(FACT_DISPATCH) | {"any"}
    _FREQS = frozenset({"1m", "daily", "any"})
    _OPTION_TYPES = frozenset({"call", "put", "both"})

    async def list_series_filtered(
        self,
        object_id: int,
        *,
        expiration_min: date | None = None,
        expiration_max: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        option_type: str = "both",
        serie_type: str = "any",
        freq: str = "any",
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one filtered page of an object's series, plus the total count.

        LEFT JOIN, not INNER: object-level series (``contract_id IS NULL`` —
        index and rate objects) must still list. Contract metadata is returned
        joined so the caller needs no second round-trip and the frontend needs
        no contract_id → contract map.

        Ordering is ``expiration, strike, option_type, serie_id`` — a TOTAL
        order. This is correctness, not presentation: under a non-deterministic
        ORDER BY, LIMIT/OFFSET paging can repeat or skip rows between pages.
        ``serie_id`` is the unique tiebreaker.
        """
        if serie_type not in self._SERIE_TYPES:
            raise DataAccessError(f"v2 unknown serie_type filter {serie_type!r}")
        if freq not in self._FREQS:
            raise DataAccessError(f"v2 unknown freq filter {freq!r}")
        if option_type not in self._OPTION_TYPES:
            raise DataAccessError(f"v2 unknown option_type filter {option_type!r}")

        where = ["s.object_id = %s"]
        params: list[Any] = [object_id]
        if serie_type != "any":
            where.append("s.type = %s")
            params.append(serie_type)
        if freq != "any":
            where.append("s.freq = %s")
            params.append(freq)
        if option_type != "both":
            where.append("c.option_type = %s")
            params.append(option_type)
        if expiration_min is not None:
            where.append("c.expiration >= %s")
            params.append(expiration_min)
        if expiration_max is not None:
            where.append("c.expiration <= %s")
            params.append(expiration_max)
        if strike_min is not None:
            where.append("c.strike >= %s")
            params.append(strike_min)
        if strike_max is not None:
            where.append("c.strike <= %s")
            params.append(strike_max)
        clause = " AND ".join(where)

        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT COUNT(*) AS total
                            FROM {V2_SCHEMA}.serie s
                            LEFT JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            WHERE {clause}""",
                        tuple(params),
                    )
                    row = await cur.fetchone()
                    total = int(row["total"]) if row else 0

                    await cur.execute(
                        f"""SELECT s.serie_id, s.contract_id, s.type, s.freq,
                                   s.source, c.contract_code, c.expiration,
                                   c.strike, c.option_type
                            FROM {V2_SCHEMA}.serie s
                            LEFT JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            WHERE {clause}
                            ORDER BY c.expiration NULLS FIRST,
                                     c.strike NULLS FIRST,
                                     c.option_type NULLS FIRST,
                                     s.serie_id
                            LIMIT %s OFFSET %s""",
                        (*params, limit, skip),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error listing filtered series for object "
                f"{object_id}: {exc}"
            ) from exc

        items = [
            {
                "serie_id": r["serie_id"],
                "contract_id": r["contract_id"],
                "type": r["type"],
                "freq": r["freq"],
                "source": r["source"],
                "contract_code": r["contract_code"],
                "expiration": r["expiration"].isoformat() if r["expiration"] else None,
                "strike": to_float(r["strike"]),
                "option_type": r["option_type"],
            }
            for r in rows
        ]
        return items, total
```

- [ ] **Step 4: Implement the service method**

In `tcg/data/service_v2.py`, after `get_object_facets`:

```python
    async def list_object_series(
        self,
        object_id: int,
        *,
        expiration_min: date | None = None,
        expiration_max: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        option_type: str = "both",
        serie_type: str = "any",
        freq: str = "any",
        skip: int = 0,
        limit: int = 50,
    ) -> dict:
        """One filtered, paginated page of an object's series.

        Returns the ``PaginatedResult`` shape (``items``/``total``/``skip``/
        ``limit``). Raises ``DataNotFoundError`` if the object does not exist —
        but a filter matching nothing is NOT an error: it returns an empty
        ``items`` with ``total: 0``.
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        items, total = await self._reader.list_series_filtered(
            object_id,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
            option_type=option_type,
            serie_type=serie_type,
            freq=freq,
            skip=skip,
            limit=limit,
        )
        return {"items": items, "total": total, "skip": skip, "limit": limit}
```

- [ ] **Step 5: Add it to the protocol**

In `tcg/data/protocols.py`, inside `class MarketDataServiceV2` after `get_object_facets`:

```python
    async def list_object_series(
        self,
        object_id: int,
        *,
        expiration_min: date | None = None,
        expiration_max: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        option_type: str = "both",
        serie_type: str = "any",
        freq: str = "any",
        skip: int = 0,
        limit: int = 50,
    ) -> dict: ...
```

- [ ] **Step 6: Run the service tests to verify they pass**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k list_object_series -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Write the failing route tests**

Add to `tests/unit/test_api_data_v2.py`, extending the `client` fixture mock:

```python
    mock.list_object_series = AsyncMock(
        return_value={
            "items": [
                {
                    "serie_id": 1433194,
                    "contract_id": 77,
                    "type": "bbba",
                    "freq": "1m",
                    "source": "DATABENTO:GLBX.MDP3:bbo-1m",
                    "contract_code": "EW2H6 P6260.20260313",
                    "expiration": "2026-03-13",
                    "strike": 6260.0,
                    "option_type": "put",
                }
            ],
            "total": 195,
            "skip": 0,
            "limit": 50,
        }
    )
```

Tests:

```python
async def test_series_list_route_returns_paginated_shape(client):
    res = await client.get("/api/data-v2/objects/12/series?serie_type=bbba&freq=1m")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 195
    assert body["limit"] == 50
    assert body["items"][0]["option_type"] == "put"


async def test_series_list_route_rejects_inverted_strike_range(client):
    res = await client.get(
        "/api/data-v2/objects/12/series?strike_min=7000&strike_max=6000"
    )
    assert res.status_code == 400
    assert "strike_min" in res.json()["message"]


async def test_series_list_route_rejects_inverted_expiration_range(client):
    res = await client.get(
        "/api/data-v2/objects/12/series"
        "?expiration_min=2026-03-31&expiration_max=2026-03-01"
    )
    assert res.status_code == 400
    assert "expiration_min" in res.json()["message"]


async def test_series_list_route_rejects_unknown_serie_type(client):
    res = await client.get("/api/data-v2/objects/12/series?serie_type=nope")
    assert res.status_code == 400
    assert "serie_type" in res.json()["message"]


async def test_series_list_route_caps_limit_at_500(client):
    res = await client.get("/api/data-v2/objects/12/series?limit=5000")
    assert res.status_code == 422
```

- [ ] **Step 8: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_api_data_v2.py -k series_list_route -v`
Expected: FAIL with 404 (route not registered)

- [ ] **Step 9: Add the route with validation**

In `tcg/core/api/data_v2.py`, directly after the facets route (still above the catch-all):

```python
#: Filter enum domains, mirrored from the reader's whitelist so a bad value is
#: a clean 400 at the boundary rather than a DataAccessError from the adapter.
_SERIE_TYPE_VALUES = ("bar", "value", "greeks", "bbba", "any")
_FREQ_VALUES = ("1m", "daily", "any")
_OPTION_TYPE_VALUES = ("call", "put", "both")


@router.get("/objects/{object_id}/series")
async def list_object_series(
    object_id: int,
    expiration_min: str | None = Query(None, description="Earliest expiration YYYY-MM-DD"),
    expiration_max: str | None = Query(None, description="Latest expiration YYYY-MM-DD"),
    strike_min: float | None = Query(None, description="Strike lower bound"),
    strike_max: float | None = Query(None, description="Strike upper bound"),
    option_type: str = Query("both", description="call | put | both"),
    serie_type: str = Query("any", description="bar | value | greeks | bbba | any"),
    freq: str = Query("any", description="1m | daily | any"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """One filtered, paginated page of an object's series.

    Declared before the ``/objects/{object_id}`` catch-all. A filter that
    matches nothing returns 200 with an empty ``items`` and ``total: 0`` — a
    narrow filter is a result, not an error.
    """
    if serie_type not in _SERIE_TYPE_VALUES:
        raise ValidationError(
            f"Invalid serie_type {serie_type!r}. Must be one of: "
            f"{', '.join(_SERIE_TYPE_VALUES)}"
        )
    if freq not in _FREQ_VALUES:
        raise ValidationError(
            f"Invalid freq {freq!r}. Must be one of: {', '.join(_FREQ_VALUES)}"
        )
    if option_type not in _OPTION_TYPE_VALUES:
        raise ValidationError(
            f"Invalid option_type {option_type!r}. Must be one of: "
            f"{', '.join(_OPTION_TYPE_VALUES)}"
        )
    try:
        exp_min, exp_max = parse_iso_range(expiration_min, expiration_max)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if exp_min is not None and exp_max is not None and exp_min > exp_max:
        raise ValidationError(
            f"expiration_min ({exp_min.isoformat()}) is after "
            f"expiration_max ({exp_max.isoformat()})"
        )
    if strike_min is not None and strike_max is not None and strike_min > strike_max:
        raise ValidationError(
            f"strike_min ({strike_min}) is greater than strike_max ({strike_max})"
        )

    return await svc.list_object_series(
        object_id,
        expiration_min=exp_min,
        expiration_max=exp_max,
        strike_min=strike_min,
        strike_max=strike_max,
        option_type=option_type,
        serie_type=serie_type,
        freq=freq,
        skip=skip,
        limit=limit,
    )
```

`parse_iso_range` (`tcg/core/api/_dates.py`) is
`(str | None, str | None) -> tuple[date | None, date | None]` and returns `(None, None)` for empty
input, so the `is not None` guards above are correct as written.

- [ ] **Step 10: Run the route tests to verify they pass**

Run: `uv run pytest tests/unit/test_api_data_v2.py -k series_list_route -v`
Expected: PASS (5 tests)

- [ ] **Step 11: Verify against the live warehouse**

```bash
curl -s -w "\nHTTP %{http_code} in %{time_total}s\n" --max-time 60 \
  "http://127.0.0.1:8000/api/data-v2/objects/12/series?expiration_min=2026-03-13&expiration_max=2026-03-13&serie_type=bbba&option_type=put&strike_min=6000&strike_max=7000&limit=50" \
  | python3 -c "
import json,sys
raw=sys.stdin.read(); head,_,tail=raw.rpartition('HTTP')
d=json.loads(head)
print('total:', d['total'], '| items:', len(d['items']))
print('first:', d['items'][0]['contract_code'], d['items'][0]['strike'])
print('HTTP'+tail)"
```

Expected: `total: 195`, 50 items, first item around strike 6000, under 2 s.

Then confirm paging is stable (no repeats across page boundaries):

```bash
for skip in 0 50; do
  curl -s "http://127.0.0.1:8000/api/data-v2/objects/12/series?expiration_min=2026-03-13&expiration_max=2026-03-13&serie_type=bbba&option_type=put&skip=$skip&limit=50" \
    | python3 -c "import json,sys; print([i['serie_id'] for i in json.load(sys.stdin)['items']][:3])"
done
```

Expected: two disjoint id lists.

- [ ] **Step 12: Commit**

```bash
git add tcg/data/_sql/instruments_v2.py tcg/data/service_v2.py tcg/data/protocols.py tcg/core/api/data_v2.py tests/unit/test_data_v2_service.py tests/unit/test_api_data_v2.py
git commit -m "feat(data-v2): filtered, paginated series endpoint

Pushes expiration/strike/type/freq filters plus LIMIT/OFFSET into SQL and
returns contract metadata joined. Ordered by a total key so LIMIT/OFFSET
paging cannot repeat or skip rows. LEFT JOIN keeps object-level series
(index/rate) listable."
```

---

## Task 5: Frontend API client and hooks

**Files:**
- Modify: `frontend/src/api/dataV2.js`
- Modify: `frontend/src/queryKeys.js:118-150` (the `market.v2` block)
- Modify: `frontend/src/hooks/marketQueries.js:263-300`
- Test: `frontend/src/api/dataV2.test.js` (new — the codebase already keeps per-module API tests: `options.test.js`, `indicators.test.js`, `persistence.test.js`, `portfolio.test.js`)

**Interfaces:**
- Consumes: `GET /objects/{id}/facets` and `GET /objects/{id}/series` from Tasks 3-4.
- Produces:
  - `getObjectFacetsV2(objectId, { signal }) -> Promise<facets>`
  - `getObjectSeriesV2(objectId, { expirationMin, expirationMax, strikeMin, strikeMax, optionType, serieType, freq, skip, limit, signal }) -> Promise<{items,total,skip,limit}>`
  - `useObjectFacetsV2(objectId, options)`
  - `useObjectSeriesV2(objectId, filters, options)` where `filters` is the camelCase object above; `enabled` is false until `filters` is non-null (this is what keeps the first load gated)

- [ ] **Step 1: Write the failing client tests**

Create `frontend/src/api/dataV2.test.js`:

```javascript
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getObjectSeriesV2 } from './dataV2';
import * as client from './client';

describe('getObjectSeriesV2', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('maps camelCase filters onto snake_case query params', async () => {
    const spy = vi.spyOn(client, 'fetchApi').mockResolvedValue({
      items: [], total: 0, skip: 0, limit: 50,
    });
    await getObjectSeriesV2(12, {
      expirationMin: '2026-03-01',
      expirationMax: '2026-03-31',
      strikeMin: 6000,
      strikeMax: 7000,
      optionType: 'put',
      serieType: 'bbba',
      freq: '1m',
      skip: 50,
      limit: 100,
    });
    const url = spy.mock.calls[0][0];
    expect(url).toContain('expiration_min=2026-03-01');
    expect(url).toContain('expiration_max=2026-03-31');
    expect(url).toContain('strike_min=6000');
    expect(url).toContain('strike_max=7000');
    expect(url).toContain('option_type=put');
    expect(url).toContain('serie_type=bbba');
    expect(url).toContain('freq=1m');
    expect(url).toContain('skip=50');
    expect(url).toContain('limit=100');
  });

  it('omits unset filters entirely', async () => {
    const spy = vi.spyOn(client, 'fetchApi').mockResolvedValue({
      items: [], total: 0, skip: 0, limit: 50,
    });
    await getObjectSeriesV2(12, {});
    const url = spy.mock.calls[0][0];
    expect(url).not.toContain('strike_min');
    expect(url).not.toContain('expiration_min');
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/api/dataV2.test.js`
Expected: FAIL — `getObjectSeriesV2` is not exported.

- [ ] **Step 3: Implement the client functions**

Append to `frontend/src/api/dataV2.js`:

```javascript
/**
 * GET /api/data-v2/objects/{object_id}/facets
 * → { object_id, kind, expirations:[{expiration, contracts}], strike_min,
 *     strike_max, option_types:[...], serie_types:[{type, freq, series}],
 *     totals:{contracts, series} }
 * Cheap aggregate over the dimension tables — this is what the filter panel
 * is built from, so it never touches a fact table.
 */
export async function getObjectFacetsV2(objectId, { signal } = {}) {
  return fetchClassified(
    `/data-v2/objects/${encodeURIComponent(objectId)}/facets`,
    { signal },
  );
}

/**
 * GET /api/data-v2/objects/{object_id}/series?<filters>&skip&limit
 * → { items:[{serie_id, contract_id, type, freq, source, contract_code,
 *     expiration, strike, option_type}], total, skip, limit }
 * Contract metadata arrives joined, so no contract_id → contract map is needed
 * client-side. An empty `items` with `total: 0` is a normal answer.
 */
export async function getObjectSeriesV2(objectId, {
  expirationMin,
  expirationMax,
  strikeMin,
  strikeMax,
  optionType,
  serieType,
  freq,
  skip,
  limit,
  signal,
} = {}) {
  const params = new URLSearchParams();
  if (expirationMin) params.set('expiration_min', expirationMin);
  if (expirationMax) params.set('expiration_max', expirationMax);
  if (strikeMin !== undefined && strikeMin !== null && strikeMin !== '') {
    params.set('strike_min', String(strikeMin));
  }
  if (strikeMax !== undefined && strikeMax !== null && strikeMax !== '') {
    params.set('strike_max', String(strikeMax));
  }
  if (optionType) params.set('option_type', optionType);
  if (serieType) params.set('serie_type', serieType);
  if (freq) params.set('freq', freq);
  if (skip) params.set('skip', String(skip));
  if (limit) params.set('limit', String(limit));
  const query = params.toString() ? `?${params}` : '';
  return fetchClassified(
    `/data-v2/objects/${encodeURIComponent(objectId)}/series${query}`,
    { signal },
  );
}
```

- [ ] **Step 4: Run the client tests to verify they pass**

Run: `cd frontend && npx vitest run src/api/dataV2.test.js`
Expected: PASS (2 tests)

- [ ] **Step 5: Add the query-key builders**

In `frontend/src/queryKeys.js`, inside the `market.v2` block (after `object` at :126):

```javascript
      /** GET /data-v2/objects/{id}/facets — filterable dimensions */
      facets: (objectId) => ['market', 'v2', 'facets', objectId ?? null],

      /**
       * GET /data-v2/objects/{id}/series?<filters> — one filtered page.
       * The filter object is part of the key, so changing any dimension is a
       * distinct cache entry rather than a refetch of the same key.
       */
      seriesList: (objectId, filters) => [
        'market', 'v2', 'seriesList', objectId ?? null, filters ?? null,
      ],
```

- [ ] **Step 6: Add the hooks**

In `frontend/src/hooks/marketQueries.js`, add `getObjectFacetsV2, getObjectSeriesV2` to the
existing import from `'../api/dataV2'` (:53-59), then after `useObjectDetailV2`:

```javascript
/** GET /data-v2/objects/{id}/facets — drives the series filter panel. */
export function useObjectFacetsV2(objectId, options = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.v2.facets(objectId),
      queryFn: ({ signal }) => getObjectFacetsV2(objectId, { signal }),
      enabled: objectId != null && (options.enabled ?? true),
      ...options,
    }),
  );
}

/**
 * GET /data-v2/objects/{id}/series — one filtered page.
 *
 * `filters` null → disabled. That is what gates the first load: nothing is
 * fetched until the user applies a filter, so an unbounded request can never
 * be issued. Once a filter exists every change refetches automatically — each
 * query is bounded by `limit` and measured under 0.6 s.
 *
 * `keepPreviousData` keeps the current page visible while the next one loads,
 * matching `useSeriesV2`'s behaviour.
 */
export function useObjectSeriesV2(objectId, filters, options = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.v2.seriesList(objectId, filters),
      queryFn: ({ signal }) => getObjectSeriesV2(objectId, { ...filters, signal }),
      enabled: objectId != null && filters != null && (options.enabled ?? true),
      placeholderData: keepPreviousData,
      ...options,
    }),
  );
}
```

`asAsyncResult`, `useQuery`, `keepPreviousData` and `queryKeys` are all already imported in that
module — this matches `useObjectDetailV2` / `useSeriesV2` exactly.

- [ ] **Step 7: Run the hook tests**

Run: `cd frontend && npx vitest run src/hooks/marketQueries.test.jsx src/api/dataV2.test.js`
Expected: PASS (no regressions)

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/dataV2.js frontend/src/queryKeys.js frontend/src/hooks/marketQueries.js frontend/src/api/dataV2.test.js
git commit -m "feat(data-v2): client + hooks for facets and filtered series

useObjectSeriesV2 stays disabled until a filter object exists, which is what
gates the first load; subsequent filter changes refetch automatically."
```

---

## Task 6: `SeriesFilterPanel`

**Files:**
- Create: `frontend/src/pages/DataV2/SeriesFilterPanel.jsx`
- Test: `frontend/src/pages/DataV2/SeriesFilterPanel.test.jsx`
- Modify: `frontend/src/pages/DataV2/DataV2.module.css` (panel classes)

**Interfaces:**
- Consumes: `useObjectFacetsV2` (Task 5).
- Produces: `<SeriesFilterPanel objectId={number} onApply={(filters) => void} />` where `filters`
  is the camelCase object `useObjectSeriesV2` consumes. Calls `onApply` on the explicit
  **Apply** click for the first application, then on **every** subsequent field change.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/DataV2/SeriesFilterPanel.test.jsx`:

```javascript
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

// Mock the hook module (declared before the component import for hoisting) so
// the panel needs no QueryClient. This mirrors how DataV2Page.test.jsx stubs
// its dependencies.
vi.mock('../../hooks/marketQueries', () => ({
  useObjectFacetsV2: vi.fn(),
}));

import SeriesFilterPanel from './SeriesFilterPanel';
import { useObjectFacetsV2 } from '../../hooks/marketQueries';

afterEach(cleanup);

const OPTION_FACETS = {
  object_id: 12,
  kind: 'option',
  expirations: [
    { expiration: '2026-03-13', contracts: 500 },
    { expiration: '2026-02-13', contracts: 480 },
  ],
  strike_min: 15,
  strike_max: 10600,
  option_types: ['call', 'put'],
  serie_types: [
    { type: 'bar', freq: '1m', series: 96106 },
    { type: 'bbba', freq: '1m', series: 96106 },
    { type: 'greeks', freq: 'daily', series: 4230 },
  ],
  totals: { contracts: 96106, series: 200672 },
};

const INDEX_FACETS = {
  object_id: 5,
  kind: 'index',
  expirations: [],
  strike_min: null,
  strike_max: null,
  option_types: [],
  serie_types: [{ type: 'bar', freq: 'daily', series: 1 }],
  totals: { contracts: 0, series: 1 },
};

function mockFacets(data) {
  useObjectFacetsV2.mockReturnValue({ data, loading: false, error: null });
}

describe('SeriesFilterPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('offers expiration, strike, option type and series controls for an option', () => {
    mockFacets(OPTION_FACETS);
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    expect(screen.getByLabelText(/expiration/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/strike min/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/option type/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/series type/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/frequency/i)).toBeInTheDocument();
  });

  it('hides contract dimensions for an object without contracts', () => {
    mockFacets(INDEX_FACETS);
    render(<SeriesFilterPanel objectId={5} onApply={() => {}} />);
    expect(screen.queryByLabelText(/expiration/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/strike min/i)).not.toBeInTheDocument();
    // The series-type control always exists — it is the only dimension here.
    expect(screen.getByLabelText(/series type/i)).toBeInTheDocument();
  });

  it('populates the expiration options from facets', () => {
    mockFacets(OPTION_FACETS);
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    expect(screen.getByRole('option', { name: /2026-03-13/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /2026-02-13/ })).toBeInTheDocument();
  });

  it('does not call onApply before the first Apply click', () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);
    fireEvent.change(screen.getByLabelText(/series type/i), {
      target: { value: 'bbba' },
    });
    expect(onApply).not.toHaveBeenCalled();
  });

  it('applies on the first click, then auto-applies on every later change', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);

    fireEvent.change(screen.getByLabelText(/expiration/i), {
      target: { value: '2026-03-13' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(onApply.mock.calls[0][0]).toMatchObject({
      expirationMin: '2026-03-13',
      expirationMax: '2026-03-13',
    });

    fireEvent.change(screen.getByLabelText(/option type/i), {
      target: { value: 'put' },
    });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(2));
    expect(onApply.mock.calls[1][0]).toMatchObject({ optionType: 'put' });
  });

  it('reset clears the fields and re-gates until Apply', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: /reset/i }));
    expect(screen.getByLabelText(/series type/i)).toHaveValue('any');
    const before = onApply.mock.calls.length;
    fireEvent.change(screen.getByLabelText(/series type/i), {
      target: { value: 'bar' },
    });
    expect(onApply.mock.calls.length).toBe(before);
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesFilterPanel.test.jsx`
Expected: FAIL — module `./SeriesFilterPanel` does not exist.

- [ ] **Step 3: Implement the component**

Create `frontend/src/pages/DataV2/SeriesFilterPanel.jsx`:

```javascript
import { useState, useMemo, useEffect, useRef, useId } from 'react';
import { useObjectFacetsV2 } from '../../hooks/marketQueries';
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

/**
 * Persistent filter panel for an object's series.
 *
 * Two behaviours the design turns on:
 *  - Nothing is fetched until the user clicks Apply, so an unbounded series
 *    request can never be issued (the 38 MB payload this page used to send).
 *  - After that first Apply, every field change auto-applies. Each query is
 *    bounded by `limit` and sub-second, so the gate has done its job and
 *    re-clicking Apply would just be friction.
 *
 * The panel STAYS MOUNTED beside the results — changing one dimension must not
 * cost re-entering the others.
 *
 * Controls are driven by `/facets`, so only dimensions that exist for this
 * object are rendered: an index or rate object has no contracts, hence no
 * expiration or strike control.
 */
function SeriesFilterPanel({ objectId, onApply }) {
  const { data: facets, loading, error } = useObjectFacetsV2(objectId);
  const uid = useId();

  const [expiration, setExpiration] = useState('');
  const [strikeMin, setStrikeMin] = useState('');
  const [strikeMax, setStrikeMax] = useState('');
  const [optionType, setOptionType] = useState('both');
  const [serieType, setSerieType] = useState('any');
  const [freq, setFreq] = useState('any');

  // False until the first explicit Apply; gates auto-application.
  const [applied, setApplied] = useState(false);

  const hasContracts = (facets?.expirations?.length || 0) > 0;
  const hasStrikes = facets?.strike_min != null && facets?.strike_max != null;
  const hasOptionTypes = (facets?.option_types?.length || 0) > 0;

  // Distinct type / freq values actually present on this object.
  const serieTypeValues = useMemo(() => {
    const s = new Set((facets?.serie_types || []).map((t) => t.type));
    return Array.from(s).sort();
  }, [facets]);
  const freqValues = useMemo(() => {
    const s = new Set((facets?.serie_types || []).map((t) => t.freq));
    return Array.from(s).sort();
  }, [facets]);

  const filters = useMemo(() => ({
    // A single expiration choice maps to a closed [min, max] window of one day.
    expirationMin: expiration || undefined,
    expirationMax: expiration || undefined,
    strikeMin: strikeMin === '' ? undefined : Number(strikeMin),
    strikeMax: strikeMax === '' ? undefined : Number(strikeMax),
    optionType,
    serieType,
    freq,
  }), [expiration, strikeMin, strikeMax, optionType, serieType, freq]);

  // Auto-apply on change, but only once the user has applied at least once.
  const onApplyRef = useRef(onApply);
  onApplyRef.current = onApply;
  const firstRun = useRef(true);
  useEffect(() => {
    if (!applied) return;
    if (firstRun.current) { firstRun.current = false; return; }
    onApplyRef.current(filters);
  }, [filters, applied]);

  function handleApply() {
    firstRun.current = true;   // the Apply click itself is the first emission
    setApplied(true);
    onApplyRef.current(filters);
  }

  function handleReset() {
    setExpiration('');
    setStrikeMin('');
    setStrikeMax('');
    setOptionType('both');
    setSerieType('any');
    setFreq('any');
    setApplied(false);         // re-gate: no fetch until Apply again
  }

  if (loading) {
    return <div className={baseStyles.status}>Loading filters…</div>;
  }
  if (error) {
    return (
      <div className={baseStyles.error}>
        Failed to load filters: {error.message || String(error)}
      </div>
    );
  }

  return (
    <div className={styles.filterPanel}>
      <div className={styles.filterHeader}>
        Filters
        {facets?.totals ? (
          <span className={baseStyles.meta}>
            {` · ${facets.totals.series.toLocaleString()} series`}
          </span>
        ) : null}
      </div>

      {hasContracts && (
        <label className={styles.filterField} htmlFor={`${uid}-exp`}>
          Expiration
          <select
            id={`${uid}-exp`}
            value={expiration}
            onChange={(e) => setExpiration(e.target.value)}
          >
            <option value="">Any</option>
            {facets.expirations.map((e) => (
              <option key={e.expiration} value={e.expiration}>
                {`${e.expiration} · ${e.contracts.toLocaleString()}`}
              </option>
            ))}
          </select>
        </label>
      )}

      {hasStrikes && (
        <>
          <label className={styles.filterField} htmlFor={`${uid}-kmin`}>
            Strike min
            <input
              id={`${uid}-kmin`}
              type="number"
              value={strikeMin}
              placeholder={String(facets.strike_min)}
              onChange={(e) => setStrikeMin(e.target.value)}
            />
          </label>
          <label className={styles.filterField} htmlFor={`${uid}-kmax`}>
            Strike max
            <input
              id={`${uid}-kmax`}
              type="number"
              value={strikeMax}
              placeholder={String(facets.strike_max)}
              onChange={(e) => setStrikeMax(e.target.value)}
            />
          </label>
        </>
      )}

      {hasOptionTypes && (
        <label className={styles.filterField} htmlFor={`${uid}-otype`}>
          Option type
          <select
            id={`${uid}-otype`}
            value={optionType}
            onChange={(e) => setOptionType(e.target.value)}
          >
            <option value="both">Both</option>
            {facets.option_types.map((t) => (
              <option key={t} value={t}>{t === 'call' ? 'Call' : 'Put'}</option>
            ))}
          </select>
        </label>
      )}

      <label className={styles.filterField} htmlFor={`${uid}-stype`}>
        Series type
        <select
          id={`${uid}-stype`}
          value={serieType}
          onChange={(e) => setSerieType(e.target.value)}
        >
          <option value="any">Any</option>
          {serieTypeValues.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </label>

      {/* freq is a first-class user control: it is how the user chooses
          between the minute quotes and the daily marks. */}
      <label className={styles.filterField} htmlFor={`${uid}-freq`}>
        Frequency
        <select
          id={`${uid}-freq`}
          value={freq}
          onChange={(e) => setFreq(e.target.value)}
        >
          <option value="any">Any</option>
          {freqValues.map((f) => (
            <option key={f} value={f}>{f}</option>
          ))}
        </select>
      </label>

      <div className={styles.filterActions}>
        <button type="button" onClick={handleApply}>Apply</button>
        <button type="button" onClick={handleReset}>Reset</button>
      </div>
    </div>
  );
}

export default SeriesFilterPanel;
```

- [ ] **Step 4: Add the CSS classes**

Append to `frontend/src/pages/DataV2/DataV2.module.css`:

```css
.filterPanel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 220px;
  padding: 12px;
}

.filterHeader {
  font-weight: 600;
  font-size: 13px;
}

.filterField {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}

.filterActions {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesFilterPanel.test.jsx`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/DataV2/SeriesFilterPanel.jsx frontend/src/pages/DataV2/SeriesFilterPanel.test.jsx frontend/src/pages/DataV2/DataV2.module.css
git commit -m "feat(data-v2): persistent series filter panel

Driven by /facets so only dimensions that exist are offered. Gates the first
load behind Apply, then auto-applies on every change. freq is exposed as a
user control: it selects minute quotes vs daily marks."
```

---

## Task 7: `SeriesResultList`

**Files:**
- Create: `frontend/src/pages/DataV2/SeriesResultList.jsx`
- Test: `frontend/src/pages/DataV2/SeriesResultList.test.jsx`
- Modify: `frontend/src/pages/DataV2/DataV2.module.css`

**Interfaces:**
- Consumes: nothing from earlier tasks (it is presentational — the caller owns the query).
- Produces: `<SeriesResultList items={array} total={number} skip={number} limit={number} loading={bool} error={Error|null} selectedSerieId={number|null} onSelect={(serieId) => void} onPageChange={(nextSkip) => void} />`

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/DataV2/SeriesResultList.test.jsx`:

```javascript
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SeriesResultList from './SeriesResultList';

const ITEMS = [
  {
    serie_id: 1433194, type: 'bbba', freq: '1m',
    contract_code: 'EW2H6 P6260.20260313',
    expiration: '2026-03-13', strike: 6260, option_type: 'put',
  },
  {
    serie_id: 1492643, type: 'bbba', freq: '1m',
    contract_code: 'EW2H6 P6255.20260313',
    expiration: '2026-03-13', strike: 6255, option_type: 'put',
  },
];

describe('SeriesResultList', () => {
  it('renders the range and total', () => {
    render(
      <SeriesResultList items={ITEMS} total={195} skip={0} limit={50}
        loading={false} error={null} selectedSerieId={null}
        onSelect={() => {}} onPageChange={() => {}} />,
    );
    expect(screen.getByText(/195/)).toBeInTheDocument();
    expect(screen.getByText('EW2H6 P6260.20260313')).toBeInTheDocument();
  });

  it('shows an explicit empty state rather than an error', () => {
    render(
      <SeriesResultList items={[]} total={0} skip={0} limit={50}
        loading={false} error={null} selectedSerieId={null}
        onSelect={() => {}} onPageChange={() => {}} />,
    );
    expect(screen.getByText(/no series match/i)).toBeInTheDocument();
  });

  it('calls onSelect with the serie id', () => {
    const onSelect = vi.fn();
    render(
      <SeriesResultList items={ITEMS} total={195} skip={0} limit={50}
        loading={false} error={null} selectedSerieId={null}
        onSelect={onSelect} onPageChange={() => {}} />,
    );
    fireEvent.click(screen.getByText('EW2H6 P6260.20260313'));
    expect(onSelect).toHaveBeenCalledWith(1433194);
  });

  it('disables Prev on the first page and pages forward by limit', () => {
    const onPageChange = vi.fn();
    render(
      <SeriesResultList items={ITEMS} total={195} skip={0} limit={50}
        loading={false} error={null} selectedSerieId={null}
        onSelect={() => {}} onPageChange={onPageChange} />,
    );
    expect(screen.getByRole('button', { name: /prev/i })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(onPageChange).toHaveBeenCalledWith(50);
  });

  it('disables Next on the last page', () => {
    render(
      <SeriesResultList items={ITEMS} total={195} skip={150} limit={50}
        loading={false} error={null} selectedSerieId={null}
        onSelect={() => {}} onPageChange={() => {}} />,
    );
    expect(screen.getByRole('button', { name: /next/i })).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesResultList.test.jsx`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the component**

Create `frontend/src/pages/DataV2/SeriesResultList.jsx`:

```javascript
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

/**
 * Paginated list of filtered series.
 *
 * Presentational: the caller owns the query and the filter state. No
 * virtualisation is needed — a page is `limit` rows (50 by default), not the
 * 200 672 this page used to mount at once.
 *
 * An empty result is a normal outcome of a narrow filter, so it renders an
 * empty state rather than an error.
 */
function SeriesResultList({
  items,
  total,
  skip,
  limit,
  loading,
  error,
  selectedSerieId,
  onSelect,
  onPageChange,
}) {
  const from = total === 0 ? 0 : skip + 1;
  const to = Math.min(skip + limit, total);
  const hasPrev = skip > 0;
  const hasNext = skip + limit < total;

  if (error) {
    return (
      <div className={baseStyles.error}>
        Failed to load series: {error.message || String(error)}
      </div>
    );
  }

  return (
    <div className={styles.resultList}>
      <div className={styles.seriesListHeader}>
        {loading
          ? 'Loading series…'
          : `${total.toLocaleString()} series${total ? ` (${from}-${to})` : ''}`}
      </div>

      {!loading && items.length === 0 ? (
        <div className={baseStyles.status} style={{ padding: 16 }}>
          No series match this filter.
        </div>
      ) : (
        items.map((s) => (
          <button
            key={s.serie_id}
            type="button"
            className={`${styles.seriesItem} ${
              s.serie_id === selectedSerieId ? styles.seriesItemActive : ''
            }`}
            onClick={() => onSelect(s.serie_id)}
            title={`${s.contract_code || `serie ${s.serie_id}`} — ${s.type} · ${s.freq}`}
          >
            <span className={styles.seriesItemPrimary}>
              {s.contract_code || `serie ${s.serie_id}`}
            </span>
            <span className={styles.seriesItemMeta}>
              {`${s.type} · ${s.freq}`}
            </span>
          </button>
        ))
      )}

      <div className={styles.pager}>
        <button
          type="button"
          disabled={!hasPrev}
          onClick={() => onPageChange(Math.max(0, skip - limit))}
        >
          ‹ Prev
        </button>
        <button
          type="button"
          disabled={!hasNext}
          onClick={() => onPageChange(skip + limit)}
        >
          Next ›
        </button>
      </div>
    </div>
  );
}

export default SeriesResultList;
```

- [ ] **Step 4: Add the CSS classes**

Append to `frontend/src/pages/DataV2/DataV2.module.css`:

```css
.resultList {
  display: flex;
  flex-direction: column;
  min-width: 260px;
}

.pager {
  display: flex;
  gap: 8px;
  justify-content: center;
  padding: 8px;
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesResultList.test.jsx`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/DataV2/SeriesResultList.jsx frontend/src/pages/DataV2/SeriesResultList.test.jsx frontend/src/pages/DataV2/DataV2.module.css
git commit -m "feat(data-v2): paginated series result list

One page of `limit` rows, so no virtualisation is needed. An empty result
renders an empty state, not an error — a narrow filter is a valid outcome."
```

---

## Task 8: Grain dispatch in `SeriesChartV2`

`formatDateInt` returns its input unchanged when the string is not 8 characters long
(`frontend/src/utils/format.js:64-69`), so an ISO timestamp already survives it. That is
incidental, not intent — make the dispatch explicit and pin it with tests.

**Files:**
- Modify: `frontend/src/pages/DataV2/SeriesChartV2.jsx:52`
- Test: `frontend/src/pages/DataV2/SeriesChartV2.test.jsx` (new)

**Interfaces:**
- Consumes: the `grain` field from Task 1.
- Produces: no new exports; the chart's x values are ISO strings for `grain === 'intraday'` and `YYYY-MM-DD` strings for `daily`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/DataV2/SeriesChartV2.test.jsx`:

```javascript
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';

const captured = { traces: null };

// SeriesChartV2 imports `Chart` from '../../components/Chart' (the barrel), the
// same path DataV2Page.test.jsx stubs. Capture the traces it receives.
vi.mock('../../components/Chart', () => ({
  default: ({ traces }) => {
    captured.traces = traces;
    return <div data-testid="chart" />;
  },
}));

vi.mock('../../hooks/marketQueries', () => ({
  useSeriesV2: vi.fn(),
}));

import SeriesChartV2 from './SeriesChartV2';
import { useSeriesV2 } from '../../hooks/marketQueries';

afterEach(cleanup);

function mockSeries(data) {
  useSeriesV2.mockReturnValue({ data, loading: false, error: null });
}

describe('SeriesChartV2 grain dispatch', () => {
  beforeEach(() => { vi.clearAllMocks(); captured.traces = null; });

  it('formats daily int dates as YYYY-MM-DD', () => {
    mockSeries({
      serie_id: 1, type: 'value', grain: 'daily',
      fields: ['value'],
      points: { ts: [20260601, 20260602], value: [1.5, 1.6] },
    });
    render(<SeriesChartV2 serieId={1} serieType="value" label="x" />);
    expect(captured.traces[0].x).toEqual(['2026-06-01', '2026-06-02']);
  });

  it('passes intraday ISO timestamps through unchanged', () => {
    mockSeries({
      serie_id: 2, type: 'value', grain: 'intraday',
      fields: ['value'],
      points: {
        ts: ['2026-06-01T14:31:00Z', '2026-06-01T14:32:00Z'],
        value: [1.5, 1.6],
      },
    });
    render(<SeriesChartV2 serieId={2} serieType="value" label="x" />);
    expect(captured.traces[0].x).toEqual([
      '2026-06-01T14:31:00Z',
      '2026-06-01T14:32:00Z',
    ]);
  });

  it('keeps distinct intraday minutes distinct', () => {
    mockSeries({
      serie_id: 3, type: 'bbba', grain: 'intraday',
      fields: ['best_bid_value', 'best_bid_volume', 'best_ask_value', 'best_ask_volume'],
      points: {
        ts: ['2026-06-01T14:31:00Z', '2026-06-01T14:32:00Z'],
        best_bid_value: [610.5, 608.5],
        best_bid_volume: [15, 15],
        best_ask_value: [612, 610],
        best_ask_volume: [15, 1],
      },
    });
    render(<SeriesChartV2 serieId={3} serieType="bbba" label="x" />);
    expect(new Set(captured.traces[0].x).size).toBe(2);
  });
});
```

`SeriesChartV2.jsx:5` imports `Chart from '../../components/Chart'` — the barrel, not
`components/Chart/Chart`. The `vi.mock` path above already matches it.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesChartV2.test.jsx`
Expected: the daily test passes incidentally; the intraday tests are the ones to watch. If they
pass too, they still lock in behaviour that is currently accidental — proceed to Step 3 to make it
intentional.

- [ ] **Step 3: Make the dispatch explicit**

In `frontend/src/pages/DataV2/SeriesChartV2.jsx`, replace line 52:

```javascript
    // ts are YYYYMMDD ints — convert to YYYY-MM-DD strings for the date x axis.
    const x = ts.map(formatDateInt);
```

with:

```javascript
    // x-axis grain is server-declared. Daily series send YYYYMMDD ints, which
    // need formatting; intraday series send ISO 8601 strings, which Plotly puts
    // on a datetime axis as-is. Reformatting those would throw away the
    // time-of-day — the whole point of a minute series.
    const grain = data.grain || 'daily';
    const x = grain === 'intraday' ? ts.slice() : ts.map(formatDateInt);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesChartV2.test.jsx`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/DataV2/SeriesChartV2.jsx frontend/src/pages/DataV2/SeriesChartV2.test.jsx
git commit -m "feat(data-v2): dispatch chart x-axis on server-declared grain

Intraday series now plot on a real datetime axis. This worked only by
accident before (formatDateInt passes non-8-char strings through); make it
intentional and pin it with tests."
```

---

## Task 9: Wire the new drill-down into `ObjectDetail`

**Files:**
- Modify: `frontend/src/pages/DataV2/ObjectDetail.jsx`
- Test: `frontend/src/pages/DataV2/DataV2Page.test.jsx`

**Interfaces:**
- Consumes: `SeriesFilterPanel` (Task 6), `SeriesResultList` (Task 7), `useObjectSeriesV2` (Task 5).
- Produces: no new exports. After this task the page no longer reads `data.contracts` or `data.series`.

- [ ] **Step 1: Write the failing page test**

Replace the Series-tab portion of `frontend/src/pages/DataV2/DataV2Page.test.jsx` with
assertions for the new flow, keeping the existing Continuous-tab test intact:

This file renders through `renderWithClient` (from `../../test/queryWrapper`), not bare `render`,
and mocks `'../../api/dataV2'` with an explicit factory. Add the two new client functions to that
factory:

```javascript
vi.mock('../../api/dataV2', () => ({
  listObjectsV2: vi.fn(),
  getObjectDetailV2: vi.fn(),
  getObjectFacetsV2: vi.fn(),
  getObjectSeriesV2: vi.fn(),
  getSeriesV2: vi.fn(),
  getContinuousFuturesV2: vi.fn(),
  getV2FuturesCycles: vi.fn(),
  getContinuousOptionsV2: vi.fn(),
}));
```

Add them to the import list below the mock, and in `beforeEach` give them resolved values:

```javascript
  getObjectFacetsV2.mockResolvedValue({
    object_id: 7, kind: 'option',
    expirations: [{ expiration: '2026-03-13', contracts: 500 }],
    strike_min: 15, strike_max: 10600,
    option_types: ['call', 'put'],
    serie_types: [{ type: 'bbba', freq: '1m', series: 96106 }],
    totals: { contracts: 96106, series: 200672 },
  });
  getObjectSeriesV2.mockResolvedValue({
    items: [{
      serie_id: 1433194, contract_id: 77, type: 'bbba', freq: '1m',
      source: 'DATABENTO:GLBX.MDP3:bbo-1m',
      contract_code: 'EW2H6 P6260.20260313',
      expiration: '2026-03-13', strike: 6260, option_type: 'put',
    }],
    total: 195, skip: 0, limit: 50,
  });
```

Then the tests (note `OPT_SP_500_EW3` — that is the option symbol this file's `LIVE_OBJECTS`
fixture defines, object 7):

```javascript
  it('shows the filter panel and fetches nothing until Apply', async () => {
    renderWithClient(<DataV2Page />);
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    expect(await screen.findByText(/Filters/)).toBeInTheDocument();
    // The series-list query must not have run yet — this is the gate.
    expect(getObjectSeriesV2).not.toHaveBeenCalled();
  });

  it('lists series after Apply', async () => {
    renderWithClient(<DataV2Page />);
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.click(await screen.findByRole('button', { name: /apply/i }));
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeInTheDocument();
    await waitFor(() => expect(getObjectSeriesV2).toHaveBeenCalled());
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/pages/DataV2/DataV2Page.test.jsx`
Expected: FAIL — no "Filters" text; the old flat list renders instead.

- [ ] **Step 3: Rewrite the Series tab of `ObjectDetail`**

In `frontend/src/pages/DataV2/ObjectDetail.jsx`:

Replace the imports at :1-8 — drop nothing, add the two new components:

```javascript
import { useState, useMemo } from 'react';
import { useObjectDetailV2, useObjectSeriesV2 } from '../../hooks/marketQueries';
import SeriesChartV2 from './SeriesChartV2';
import SeriesFilterPanel from './SeriesFilterPanel';
import SeriesResultList from './SeriesResultList';
import ContinuousFuturesChartV2 from './ContinuousFuturesChartV2';
import ContinuousOptionsChartV2 from './ContinuousOptionsChartV2';
import pageStyles from '../Data/DataPage.module.css';
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';
```

Delete the `contractsById` memo (:23-28) and the `seriesList` memo (:32-46) entirely, and replace
the component's state block with:

```javascript
function ObjectDetail({ object }) {
  const { data, loading, error } = useObjectDetailV2(object.object_id);
  const [tab, setTab] = useState('series');
  const [selectedSerieId, setSelectedSerieId] = useState(null);

  // null until the user applies a filter — this is what gates the first fetch.
  const [filters, setFilters] = useState(null);
  const [skip, setSkip] = useState(0);
  const LIMIT = 50;

  const query = useMemo(
    () => (filters ? { ...filters, skip, limit: LIMIT } : null),
    [filters, skip],
  );
  const {
    data: page,
    loading: pageLoading,
    error: pageError,
  } = useObjectSeriesV2(object.object_id, query);

  // A new filter starts from the first page; changing pages must not reset it.
  function handleApply(next) {
    setSkip(0);
    setFilters(next);
  }
```

Keep `selectedSerie` resolvable from the current page, falling back to the id alone so a chart
survives a filter change that excludes it:

```javascript
  const selectedSerie = useMemo(() => {
    const found = (page?.items || []).find((s) => s.serie_id === selectedSerieId);
    if (found) return { ...found, outsideFilter: false };
    if (selectedSerieId == null) return null;
    // Still chartable: the serie_id remains valid. Erasing a user's chart
    // because they moved a filter bound would make the tool tiresome.
    return { serie_id: selectedSerieId, type: null, outsideFilter: true };
  }, [page, selectedSerieId]);
```

Replace the header's counts (:85-86), which no longer exist on `data`:

```javascript
          {object.name}
          {object.cycle ? ` · cycle ${object.cycle}` : ''}
```

Replace the whole `{tab === 'series' && (…)}` block (:109-150) with:

```javascript
        {tab === 'series' && (
          <div className={styles.seriesLayout}>
            <SeriesFilterPanel
              objectId={object.object_id}
              onApply={handleApply}
            />
            {filters == null ? (
              <div className={styles.seriesEmpty}>
                Set a filter and press Apply to list this object&apos;s series.
              </div>
            ) : (
              <SeriesResultList
                items={page?.items || []}
                total={page?.total || 0}
                skip={page?.skip ?? skip}
                limit={page?.limit ?? LIMIT}
                loading={pageLoading}
                error={pageError}
                selectedSerieId={selectedSerieId}
                onSelect={setSelectedSerieId}
                onPageChange={setSkip}
              />
            )}
            <div className={styles.seriesChartCol}>
              {selectedSerie ? (
                <>
                  {selectedSerie.outsideFilter && (
                    <div className={baseStyles.meta}>
                      This series is outside the current filter.
                    </div>
                  )}
                  <SeriesChartV2
                    key={selectedSerie.serie_id}
                    serieId={selectedSerie.serie_id}
                    serieType={selectedSerie.type}
                    label={`${object.symbol} · ${
                      selectedSerie.contract_code || selectedSerie.serie_id
                    }`}
                    downloadFilename={`${object.symbol}-${selectedSerie.serie_id}`}
                  />
                </>
              ) : (
                <div className={styles.seriesEmpty}>
                  Pick a series to chart it.
                </div>
              )}
            </div>
          </div>
        )}
```

- [ ] **Step 4: Run the page tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DataV2/`
Expected: PASS across all DataV2 test files.

- [ ] **Step 5: Verify in the running app**

With the backend and Vite up, open `http://localhost:5173`, go to Database v2, click
`OPT_SP_500_EW2`. Expected: the filter panel appears **immediately** (no 34 s wait, no freeze).
Pick expiration `2026-03-13`, series type `bbba`, frequency `1m`, press Apply, then click a
contract. Expected: a chart with a real intraday time axis (distinct minutes on x).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/DataV2/ObjectDetail.jsx frontend/src/pages/DataV2/DataV2Page.test.jsx
git commit -m "feat(data-v2): filter-driven series drill-down

Replaces the flat 200 672-entry list (and the client-side contract map) with
a persistent filter panel plus a paginated result list. A filter change never
discards the chart: the serie_id stays valid and is flagged out-of-filter."
```

---

## Task 10: Slim the object detail payload

Only now is this safe — the frontend no longer reads `contracts` or `series`.

**Files:**
- Modify: `tcg/data/service_v2.py:44-54` (`get_object_detail`)
- Modify: `frontend/src/api/dataV2.js` (docstring of `getObjectDetailV2`)
- Test: `tests/unit/test_data_v2_service.py`, `tests/unit/test_api_data_v2.py`

**Interfaces:**
- Consumes: Task 9 (frontend already migrated).
- Produces: `get_object_detail(object_id) -> {"object": {...}}` — no `contracts`, no `series`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_data_v2_service.py`:

```python
async def test_get_object_detail_no_longer_ships_contracts_or_series():
    """The 38 MB payload is gone: bulk lists moved to the paginated endpoint."""
    reader = _FakeReaderService(obj=_EW2_OBJECT)
    out = await _make_service(reader).get_object_detail(12)
    assert out["object"]["object_id"] == 12
    assert "contracts" not in out
    assert "series" not in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k no_longer_ships -v`
Expected: FAIL — `contracts` is still present in the response.

- [ ] **Step 3: Slim the service method**

In `tcg/data/service_v2.py`, replace `get_object_detail` (:44-54):

```python
    async def get_object_detail(self, object_id: int) -> dict:
        """Return ``{object}`` for one object — metadata only.

        Contracts and series are NOT included: on the large option roots that
        was 96 106 contracts + 200 672 series in a single 38 MB response taking
        34 s. Both now come from ``list_object_series`` (filtered + paginated)
        and ``get_object_facets`` (aggregated).

        Raises ``DataNotFoundError`` if the object does not exist.
        """
        obj = await self._reader.get_object(object_id)
        if obj is None:
            raise DataNotFoundError(f"Object {object_id} not found in v2")
        return {"object": obj}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/unit/test_data_v2_service.py -k no_longer_ships -v`
Expected: PASS

- [ ] **Step 5: Update the stale API-client docstring**

In `frontend/src/api/dataV2.js`, replace the `getObjectDetailV2` doc comment:

```javascript
/**
 * GET /api/data-v2/objects/{object_id}
 * → { object: { object_id, kind, symbol, name, cycle, underlying_object_id } }
 * Metadata only. Contracts and series come from
 * `getObjectFacetsV2` (aggregated dimensions) and `getObjectSeriesV2`
 * (filtered + paginated) — shipping them here was a 38 MB response.
 */
```

- [ ] **Step 6: Run the whole backend suite plus the boundary check**

Run:

```bash
uv run pytest -m "not integration" -q
uv run lint-imports --config .import-linter.cfg
```

Expected: both pass. Update any router test still asserting `contracts`/`series` on
`/objects/{id}`.

- [ ] **Step 7: Verify the payload shrank**

```bash
curl -s -o /dev/null -w "HTTP %{http_code} — %{time_total}s — %{size_download} bytes\n" \
  "http://127.0.0.1:8000/api/data-v2/objects/12"
```

Expected: well under 10 KB in well under 1 s. Baseline before this plan: 38 185 152 bytes in 33.9 s.

- [ ] **Step 8: Commit**

```bash
git add tcg/data/service_v2.py frontend/src/api/dataV2.js tests/unit/test_data_v2_service.py tests/unit/test_api_data_v2.py
git commit -m "perf(data-v2): object detail returns metadata only

Drops contracts+series from the response now that the frontend reads them
from the facets and paginated-series endpoints. EW2 went from 38.2 MB / 33.9 s
to a few hundred bytes."
```

---

## Task 11: Live-warehouse integration coverage

**Files:**
- Modify: `tests/integration/data/test_instruments_v2_integration.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4.
- Produces: no exports.

- [ ] **Step 1: Write the integration tests**

Append to `tests/integration/data/test_instruments_v2_integration.py`:

```python
@pytest.mark.integration
async def test_facets_on_the_largest_option_root(svc):
    """EW2 (object 12) is the worst case: 96 106 contracts, 200 672 series."""
    facets = await svc.get_object_facets(12)
    assert facets["kind"] == "option"
    assert len(facets["expirations"]) > 150
    assert facets["strike_min"] is not None
    assert facets["strike_max"] > facets["strike_min"]
    assert set(facets["option_types"]) == {"call", "put"}
    pairs = {(t["type"], t["freq"]) for t in facets["serie_types"]}
    assert ("bbba", "1m") in pairs
    assert facets["totals"]["series"] > 100_000


@pytest.mark.integration
async def test_facets_on_an_object_without_contracts(svc):
    """An index has no contracts — empty expirations is a normal answer."""
    facets = await svc.get_object_facets(5)
    assert facets["kind"] == "index"
    assert facets["expirations"] == []
    assert facets["strike_min"] is None
    assert facets["option_types"] == []
    assert facets["totals"]["series"] >= 1


@pytest.mark.integration
async def test_filtered_series_page_respects_limit_and_total(svc):
    page = await svc.list_object_series(
        12,
        expiration_min=date(2026, 3, 13),
        expiration_max=date(2026, 3, 13),
        serie_type="bbba",
        option_type="put",
        limit=50,
    )
    assert page["limit"] == 50
    assert len(page["items"]) <= 50
    assert page["total"] >= len(page["items"])
    for item in page["items"]:
        assert item["type"] == "bbba"
        assert item["option_type"] == "put"
        assert item["expiration"] == "2026-03-13"
        assert item["contract_code"], "contract metadata must arrive joined"


@pytest.mark.integration
async def test_paging_does_not_repeat_rows(svc):
    """A total ORDER BY is what makes LIMIT/OFFSET paging safe."""
    kw = dict(
        expiration_min=date(2026, 3, 13),
        expiration_max=date(2026, 3, 13),
        serie_type="bbba",
        option_type="put",
        limit=25,
    )
    first = await svc.list_object_series(12, skip=0, **kw)
    second = await svc.list_object_series(12, skip=25, **kw)
    ids_a = {i["serie_id"] for i in first["items"]}
    ids_b = {i["serie_id"] for i in second["items"]}
    assert ids_a and ids_b
    assert not (ids_a & ids_b), "pages overlap — ordering is not a total order"


@pytest.mark.integration
async def test_narrow_filter_returns_empty_not_error(svc):
    page = await svc.list_object_series(
        12, strike_min=999_999.0, strike_max=1_000_000.0
    )
    assert page["items"] == []
    assert page["total"] == 0


@pytest.mark.integration
async def test_intraday_series_timestamps_are_distinct(svc):
    """The regression test for the original defect.

    _ts_to_int collapsed every ts to YYYYMMDD, so a 1m series returned the
    same abscissa repeatedly and charted as a single point. Any future change
    that reintroduces date-collapsing on an intraday series fails here.
    """
    page = await svc.list_object_series(
        12, serie_type="bbba", freq="1m", limit=1
    )
    assert page["items"], "expected at least one 1m bbba series on EW2"
    serie_id = page["items"][0]["serie_id"]

    out = await svc.get_series(serie_id)
    ts = out["points"]["ts"]
    assert out["grain"] == "intraday"
    assert ts, f"serie {serie_id} returned no points"
    assert all(isinstance(t, str) and "T" in t for t in ts)
    assert len(set(ts)) == len(ts), "intraday timestamps collapsed onto one date"


@pytest.mark.integration
async def test_daily_series_still_returns_int_dates(svc):
    """v1-parity: daily series must keep YYYYMMDD ints."""
    page = await svc.list_object_series(5, serie_type="bar", freq="daily", limit=1)
    assert page["items"]
    out = await svc.get_series(page["items"][0]["serie_id"])
    assert out["grain"] == "daily"
    assert all(isinstance(t, int) for t in out["points"]["ts"])
```

- [ ] **Step 2: Open the tunnel and run them**

Run:

```bash
bash ~/.claude/skills/tcg-db/scripts/tunnel.sh
uv run pytest tests/integration/data/test_instruments_v2_integration.py --run-integration -v
```

Expected: PASS. `test_intraday_series_timestamps_are_distinct` is the one that must have failed
before Task 1.

- [ ] **Step 3: Run the whole suite and the boundary check**

Run:

```bash
uv run pytest -m "not integration" -q
uv run lint-imports --config .import-linter.cfg
cd frontend && npx vitest run && cd ..
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/data/test_instruments_v2_integration.py
git commit -m "test(data-v2): live coverage for facets, paging and intraday grain

Includes the regression test for the original defect: an intraday series must
return distinct timestamps, which the YYYYMMDD collapse made impossible."
```

---

## Task 12: Filter state in the URL

The spec's last frontend requirement: `?expiration=2026-03-13&type=put&serie_type=bbba` so the
back button works, a filter state is shareable by link, and a reload loses nothing.

`react-router-dom@^6.20` is already a dependency and `DataV2Page` is routed at `/data-v2`
(`frontend/src/App.jsx:64`), but **`useSearchParams` is not used anywhere in this codebase yet** —
there is no local precedent to copy, so follow the react-router v6 API directly.

Note for the tests: `src/test/setup.js` auto-wraps every RTL `render` in a `QueryClientProvider`,
but nothing provides a Router. A component calling `useSearchParams` throws outside one, so these
tests must wrap in `MemoryRouter`.

**Files:**
- Modify: `frontend/src/pages/DataV2/ObjectDetail.jsx`
- Modify: `frontend/src/pages/DataV2/SeriesFilterPanel.jsx` (accept `initialFilters`)
- Test: `frontend/src/pages/DataV2/SeriesFilterPanel.test.jsx`, `frontend/src/pages/DataV2/ObjectDetail.urlstate.test.jsx` (new)

**Interfaces:**
- Consumes: `SeriesFilterPanel` (Task 6), the filter state in `ObjectDetail` (Task 9).
- Produces: `<SeriesFilterPanel objectId initialFilters={filters|null} onApply />`. When
  `initialFilters` is non-null the panel starts pre-filled **and already applied**, so a shared
  link lists results without a click.

- [ ] **Step 1: Write the failing panel test for `initialFilters`**

Add to `frontend/src/pages/DataV2/SeriesFilterPanel.test.jsx`:

```javascript
  it('pre-fills from initialFilters and starts already applied', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(
      <SeriesFilterPanel
        objectId={12}
        initialFilters={{
          expirationMin: '2026-03-13',
          expirationMax: '2026-03-13',
          optionType: 'put',
          serieType: 'bbba',
          freq: '1m',
        }}
        onApply={onApply}
      />,
    );
    expect(screen.getByLabelText(/expiration/i)).toHaveValue('2026-03-13');
    expect(screen.getByLabelText(/series type/i)).toHaveValue('bbba');
    expect(screen.getByLabelText(/frequency/i)).toHaveValue('1m');

    // Already applied: a change auto-applies with no Apply click first.
    fireEvent.change(screen.getByLabelText(/option type/i), {
      target: { value: 'call' },
    });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(onApply.mock.calls[0][0]).toMatchObject({ optionType: 'call' });
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesFilterPanel.test.jsx -t initialFilters`
Expected: FAIL — the fields are empty (the prop is ignored) and `onApply` is never called.

- [ ] **Step 3: Accept `initialFilters` in the panel**

In `frontend/src/pages/DataV2/SeriesFilterPanel.jsx`, change the signature and the state seeds:

```javascript
function SeriesFilterPanel({ objectId, initialFilters = null, onApply }) {
  const { data: facets, loading, error } = useObjectFacetsV2(objectId);
  const uid = useId();

  const [expiration, setExpiration] = useState(initialFilters?.expirationMin || '');
  const [strikeMin, setStrikeMin] = useState(
    initialFilters?.strikeMin != null ? String(initialFilters.strikeMin) : '',
  );
  const [strikeMax, setStrikeMax] = useState(
    initialFilters?.strikeMax != null ? String(initialFilters.strikeMax) : '',
  );
  const [optionType, setOptionType] = useState(initialFilters?.optionType || 'both');
  const [serieType, setSerieType] = useState(initialFilters?.serieType || 'any');
  const [freq, setFreq] = useState(initialFilters?.freq || 'any');

  // A URL that already carries filters is an applied state: a shared link must
  // list results without the recipient re-clicking Apply.
  const [applied, setApplied] = useState(initialFilters != null);
```

- [ ] **Step 4: Run the panel tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DataV2/SeriesFilterPanel.test.jsx`
Expected: PASS (7 tests)

- [ ] **Step 5: Write the failing URL round-trip test**

Create `frontend/src/pages/DataV2/ObjectDetail.urlstate.test.jsx`:

```javascript
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, useSearchParams } from 'react-router-dom';

vi.mock('../../api/dataV2', () => ({
  getObjectDetailV2: vi.fn(),
  getObjectFacetsV2: vi.fn(),
  getObjectSeriesV2: vi.fn(),
  getSeriesV2: vi.fn(),
  getContinuousFuturesV2: vi.fn(),
  getV2FuturesCycles: vi.fn(),
  getContinuousOptionsV2: vi.fn(),
}));

vi.mock('../../components/Chart', () => ({
  default: () => <div data-testid="chart" />,
}));

import ObjectDetail from './ObjectDetail';
import {
  getObjectDetailV2,
  getObjectFacetsV2,
  getObjectSeriesV2,
} from '../../api/dataV2';

afterEach(cleanup);

const OBJECT = {
  object_id: 12, kind: 'option', symbol: 'OPT_SP_500_EW2',
  name: 'EW2 Weekly', cycle: 'weekly', underlying_object_id: 6,
};

function Spy() {
  const [params] = useSearchParams();
  return <div data-testid="qs">{params.toString()}</div>;
}

beforeEach(() => {
  vi.clearAllMocks();
  getObjectDetailV2.mockResolvedValue({ object: OBJECT });
  getObjectFacetsV2.mockResolvedValue({
    object_id: 12, kind: 'option',
    expirations: [{ expiration: '2026-03-13', contracts: 500 }],
    strike_min: 15, strike_max: 10600,
    option_types: ['call', 'put'],
    serie_types: [{ type: 'bbba', freq: '1m', series: 96106 }],
    totals: { contracts: 96106, series: 200672 },
  });
  getObjectSeriesV2.mockResolvedValue({
    items: [{
      serie_id: 1433194, contract_id: 77, type: 'bbba', freq: '1m',
      source: 'DATABENTO', contract_code: 'EW2H6 P6260.20260313',
      expiration: '2026-03-13', strike: 6260, option_type: 'put',
    }],
    total: 195, skip: 0, limit: 50,
  });
});

describe('ObjectDetail filter state in the URL', () => {
  it('writes the applied filter into the query string', async () => {
    render(
      <MemoryRouter initialEntries={['/data-v2']}>
        <ObjectDetail object={OBJECT} />
        <Spy />
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText(/series type/i), {
      target: { value: 'bbba' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() =>
      expect(screen.getByTestId('qs').textContent).toContain('serie_type=bbba'),
    );
  });

  it('restores an applied filter from the query string and fetches at once', async () => {
    render(
      <MemoryRouter
        initialEntries={['/data-v2?serie_type=bbba&freq=1m&option_type=put']}
      >
        <ObjectDetail object={OBJECT} />
      </MemoryRouter>,
    );
    // No Apply click: the URL already expresses an applied filter.
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeInTheDocument();
    await waitFor(() => expect(getObjectSeriesV2).toHaveBeenCalled());
    const [, sent] = getObjectSeriesV2.mock.calls[0];
    expect(sent).toMatchObject({ serieType: 'bbba', freq: '1m', optionType: 'put' });
  });

  it('records the page in the URL so paging survives a reload', async () => {
    render(
      <MemoryRouter initialEntries={['/data-v2?serie_type=bbba']}>
        <ObjectDetail object={OBJECT} />
        <Spy />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole('button', { name: /next/i }));
    await waitFor(() =>
      expect(screen.getByTestId('qs').textContent).toContain('skip=50'),
    );
  });
});
```

- [ ] **Step 6: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/pages/DataV2/ObjectDetail.urlstate.test.jsx`
Expected: FAIL — the query string stays empty; filters live only in component state.

- [ ] **Step 7: Back the filter state with the URL**

In `frontend/src/pages/DataV2/ObjectDetail.jsx`, add the import:

```javascript
import { useSearchParams } from 'react-router-dom';
```

Replace the `filters` / `skip` `useState` pair from Task 9 with URL-derived state:

```javascript
  const [searchParams, setSearchParams] = useSearchParams();
  const LIMIT = 50;

  // Filters live in the URL: the back button works, a filter state is
  // shareable by link, and a reload loses nothing. A URL carrying any filter
  // key is an APPLIED state — the recipient of a link must not have to press
  // Apply. No filter keys → null → the series query stays disabled (the gate).
  const filters = useMemo(() => {
    const keys = {
      expirationMin: searchParams.get('expiration_min') || searchParams.get('expiration'),
      expirationMax: searchParams.get('expiration_max') || searchParams.get('expiration'),
      strikeMin: searchParams.get('strike_min'),
      strikeMax: searchParams.get('strike_max'),
      optionType: searchParams.get('option_type'),
      serieType: searchParams.get('serie_type'),
      freq: searchParams.get('freq'),
    };
    const present = Object.values(keys).some((v) => v != null && v !== '');
    if (!present) return null;
    return {
      expirationMin: keys.expirationMin || undefined,
      expirationMax: keys.expirationMax || undefined,
      strikeMin: keys.strikeMin == null || keys.strikeMin === '' ? undefined : Number(keys.strikeMin),
      strikeMax: keys.strikeMax == null || keys.strikeMax === '' ? undefined : Number(keys.strikeMax),
      optionType: keys.optionType || 'both',
      serieType: keys.serieType || 'any',
      freq: keys.freq || 'any',
    };
  }, [searchParams]);

  const skip = Number(searchParams.get('skip') || 0);
```

Then replace `handleApply` and wire paging through the URL:

```javascript
  function writeParams(next, nextSkip) {
    const p = new URLSearchParams();
    if (next) {
      if (next.expirationMin) p.set('expiration_min', next.expirationMin);
      if (next.expirationMax) p.set('expiration_max', next.expirationMax);
      if (next.strikeMin != null) p.set('strike_min', String(next.strikeMin));
      if (next.strikeMax != null) p.set('strike_max', String(next.strikeMax));
      if (next.optionType && next.optionType !== 'both') p.set('option_type', next.optionType);
      if (next.serieType && next.serieType !== 'any') p.set('serie_type', next.serieType);
      if (next.freq && next.freq !== 'any') p.set('freq', next.freq);
    }
    if (nextSkip) p.set('skip', String(nextSkip));
    setSearchParams(p);
  }

  // A new filter always starts from the first page; paging keeps the filter.
  const handleApply = (next) => writeParams(next, 0);
  const handlePageChange = (nextSkip) => writeParams(filters, nextSkip);
```

Pass `initialFilters={filters}` to `SeriesFilterPanel`, and `onPageChange={handlePageChange}`
plus `skip={page?.skip ?? skip}` to `SeriesResultList`. Keep `query` derived as in Task 9:

```javascript
  const query = useMemo(
    () => (filters ? { ...filters, skip, limit: LIMIT } : null),
    [filters, skip],
  );
```

One caveat to respect: `SeriesFilterPanel` seeds its fields from `initialFilters` **once** (at
mount). Give the panel a `key` that changes only with the object, not with the filter, so typing
in it is never interrupted by the URL write it just caused:

```javascript
            <SeriesFilterPanel
              key={object.object_id}
              objectId={object.object_id}
              initialFilters={filters}
              onApply={handleApply}
            />
```

- [ ] **Step 8: Run the URL tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/DataV2/ObjectDetail.urlstate.test.jsx`
Expected: PASS (3 tests)

- [ ] **Step 9: Run the whole frontend suite**

Run: `cd frontend && npx vitest run`
Expected: PASS. `DataV2Page.test.jsx` renders `DataV2Page` inside the app's router in the real
app, but if it renders the page bare it will now throw on `useSearchParams` — wrap its renders in
`MemoryRouter` if so.

- [ ] **Step 10: Verify in the running app**

Apply a filter, copy the URL, open it in a new tab. Expected: the same filtered list appears with
no Apply click. Press the browser back button. Expected: the previous filter returns.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/pages/DataV2/ObjectDetail.jsx frontend/src/pages/DataV2/SeriesFilterPanel.jsx frontend/src/pages/DataV2/SeriesFilterPanel.test.jsx frontend/src/pages/DataV2/ObjectDetail.urlstate.test.jsx frontend/src/pages/DataV2/DataV2Page.test.jsx
git commit -m "feat(data-v2): keep series filter state in the URL

Back button, shareable links and reload-safety. A URL carrying filter keys is
an applied state, so a shared link lists results without re-pressing Apply."
```

---

## Verification checklist

Before opening the PR, confirm each of these against the running app:

- [ ] `GET /api/data-v2/objects/12` returns under 10 KB (was 38.2 MB).
- [ ] Clicking `OPT_SP_500_EW2` shows the filter panel immediately; no freeze.
- [ ] Applying expiration + `bbba` + `1m` lists ≤ 50 rows with a correct total.
- [ ] Paging forward and back never repeats a row.
- [ ] A `1m` series charts on a real datetime axis with distinct minutes.
- [ ] A `daily` series still charts on a date axis (v1 parity intact).
- [ ] `IND_SP_500` (no contracts) shows only the series-type and frequency controls.
- [ ] A filter URL opened in a fresh tab reproduces the same list with no Apply click, and the
      browser back button restores the previous filter.
- [ ] Continuous (Options) with `criterion=moneyness` returns 200 (was 502).
- [ ] `uv run pytest -m "not integration"`, `uv run lint-imports --config .import-linter.cfg`, and `npx vitest run` all pass.
