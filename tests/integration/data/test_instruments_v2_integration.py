"""Live-dwh integration tests for the Database v2 backend.

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by the
``DWH_*`` connection variables being present (``load_dwh_config`` raises
otherwise -> skip). Reads the 5 live v2 objects (RATE_US_CMT_1M, RATE_US_SOFR_ON,
IND_SP_500, FUT_SP_500, OPT_SP_500_EW3) READ-ONLY via ``tcg_read``.

Verifies:
  * every live object lists with the right kind;
  * the object-facets aggregate's SQL semantics (the unit tests use a fake
    cursor and never execute a statement);
  * the filtered series page's WHERE / ORDER BY / LIMIT-OFFSET semantics, for
    the same reason — plus that its LEFT JOIN keeps contract-less (index / rate)
    series listable;
  * fact-table dispatch reads the index bar series and a rate value series;
  * futures continuous on FUT_SP_500 stitches a multi-contract series;
  * options continuous on OPT_SP_500_EW3 selects by strike and by moneyness,
    and rejects delta with a clean ValidationError.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.instruments_v2 import V2_SCHEMA
from tcg.data.service_v2 import DefaultMarketDataServiceV2
from tcg.types.errors import ValidationError
from tcg.types.market import AdjustmentMethod, ContinuousRollConfig, RollStrategy


@pytest.fixture
async def svc():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield DefaultMarketDataServiceV2(pool)
    await pool.close()


@pytest.mark.integration
async def test_lists_five_live_objects(svc):
    objs = await svc.list_objects()
    by_symbol = {o["symbol"]: o for o in objs}
    for sym in (
        "RATE_US_CMT_1M",
        "RATE_US_SOFR_ON",
        "IND_SP_500",
        "FUT_SP_500",
        "OPT_SP_500_EW3",
    ):
        assert sym in by_symbol, f"missing live object {sym}"
    assert by_symbol["IND_SP_500"]["kind"] == "index"
    assert by_symbol["FUT_SP_500"]["kind"] == "future"
    assert by_symbol["OPT_SP_500_EW3"]["kind"] == "option"
    assert by_symbol["RATE_US_CMT_1M"]["kind"] == "rate"
    # Derivative -> underlying wiring.
    assert (
        by_symbol["FUT_SP_500"]["underlying_object_id"]
        == by_symbol["IND_SP_500"]["object_id"]
    )


@pytest.mark.integration
async def test_object_detail_and_series_dispatch(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    ind_id = objs["IND_SP_500"]["object_id"]
    detail = await svc.get_object_detail(ind_id)
    assert detail["object"]["symbol"] == "IND_SP_500"
    # Metadata only: the bulk lists left this response (they were 38 MB on the
    # big option root). Series now come from the paginated endpoint.
    assert set(detail) == {"object"}
    page = await svc.list_object_series(ind_id, serie_type="bar", limit=50)
    # index has one non-contract bar series.
    bar_series = [s for s in page["items"] if s["type"] == "bar"]
    assert bar_series
    serie_id = bar_series[0]["serie_id"]
    result = await svc.get_series(
        serie_id, start=date(2020, 1, 1), end=date(2020, 12, 31)
    )
    assert result["type"] == "bar"
    assert "close" in result["fields"]
    assert len(result["points"]["ts"]) > 100  # a year of index bars
    assert len(result["points"]["close"]) == len(result["points"]["ts"])


@pytest.mark.integration
async def test_object_facets_semantics_live(svc):
    """Execute the facets SQL for real — the unit tests cannot.

    ``tests/unit/data/sql/test_sql_instruments_v2_facets.py`` drives the real
    method through a fake cursor, so it pins the Python shaping and the SQL
    *text* but never runs a statement. Three plausible edits to the SQL therefore
    leave all ten unit tests green while corrupting the filter form:

      * ``MIN(strike)``/``MAX(strike)`` transposed -> an inverted slider
        (``strike_min: 10600.0, strike_max: 15.0``), silently;
      * the serie read's predicate changed to ``WHERE contract_id = %s`` ->
        ``serie_types: []`` and ``totals.series: 0`` for every object. Note the
        unit test's ``assert params == (12,)`` looks like it pins the predicate
        but does not: both variants bind the same parameter;
      * ``ARRAY_AGG(DISTINCT option_type)`` losing its ``DISTINCT`` -> one entry
        per contract, so a ~9 KB payload becomes a ~96 000-element list.

    Assertions are relationships, not frozen counts: the warehouse is being
    backfilled, so every absolute number here drifts run to run.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW2"]["object_id"]
    facets = await svc.get_object_facets(opt_id)

    assert facets["kind"] == "option"

    # Ordered strike bounds. An option root spans many strikes, so min and max
    # genuinely differ and a transposition is visible.
    assert facets["strike_min"] is not None
    assert facets["strike_max"] is not None
    assert facets["strike_min"] < facets["strike_max"]

    # Exactly the two option types, deduplicated. A missing DISTINCT returns one
    # element per contract instead of one per type.
    assert facets["option_types"] == ["call", "put"]

    # The serie groups must be read for the OBJECT. Bound to contract_id instead
    # this returns nothing (contract_id 12 carries no series at all).
    pairs = {(s["type"], s["freq"]) for s in facets["serie_types"]}
    assert pairs, "serie_types empty — the serie read is not scoped by object_id"
    assert ("bbba", "1m") in pairs, f"expected a bbba:1m group, got {sorted(pairs)}"
    # Every one of this object's contracts carries at least one serie (verified
    # live: 0 of 96 194 without), and every expiration carries at least one
    # contract, so the series total cannot be smaller than the expiration count.
    # A single contract's groups total single digits, hundreds short of this.
    assert facets["totals"]["series"] >= len(facets["expirations"])

    # The two ``contract`` reads must agree: the per-expiration counts sum to the
    # total. Losing the object_id predicate on either one breaks this (the whole
    # table is ~5x this object).
    assert facets["expirations"], "an option root must have expirations"
    assert (
        sum(e["contracts"] for e in facets["expirations"])
        == facets["totals"]["contracts"]
    )

    # An index has no contracts at all: empty expirations and NULL strike bounds
    # are the correct answer, not an error.
    ind = await svc.get_object_facets(objs["IND_SP_500"]["object_id"])
    assert ind["expirations"] == []
    assert ind["strike_min"] is None
    assert ind["strike_max"] is None
    assert ind["option_types"] == []
    assert ind["totals"]["contracts"] == 0


