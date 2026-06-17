"""Live-dwh integration tests for ``SqlOptionsDataReader.query_chain_bulk``.

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by
the ``DWH_*`` connection variables being present (``load_dwh_config`` raises
otherwise -> skip).  The dwh is directly reachable from the dev WSL host (no
SSM tunnel); creds live in ``TCG-software/.env``.

These prove the bulk chain query is a faithful drop-in for ``query_chain``:

  * bulk(single date) == query_chain(that date) -- same contracts, same marks;
  * one bulk call returns every requested date keyed (``[]`` when empty);
  * a ByDelta-style scan over the bulk chain selects a ~10-delta contract when
    the target is the fractional 0.10 (NOT deep-ITM), confirming the stored
    delta scale is [-1, 1] and the engine's ``target_delta`` is a fraction.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.options import SqlOptionsDataReader

# BTC has stored greeks for 2018-01-01..2023-02-13 (verified); use a 2022
# window so ByDelta has real deltas to match.  Same root as the bug repro.
_ROOT = "OPT_BTC"
_EXP = date(2022, 6, 24)
_DATES = [date(2022, 6, 1), date(2022, 6, 2), date(2022, 6, 3)]


@pytest.fixture
async def reader():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield SqlOptionsDataReader(pool)
    await pool.close()


@pytest.mark.integration
async def test_bulk_keys_every_requested_date(reader):
    result = await reader.query_chain_bulk(
        root=_ROOT,
        dates=_DATES,
        type="C",
        expiration_min=_EXP,
        expiration_max=_EXP,
    )
    assert set(result.keys()) == set(_DATES)
    # At least one date in this liquid window must carry contracts.
    assert any(len(v) > 0 for v in result.values())


@pytest.mark.integration
async def test_bulk_single_date_matches_query_chain(reader):
    """bulk({d})[d] must equal query_chain(d) row-for-row (drop-in parity)."""
    d = _DATES[0]
    single = await reader.query_chain(
        root=_ROOT,
        date=d,
        type="C",
        expiration_min=_EXP,
        expiration_max=_EXP,
    )
    bulk = await reader.query_chain_bulk(
        root=_ROOT,
        dates=[d],
        type="C",
        expiration_min=_EXP,
        expiration_max=_EXP,
    )
    bulk_rows = bulk[d]

    # Same set of contracts.
    single_ids = sorted(c.contract_id for c, _r in single)
    bulk_ids = sorted(c.contract_id for c, _r in bulk_rows)
    assert single_ids == bulk_ids, "bulk and single-date chains differ in contracts"

    # Same marks per contract (mid / delta), keyed by contract_id.
    single_by_id = {c.contract_id: r for c, r in single}
    for c, r in bulk_rows:
        sr = single_by_id[c.contract_id]
        assert r.mid == sr.mid, f"mid mismatch for {c.contract_id}"
        assert r.delta_stored == sr.delta_stored, f"delta mismatch for {c.contract_id}"
        assert r.date == d


@pytest.mark.integration
async def test_bulk_chain_ten_delta_selection_is_otm_not_itm(reader):
    """A ~10-delta target (0.10) selects an OTM call, NOT deep-ITM (~1.0).

    This is the delta-convention proof: stored delta is a [-1,1] fraction, so
    matching nearest-to-0.10 lands on a low-delta OTM call (strike > spot),
    whereas matching nearest-to-10.0 (the raw, unscaled value) would pick the
    deepest-ITM call (delta ~ 1.0, strike << spot).
    """
    d = _DATES[0]
    bulk = await reader.query_chain_bulk(
        root=_ROOT,
        dates=[d],
        type="C",
        expiration_min=_EXP,
        expiration_max=_EXP,
    )
    rows = [(c, r) for c, r in bulk[d] if r.delta_stored is not None]
    assert rows, "no rows with stored delta in the bulk chain"

    spot = next(
        (r.underlying_price_stored for _c, r in rows if r.underlying_price_stored),
        None,
    )
    assert spot is not None and spot > 0

    # Nearest to the FRACTIONAL 0.10 target.
    c010, r010 = min(rows, key=lambda cr: abs((cr[1].delta_stored or 0) - 0.10))
    # Nearest to the RAW 10.0 (what the un-reconciled UI value would do).
    c10, r10 = min(rows, key=lambda cr: abs((cr[1].delta_stored or 0) - 10.0))

    # The 0.10 pick is a genuine low-delta OTM call.
    assert 0.0 < (r010.delta_stored or 0) < 0.25, (
        f"target 0.10 should pick a ~10-delta call, got delta={r010.delta_stored}"
    )
    assert c010.strike > spot, "a 10-delta call must be OTM (strike > spot)"

    # The raw-10.0 pick is the deep-ITM call (the bug we are guarding against).
    assert (r10.delta_stored or 0) > 0.9, "raw 10.0 collapses onto deepest-ITM"
    assert c10.strike < spot, "the deep-ITM pick is below spot"

    # And they are different contracts (the scale mismatch matters).
    assert c010.contract_id != c10.contract_id
