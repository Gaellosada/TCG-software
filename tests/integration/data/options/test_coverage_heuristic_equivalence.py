"""Live-dwh correctness gate for the coverage EXACT-reader fix.

``GET /api/options/coverage`` derives an option leg's ``(first, last)`` window,
which feeds BOTH the portfolio cache-status label AND the backtest COMPUTE
window — so a fast-but-wrong value is a correctness bug, not a cosmetic one. The
fix replaced two full-scan reads (v1 ``cycle_trade_date_span`` partition
seq-scan; the cold ``option_trade_date_coverage`` pre-fetch) with an EXACT read:
v1 aggregates a per-contract ``LATERAL`` PK min/max; v2 scans only the cycle's
routed objects. Both are byte-identical to the pre-fix full scan — index-only
(v1) / object-scoped (v2) — and inherently robust to phantom dim contracts
(a listed-but-never-traded contract that made the rejected single-representative
heuristic return a WRONG ``None`` start).

This module is the regression guard: for the coverage params the 26 real
portfolios use (``OPT_SP_500`` × {v1,v2} × {None, M, W, W3 Friday}) plus synthetic
weekly cycles that DO contain phantom contracts (W1/W2/W4), it runs the NEW
reader method AND the pre-fix EXACT full-scan SQL (inlined as ground truth) and
asserts the two ``(first, last)`` tuples are byte-identical. Divergence on any
param = wrong compute window = FAIL.

Gated by ``--run-integration`` and by the ``DWH_*`` variables. Reads ``tcg_read``
(READ-ONLY). The pre-fix reference is the slow scan, so the module lifts
``statement_timeout`` for those ground-truth queries only.
"""

from __future__ import annotations

import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.options import SqlOptionsDataReader
from tcg.data._v2_compat._mapping import ALL_OPTION_OBJECT_IDS
from tcg.data._v2_compat.options_reader import V2OptionsDataReader, _route_objects

pytestmark = pytest.mark.integration

_ROOT = "OPT_SP_500"
_SCHEMA = "tcg_instruments"
_V2 = "tcg_instruments_v2"


@pytest.fixture
async def pool():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield pool
    await pool.close()


async def _agg(pool, sql, params):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET statement_timeout = '300s'")
            await cur.execute(sql, params)
            r = await cur.fetchone()
    return (r["lo"], r["hi"]) if r else (None, None)


async def _v1_exact_cycle(pool, cycle):
    """The pre-fix v1 shape: unbounded partition-wide hash join (ground truth)."""
    return await _agg(
        pool,
        f"""
        WITH ids AS (
            SELECT instrument_id FROM {_SCHEMA}.dim_instrument
            WHERE source_collection = %s AND asset_class = 'option'
              AND expiration IS NOT NULL AND expiration_cycle = %s
        )
        SELECT min(p.trade_date) AS lo, max(p.trade_date) AS hi
        FROM ids i JOIN {_SCHEMA}.fact_price_eod p
          ON p.instrument_id = i.instrument_id
        """,
        [_ROOT, cycle],
    )


async def _v2_exact_objects(pool, objects):
    lo, hi = await _agg(
        pool,
        f"""
        SELECT min(fv.ts) AS lo, max(fv.ts) AS hi
        FROM {_V2}.serie sv JOIN {_V2}.fact_value fv ON fv.serie_id = sv.serie_id
        WHERE sv.type = 'value' AND sv.object_id = ANY(%s)
        """,
        [objects],
    )
    return (lo.date() if lo else None, hi.date() if hi else None)


# --------------------------------------------------------------------------- #
# v1 — LATERAL exact == pre-fix hash-join extremes (real + phantom-bearing)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cycle", ["M", "W3 Friday", "W1 Friday", "W2 Friday"])
async def test_v1_cycle_span_matches_exact_fullscan(pool, cycle):
    v1 = SqlOptionsDataReader(pool)
    new = await v1.cycle_trade_date_span(_ROOT, cycle=cycle)  # unbounded
    exact = await _v1_exact_cycle(pool, cycle)
    assert new == exact, f"v1 {cycle}: LATERAL {new} != exact {exact}"
    # A cycle with contracts must yield real (non-None) bounds — the phantom
    # trap the single-representative heuristic fell into.
    assert new[0] is not None and new[1] is not None


async def test_v1_bounded_equals_unbounded_when_window_covers_data(pool):
    """The optional window is a no-op when it spans the data (the collection
    case), so bounded == unbounded — proving the dropped pre-fetch is safe."""
    v1 = SqlOptionsDataReader(pool)
    coll_first, coll_last = await v1.trade_date_coverage(_ROOT)
    unbounded = await v1.cycle_trade_date_span(_ROOT, cycle="W3 Friday")
    bounded = await v1.cycle_trade_date_span(
        _ROOT, coll_first, coll_last, cycle="W3 Friday"
    )
    assert bounded == unbounded


# --------------------------------------------------------------------------- #
# v2 — no-cycle + cycle spans == pre-fix full-scan extremes
# --------------------------------------------------------------------------- #
async def test_v2_trade_date_coverage_matches_exact_fullscan(pool):
    v2 = V2OptionsDataReader(pool)
    new = await v2.trade_date_coverage(_ROOT)
    exact = await _v2_exact_objects(pool, list(ALL_OPTION_OBJECT_IDS))
    assert new == exact, f"v2 no-cycle: {new} != exact {exact}"


@pytest.mark.parametrize("cycle", ["W", "W3 Friday", "W1 Friday", "W2 Friday"])
async def test_v2_cycle_span_matches_exact_fullscan(pool, cycle):
    v2 = V2OptionsDataReader(pool)
    new = await v2.cycle_trade_date_span(_ROOT, cycle=cycle)  # unbounded
    exact = await _v2_exact_objects(pool, _route_objects(cycle, require_filter=False))
    assert new == exact, f"v2 {cycle}: {new} != exact {exact}"


# --------------------------------------------------------------------------- #
# v1 no-cycle heuristic path must NOT regress (stays a fast, non-None answer)
# --------------------------------------------------------------------------- #
async def test_v1_nocycle_coverage_unchanged_and_fast(pool):
    v1 = SqlOptionsDataReader(pool)
    first, last = await v1.trade_date_coverage(_ROOT)
    assert first is not None and last is not None and first < last