@pytest.mark.integration
async def test_series_page_filters_live(svc):
    """Execute the filtered-page WHERE clause for real — the unit tests cannot.

    ``tests/unit/data/sql/test_sql_instruments_v2_series_page.py`` drives the real
    method through a fake cursor, so it pins the statement TEXT and the Python
    shaping but never runs a statement. Semantics need a database.

    Each assertion below is chosen because it changes value between the correct
    SQL and a specific plausible mutation:

      * ``s.type`` / ``s.freq`` rebound to each other -> zero rows (no serie has
        ``type = '1m'``), caught by the ``total > 0`` guards;
      * ``c.option_type`` predicate dropped -> calls appear in a put-only page;
      * ``expiration_min``/``expiration_max`` transposed -> ``>= hi AND <= lo``
        over an asymmetric window is empty, caught by ``total > 0``;
      * either expiration bound DROPPED -> widening that bound would no longer
        change the total, which is what the two ``wider_* > base`` asserts pin.
        A per-row range check cannot see this: ordered by expiration ascending,
        a 500-row page of a 2 261-row result never reaches the upper end of the
        window, so rows beyond it are invisible whether or not they are excluded;
      * either strike bound dropped or transposed -> the narrow strike window
        below returns fewer than ``total`` rows, so every row IS observable and
        the range check bites.

    Absolute counts are never asserted: the warehouse is being backfilled and
    every number here drifts run to run. Windows are derived from the live
    facets on each run for the same reason.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW2"]["object_id"]

    facets = await svc.get_object_facets(opt_id)
    unfiltered = await svc.list_object_series(opt_id, limit=1)
    # The join must neither drop series nor fan them out: the unfiltered page
    # total has to agree with the independent facets aggregate (which never
    # joins ``contract`` at all). A join condition on the wrong column would
    # multiply rows; an INNER join would drop the contract-less ones.
    assert unfiltered["total"] == facets["totals"]["series"]
    assert unfiltered["total"] > 1000, "expected a large option root to page"

    exps = sorted(e["expiration"] for e in facets["expirations"])
    assert len(exps) > 12, f"need a deep expiration ladder, got {len(exps)}"
    # A window with expirations on BOTH sides, so each bound has something to
    # exclude. Anchoring on exps[-1] would make expiration_max a no-op filter and
    # the assertion below unable to fail.
    below, lo, hi, above = exps[-10], exps[-9], exps[-4], exps[-3]
    assert below < lo < hi < above

    async def total_for(**kw):
        return (await svc.list_object_series(opt_id, limit=1, **kw))["total"]

    common = {"serie_type": "value", "option_type": "put"}
    base = await total_for(
        expiration_min=date.fromisoformat(lo),
        expiration_max=date.fromisoformat(hi),
        **common,
    )
    assert base > 0, f"no value/put series in {lo}..{hi} — window lost its data"
    assert base < unfiltered["total"], "the filters did not narrow anything"
    wider_lo = await total_for(
        expiration_min=date.fromisoformat(below),
        expiration_max=date.fromisoformat(hi),
        **common,
    )
    wider_hi = await total_for(
        expiration_min=date.fromisoformat(lo),
        expiration_max=date.fromisoformat(above),
        **common,
    )
    assert wider_lo > base, "expiration_min is not bounding the LOW side"
    assert wider_hi > base, "expiration_max is not bounding the HIGH side"

    # Row-level column bindings, on one expiration so the page covers the window.
    # ``freq='1m'`` (not ``serie_type``) is filtered here on purpose: object 12
    # carries ~188 000 daily and ~13 000 minute series, so a dropped or rebound
    # freq predicate lets ``value``/``greeks`` rows in and the type check fails.
    one_exp = {
        "expiration_min": date.fromisoformat(lo),
        "expiration_max": date.fromisoformat(lo),
    }
    page = await svc.list_object_series(
        opt_id, freq="1m", option_type="put", limit=500, **one_exp
    )
    assert page["total"] > 0
    assert len(page["items"]) == min(page["total"], 500)
    for it in page["items"]:
        assert it["freq"] == "1m", f"freq filter leaked: {it}"
        assert it["type"] in {"bar", "bbba"}, f"a daily serie type at freq=1m: {it}"
        assert it["option_type"] == "put", f"option_type filter leaked: {it}"
        assert it["expiration"] == lo
        assert isinstance(it["strike"], float), f"strike not coerced: {it['strike']!r}"
        assert it["contract_code"], "the LEFT JOIN did not bring contract metadata"

    # serie_type binds to ``s.type`` and genuinely narrows.
    typed = await svc.list_object_series(
        opt_id, serie_type="bbba", limit=200, **one_exp
    )
    assert typed["total"] > 0
    assert {i["type"] for i in typed["items"]} == {"bbba"}
    assert typed["total"] < await total_for(**one_exp)

    # Strike bounds, over a window narrow enough that the page holds every row —
    # only then does a per-row range check see a dropped upper bound.
    strikes = sorted({i["strike"] for i in page["items"]})
    assert len(strikes) > 8, f"need several strikes on {lo}, got {len(strikes)}"
    smin, smax = strikes[2], strikes[6]
    struck = await svc.list_object_series(
        opt_id,
        freq="1m",
        option_type="put",
        strike_min=smin,
        strike_max=smax,
        limit=500,
        **one_exp,
    )
    assert 0 < struck["total"] < page["total"]
    assert len(struck["items"]) == struck["total"], "narrow window must fit one page"
    assert all(smin <= i["strike"] <= smax for i in struck["items"]), (
        f"strike filter leaked outside [{smin}, {smax}]: "
        f"{sorted({i['strike'] for i in struck['items']})}"
    )
    # Strikes exist strictly below smin and strictly above smax (strikes[0..1] and
    # strikes[7..]), so both bounds had something to exclude and did.
    assert strikes[0] < smin and smax < strikes[-1]

    # A filter matching nothing is a result, not an error.
    empty = await svc.list_object_series(opt_id, strike_min=10**9, limit=50)
    assert empty["items"] == []
    assert empty["total"] == 0


@pytest.mark.integration
async def test_series_page_paging_is_stable_live(svc):
    """LIMIT/OFFSET paging must be a deterministic slice of one total order.

    The ORDER BY here is correctness, not presentation. Two of the three obvious
    ways to test it DO NOT DISCRIMINATE, measured on this warehouse against the
    real mutation (dropping ``s.serie_id`` from the ORDER BY, three trials each):

      * page 1 and page 2 being DISJOINT: overlap was 0 in BOTH states. The
        brief's prescribed proof is a no-op here — Postgres happens to produce a
        stable enough order for adjacent offsets under the same plan;
      * the two pages' UNION as a SET equalling a single ``LIMIT 2*n`` fetch:
        also equal in both states.

    What does discriminate, stably:

      * the full key ``(expiration, strike, option_type, serie_id)`` STRICTLY
        increasing across the concatenated pages — without the tiebreaker,
        rows sharing the leading three keys come back in arbitrary serie_id
        order (measured False);
      * the ORDERED concatenation of the two pages equalling the single
        ``LIMIT 2*n`` fetch (measured False without the tiebreaker). This is the
        property paging actually needs: page *k* of a walk must be slice *k* of
        the whole ordered result.

    Both are logical consequences of a TOTAL order, so they are guaranteed green
    on correct SQL rather than merely observed green.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW2"]["object_id"]

    n = 50
    p1 = (await svc.list_object_series(opt_id, skip=0, limit=n))["items"]
    p2 = (await svc.list_object_series(opt_id, skip=n, limit=n))["items"]
    both = (await svc.list_object_series(opt_id, skip=0, limit=2 * n))["items"]
    assert len(p1) == len(p2) == n
    assert len(both) == 2 * n

    def key(i):
        return (i["expiration"], i["strike"], i["option_type"], i["serie_id"])

    walked = [key(i) for i in p1 + p2]
    assert all(k[0] is not None and k[1] is not None for k in walked), (
        "this object's series must all carry a contract for the key to be "
        "type-comparable; see test_series_page_lists_object_level_series_live "
        "for the contract-less case"
    )

    # The window must actually contain duplicate leading keys, or the tiebreaker
    # is idle and none of this can fail. Object 12 carries up to four series per
    # contract (value/greeks daily + bar/bbba 1m); measured 67 distinct leading
    # keys in the first 100 rows.
    leading = [k[:3] for k in walked]
    assert len(set(leading)) < len(leading), (
        "no duplicate (expiration, strike, option_type) in this window — the "
        "tiebreaker assertions below cannot fail, so the test is vacuous"
    )

    assert all(walked[i] < walked[i + 1] for i in range(len(walked) - 1)), (
        "the ordering key is not strictly increasing across the two pages — "
        "LIMIT/OFFSET paging can repeat or skip rows"
    )
    assert [key(i) for i in both] == walked, (
        "a two-page walk does not equal the single ordered fetch of the same "
        "rows — the ORDER BY is not a total order"
    )
    # Non-overlap is implied by the above; asserted only as documentation of the
    # user-visible symptom. On its own it would not discriminate (see docstring).
    assert not ({i["serie_id"] for i in p1} & {i["serie_id"] for i in p2})

    # OFFSET past the end is an empty page, not an error, and the total is
    # unaffected by paging.
    tail = await svc.list_object_series(opt_id, skip=10**9, limit=n)
    assert tail["items"] == []
    assert tail["total"] > 0
    assert tail["skip"] == 10**9 and tail["limit"] == n


