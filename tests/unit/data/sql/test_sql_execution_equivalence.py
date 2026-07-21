"""Real-SQL equivalence guard (closes D3 INV-1 / D4 §2 Layer-B gap).

The sibling ``test_sql_options_*`` tests bind a *Python* reference of the
delta-rank to ``match_by_delta`` and only string-assert the actual SQL.  Real
SQL <-> Python drift is therefore caught ONLY by the out-of-tree live-dwh npz
harness, NOT unit CI: reverting the R3 ``expiration_cycle`` predicate in
``query_held_rows`` leaves the fake-cursor tests green.

These tests execute the *real* ``query_chain_bulk_multi`` (full-chain AND
delta-pushdown) and ``query_held_rows`` against a seeded ephemeral PostgreSQL
(see ``conftest.py``), feed the rows through the UNCHANGED
``match_by_delta`` / ``_row_for_contract``, and diff the resolved contract +
price against a full-chain reference over the same data.  A regression in the
real SQL now goes RED in unit CI.

If no PostgreSQL server binary is discoverable the module auto-skips (documented
residual gap — pure unit CI without a server or Docker cannot run it).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from tcg.data._sql.connection import DwhConnectionPool
from tcg.data._sql.options import SqlOptionsDataReader
from tcg.engine.options.selection._match import match_by_delta
from tcg.engine.options.series.stream_resolver import _row_for_contract

TARGET = -0.10  # 10-delta put; deltas are negative fractions
K = 2  # top-k SYMBOLS retained by the pushdown
_BIG_TOL = 10.0  # non-strict: always return the closest


@asynccontextmanager
async def _reader(params):
    """A connected read-only pool + reader against the ephemeral server."""
    pool = DwhConnectionPool(
        host=params.host,
        port=params.port,
        db=params.db,
        user=params.user,
        password=params.password,
        min_size=1,
        max_size=2,
        sslmode="disable",
    )
    await pool.connect()
    try:
        yield SqlOptionsDataReader(pool)
    finally:
        await pool.close()


def _pick(rows):
    """Winner contract via the UNCHANGED production selection function."""
    deltas = [r.delta_stored for _c, r in rows]
    return match_by_delta(
        rows,
        deltas,
        target=TARGET,
        tolerance=_BIG_TOL,
        strict=False,
        chain_size=len(rows),
    )


@pytest.mark.asyncio
async def test_delta_pushdown_matches_full_chain(seeded_dwh):
    """The delta pushdown's resolved pick == the full-chain resolved pick.

    Runs the REAL SQL both ways over the SAME seed and asserts, per trade date:
    same winning contract, the winner's whole duplicate-instrument_id set is
    retained (superset), and ``_row_for_contract`` surfaces the identical
    physical row (mid + close).
    """
    root = seeded_dwh.root
    groups = [
        (seeded_dwh.e1, list(seeded_dwh.e1_dates)),
        (seeded_dwh.e2, list(seeded_dwh.e2_dates)),
    ]
    async with _reader(seeded_dwh) as reader:
        full = await reader.query_chain_bulk_multi(root, "P", groups)
        push = await reader.query_chain_bulk_multi(
            root, "P", groups, delta_pushdown=(TARGET, K)
        )

    all_dates = list(seeded_dwh.e1_dates) + list(seeded_dwh.e2_dates)
    assert set(full) >= set(all_dates)

    for d in all_dates:
        full_rows = full[d]
        push_rows = push[d]
        assert full_rows, f"seed produced no full-chain rows for {d}"

        ref = _pick(full_rows)
        got = _pick(push_rows)
        assert ref.contract is not None and got.contract is not None

        # 1) same winning CONTRACT
        assert got.contract.contract_id == ref.contract.contract_id, (
            f"{d}: pushdown winner {got.contract.contract_id} != "
            f"full-chain winner {ref.contract.contract_id}"
        )

        # 2) winner's full duplicate-instrument_id set is RETAINED (superset)
        win_id = ref.contract.contract_id
        full_mids = {
            r.mid for c, r in full_rows if c.contract_id == win_id and r.mid is not None
        }
        push_mids = {
            r.mid for c, r in push_rows if c.contract_id == win_id and r.mid is not None
        }
        assert full_mids <= push_mids, (
            f"{d}: pushdown dropped a row of the winning symbol {win_id}"
        )

        # 3) identical resolved physical row (first-by-instrument_id)
        ref_row = _row_for_contract(full_rows, ref.contract)
        got_row = _row_for_contract(push_rows, got.contract)
        assert ref_row is not None and got_row is not None
        assert got_row.mid == ref_row.mid
        assert got_row.close == ref_row.close


@pytest.mark.asyncio
async def test_held_rows_cycle_predicate_matches_full_chain(seeded_dwh):
    """``query_held_rows`` returns the SAME frozen row as the full chain.

    The seed's held symbol ``OPT_TEST_4970P`` carries TWO instrument_ids under
    different cycles ('M' mid 7.40, 'W3 Friday' mid 4.15).  A weekly leg fetched
    with ``expiration_cycle='W3 Friday'`` must resolve to the W3-Friday row
    (4.15) — the same one the full-chain path yields — NOT the lower-iid 'M'
    sibling.  This is the flagship regression the R3 cycle predicate fixed.
    """
    root = seeded_dwh.root
    held_dates = seeded_dwh.held_dates
    sym = "OPT_TEST_4970P"
    cycle = "W3 Friday"
    lo, hi = held_dates[0], held_dates[-1]
    seam = held_dates[-1]  # the roll/read date

    async with _reader(seeded_dwh) as reader:
        held = await reader.query_held_rows(
            root, "P", [(sym, lo, hi)], expiration_cycle=cycle
        )
        # Full-chain reference over the SAME data + SAME cycle filter.
        ref_chain = await reader.query_chain_bulk_multi(
            root, "P", [(seeded_dwh.e3, list(held_dates))], expiration_cycle=cycle
        )

    held_rows = held.get(seam, [])
    ref_rows = [cr for cr in ref_chain.get(seam, []) if cr[0].contract_id == sym]
    assert held_rows, "held-rows fetch returned nothing for the seam date"
    assert ref_rows, "full-chain reference returned nothing for the held symbol"

    # Same surviving instrument set (only the W3-Friday sibling).
    held_for_sym = [cr for cr in held_rows if cr[0].contract_id == sym]
    assert len(held_for_sym) == 1, (
        "cycle predicate must collapse the cross-cycle duplicate to ONE row; "
        f"got {len(held_for_sym)} (the 'M' sibling leaked in -> R3 regression)"
    )

    # Frozen contract == the one Phase-1 would have selected from the chain.
    frozen = ref_rows[0][0]
    held_row = _row_for_contract(held_rows, frozen)
    ref_row = _row_for_contract(ref_rows, frozen)
    assert held_row is not None and ref_row is not None
    assert held_row.mid == ref_row.mid == pytest.approx(4.15), (
        f"held mid {held_row.mid} != full-chain W3-Friday mid {ref_row.mid} "
        "(the 'M' sibling's 7.40 leaked in)"
    )