@pytest.mark.integration
async def test_series_page_lists_object_level_series_live(svc):
    """The LEFT JOIN is load-bearing: index and rate series have no contract.

    An object-level serie carries ``contract_id IS NULL``, so an INNER JOIN onto
    ``contract`` drops it — and for an index or a rate object that is its ENTIRE
    series list. The failure would read as "this object has no data" rather than
    as an error, and no assertion on the big option root can see it (every one of
    object 12's series has a contract).

    ``total == len(expected) > 0`` is the discriminating form: under an INNER
    JOIN both the count and the page collapse to 0 while still being
    self-consistent, so comparing the page against its own total would stay green.

    The oracle therefore has to come from OUTSIDE ``list_series_filtered``. It
    used to be ``get_object_detail(oid)["series"]``, but that response is now
    metadata only, so it reads the reader's ``list_series`` directly instead —
    a plain ``SELECT ... FROM serie WHERE object_id = %s`` with no join at all,
    which is exactly the independence this test needs. Reaching through
    ``svc._reader`` is deliberate: routing the oracle through any service method
    that also joins ``contract`` would make the comparison circular.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    for symbol in ("IND_SP_500", "RATE_US_CMT_1M", "RATE_US_SOFR_ON"):
        oid = objs[symbol]["object_id"]
        expected = {s["serie_id"] for s in await svc._reader.list_series(oid)}
        assert expected, f"{symbol} has no series at all — fixture assumption broke"

        page = await svc.list_object_series(oid, limit=500)
        assert page["total"] == len(expected), (
            f"{symbol}: page total {page['total']} != {len(expected)} series in "
            f"the object — an INNER JOIN would report 0"
        )
        assert {i["serie_id"] for i in page["items"]} == expected
        for i in page["items"]:
            assert i["contract_id"] is None, f"{symbol}: unexpected contract {i}"
            assert i["contract_code"] is None
            assert i["expiration"] is None  # not a crash on None.isoformat()
            assert i["strike"] is None
            assert i["option_type"] is None
            assert i["type"] and i["freq"], f"{symbol}: serie metadata missing {i}"


@pytest.mark.integration
async def test_rate_value_series(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    rate_id = objs["RATE_US_CMT_1M"]["object_id"]
    page = await svc.list_object_series(rate_id, limit=50)
    assert page["items"], "RATE_US_CMT_1M has no series — fixture assumption broke"
    serie_id = page["items"][0]["serie_id"]
    result = await svc.get_series(serie_id)
    assert result["type"] == "value"
    assert result["fields"] == ["value"]
    assert len(result["points"]["value"]) > 0


@pytest.mark.integration
async def test_futures_continuous_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    fut_id = objs["FUT_SP_500"]["object_id"]
    cfg = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.RATIO
    )
    series = await svc.get_continuous_future(fut_id, cfg)
    assert series is not None
    assert len(series.prices) > 1000  # ES 2010.. -> thousands of bars
    assert len(series.contracts) > 10  # many quarterly contracts stitched
    assert len(series.roll_dates) > 10
    cycles = await svc.get_future_cycles(fut_id)
    assert cycles == ["quarterly"]


@pytest.mark.integration
async def test_front_closes_are_daily_and_one_per_date(svc):
    """The moneyness spot must come from daily bars, not minute bars.

    ``FUT_SP_500`` carries both ``bar:daily`` and ``bar:1m`` series. Without an
    ``s.freq = 'daily'`` filter this query also scans fact_bar's minute rows:
    over a wide window it times out, and per date it returns thousands of rows
    whose first entry may be a 00:00 minute bar — which
    ``_front_close_by_date`` (first row per date wins) would then report as the
    front close. On 2026-03-10 that is 6771.0 (00:00 minute bar) instead of
    6797.0 (daily close), and the two share the same ``ts`` so ``ORDER BY
    f.ts, c.expiration`` cannot even break the tie deterministically.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    fut_id = objs["FUT_SP_500"]["object_id"]
    rows = await svc._reader.fetch_future_front_closes(
        fut_id, start=date(2026, 3, 1), end=date(2026, 3, 31)
    )
    assert rows, "expected daily front-close rows for FUT_SP_500 in March 2026"
    # At daily grain there is exactly one row per (date, expiration), so a date
    # carries as many rows as it has live contracts — single digits for ES.
    # Minute rows give hundreds to thousands per date. Note it has to be the
    # *row* count: the set of distinct expirations per date is identical at
    # either grain, so counting expirations would not discriminate at all.
    per_date: dict[int, int] = {}
    for r in rows:
        per_date[r["ts_int"]] = per_date.get(r["ts_int"], 0) + 1
    worst = max(per_date.values())
    assert worst < 20, f"{worst} rows on one date — minute bars leaked in"
    pairs = [(r["ts_int"], r["expiration_int"]) for r in rows]
    assert len(set(pairs)) == len(pairs), "duplicate (date, expiration) rows"


@pytest.mark.integration
async def test_future_contract_bars_are_daily_with_unique_dates(svc):
    """The continuous-futures feed must read daily bars only.

    Same root cause as ``test_front_closes_are_daily_and_one_per_date``, one
    method over: ``FUT_SP_500`` carries both ``bar:daily`` (69 contracts,
    17 146 rows) and ``bar:1m`` (12 contracts, 980 194 rows) series, and this
    method is called with an unbounded ``ts`` range. Without an
    ``s.freq = 'daily'`` filter it scans the whole minute history — 60 s
    statement timeout — and, because ``PriceSeries.dates`` are ``YYYYMMDD``
    ints, every surviving minute row collapses onto a duplicate date inside its
    contract bucket, handing the ``ContinuousSeriesBuilder`` a series with
    repeated dates. Corrupt input, not just slow input.

    The pin is lossless: measured on the live warehouse, all 12 contracts with
    ``1m`` series also have a ``daily`` series, no contract has ``1m`` facts
    without ``daily`` facts, and all 634 distinct ``1m`` dates appear among the
    5 012 ``daily`` dates — so no contract and no date is dropped. That
    losslessness is not assumed here, it is re-derived against the dimension on
    every run (see below), because the pin's one real failure mode is a
    contract that exists *only* at another grain.
    """
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    fut_id = objs["FUT_SP_500"]["object_id"]

    # Losslessness, exactly and without a threshold: every contract the
    # type-only filter would see must also carry a `daily` bar serie. A
    # threshold ("more than N contracts survived") cannot do this job — the
    # realistic regression is the minute feed for a newly listed contract going
    # live before its daily backfill lands, which drops exactly one contract,
    # and that one is the front contract the roller stitches onto. This is a
    # dimension-only scan (no fact join), so it costs well under a second.
    async with svc._reader._pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT c.contract_code,
                           bool_or(s.freq = 'daily') AS has_daily
                    FROM {V2_SCHEMA}.serie s
                    JOIN {V2_SCHEMA}.contract c
                      ON c.contract_id = s.contract_id
                    WHERE s.object_id = %s
                      AND s.type = 'bar'
                      AND c.expiration IS NOT NULL
                    GROUP BY c.contract_id, c.contract_code""",
                (fut_id,),
            )
            grain = await cur.fetchall()
    assert grain, "expected bar-carrying contracts for FUT_SP_500"
    grain_only = sorted(r["contract_code"] for r in grain if not r["has_daily"])
    assert not grain_only, (
        f"{len(grain_only)} of {len(grain)} contracts have bar series but none "
        f"at freq='daily', so the pin drops them from the roller entirely: "
        f"{grain_only}"
    )

    contracts = await svc._reader.fetch_future_contract_bars(fut_id, "quarterly")
    assert contracts, "expected contract bars for FUT_SP_500"

    for cpd in contracts:
        dates = list(cpd.prices.dates)
        assert len(set(dates)) == len(dates), (
            f"{cpd.contract_id}: duplicate dates — minute rows collapsed "
            f"onto YYYYMMDD ints ({len(dates)} rows, "
            f"{len(set(dates))} distinct)"
        )


@pytest.mark.integration
async def test_options_continuous_strike_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW3"]["object_id"]
    res = await svc.get_continuous_options(
        opt_id,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert res.dates  # at least the 2024-06-18 settlement date
    assert all(v > 0 for v in res.values)  # false-zero guard held
    # The selected contract must be a 5000-strike put around that expiry.
    assert any("5000" in c for c in res.contracts)


@pytest.mark.integration
async def test_options_continuous_moneyness_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW3"]["object_id"]
    res = await svc.get_continuous_options(
        opt_id,
        criterion="moneyness",
        target=1.0,
        option_type="put",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert res.dates
    assert all(v > 0 for v in res.values)


@pytest.mark.integration
async def test_options_continuous_delta_rejected_live(svc):
    objs = {o["symbol"]: o for o in await svc.list_objects()}
    opt_id = objs["OPT_SP_500_EW3"]["object_id"]
    with pytest.raises(ValidationError, match="greeks"):
        await svc.get_continuous_options(
            opt_id, criterion="delta", target=0.1, option_type="put"
        )
