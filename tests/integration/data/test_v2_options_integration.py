"""Live-dwh integration tests for :class:`V2OptionsDataReader`.

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by
the ``DWH_*`` variables being present. Reads the four live EW option objects
(11/12/7/13) READ-ONLY via ``tcg_read``.

The window is fixed at 2026-06-01..2026-06-11 on expiration 2026-06-05 because
that is the exact window mapping spec §5.2 measured, where the 10Δ-put winner
was identical on v1 and v2 for 13/13 paired (expiration, trade-date) groups.
Reproducing that pick THROUGH the reader — not through hand-written SQL — is
what makes the delta path trustworthy.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._v2_compat.errors import V2UnsupportedCycle
from tcg.data._v2_compat.options_reader import V2OptionsDataReader
from tcg.engine.options.selection._match import match_by_delta

_EXPIRATION = date(2026, 6, 5)
_CYCLE = "W1 Friday"


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
    yield V2OptionsDataReader(pool)
    await pool.close()


@pytest.mark.integration
async def test_live_chain_has_rows_and_greeks(reader):
    """A real chain for one expiration: row count, greeks presence, and the
    quote fields v2 genuinely does not have."""
    pairs = await reader.query_chain(
        "OPT_SP_500",
        date(2026, 6, 1),
        "P",
        _EXPIRATION,
        _EXPIRATION,
        expiration_cycle=_CYCLE,
    )
    assert len(pairs) > 300, f"expected a full put chain, got {len(pairs)}"
    with_delta = [r for _c, r in pairs if r.delta_stored is not None]
    assert len(with_delta) > 0.8 * len(pairs), "fact_greeks coverage collapsed"

    for contract, row in pairs:
        assert contract.collection == "OPT_SP_500"
        assert contract.expiration == _EXPIRATION
        assert contract.expiration_cycle == _CYCLE
        assert contract.root_underlying == "IND_SP_500"
        assert contract.contract_size == 50.0
        # v2 has no order book for options at ALL — never a fabricated mid.
        assert row.bid is None and row.ask is None and row.mid is None
        assert row.volume is None and row.open_interest is None

    # Settlements are clean on v2 (spec D5): no false zeros, unlike v1.
    closes = [r.close for _c, r in pairs if r.close is not None]
    assert closes and min(closes) > 0


@pytest.mark.integration
async def test_live_delta_selection_reproduces_the_spec_pick(reader):
    """Spec §5.2 measured the 10Δ-put winner on (2026-06-05, 2026-06-01) as
    strike 7455 with delta ≈ -0.1021 on BOTH sources. Reproduce it through the
    reader's pushdown, and prove the pushdown agrees with the full chain."""
    groups = [(_EXPIRATION, [date(2026, 6, 1), date(2026, 6, 4)])]

    kept = await reader.query_chain_bulk_multi(
        "OPT_SP_500", "P", groups, expiration_cycle=_CYCLE, delta_pushdown=(-0.10, 4)
    )
    full = await reader.query_chain_bulk_multi(
        "OPT_SP_500", "P", groups, expiration_cycle=_CYCLE
    )

    def _pick(pairs):
        return match_by_delta(
            pairs,
            [r.delta_stored for _c, r in pairs],
            -0.10,
            1.0,
            False,
            chain_size=len(pairs),
        )

    winner = _pick(kept[date(2026, 6, 1)])
    assert winner.contract.contract_id == "OPT_FUT_SP_500_EMINI_20260605_7455_P"
    assert winner.matched_value == pytest.approx(-0.1021, abs=5e-4)

    for d in (date(2026, 6, 1), date(2026, 6, 4)):
        assert len(kept[d]) <= 4
        assert (
            _pick(kept[d]).contract.contract_id == _pick(full[d]).contract.contract_id
        ), f"pushdown diverged from the full chain on {d}"


@pytest.mark.integration
async def test_live_greeks_pair_with_the_right_price(reader):
    """Guardrail Sign 7 end-to-end.

    A contract's ``value`` serie and its ``greeks`` serie are DIFFERENT
    ``serie_id``s, tied together only by ``serie.contract_id``. A wrong pivot
    would scramble greeks against prices while leaving each side internally
    consistent — invisible in the result shape, so it has to be caught by a
    cross-table invariant. Two are asserted here, one per fact table:

    * ``fact_value``: a put's settlement is non-decreasing in strike;
    * ``fact_greeks``: a put's delta is decreasing in strike.

    The delta check is restricted to ``0.001 <= |delta| <= 0.99``. Outside that
    band the invariant genuinely does not hold: Black-76 put delta is
    ``-e^{-rT} N(-d1)``, so deep in the money it asymptotes UP to ``-e^{-rT}``
    (measured live: ≈ -0.99597, rising ~1e-6 per strike step). Those are correct
    model values, not mis-pairings — asserting monotonicity there would be
    asserting something false.
    """
    pairs = await reader.query_chain(
        "OPT_SP_500",
        date(2026, 6, 1),
        "P",
        _EXPIRATION,
        _EXPIRATION,
        expiration_cycle=_CYCLE,
    )

    prices = sorted((c.strike, r.close) for c, r in pairs if r.close is not None)
    price_inversions = sum(1 for a, b in zip(prices, prices[1:]) if b[1] < a[1] - 1e-9)
    assert price_inversions == 0, (
        f"{price_inversions} settlement inversions across strikes — the "
        "fact_value join is wrong"
    )

    ranked = sorted(
        (c.strike, r.delta_stored)
        for c, r in pairs
        if r.delta_stored is not None and 0.001 <= abs(r.delta_stored) <= 0.99
    )
    assert len(ranked) > 100, "band too small to be a meaningful check"
    delta_inversions = sum(1 for a, b in zip(ranked, ranked[1:]) if b[1] > a[1] + 1e-9)
    assert delta_inversions == 0, (
        f"{delta_inversions}/{len(ranked)} delta inversions across strikes — "
        "greeks are paired with the wrong contract (serie_id fan-out)"
    )


@pytest.mark.integration
async def test_live_held_rows_returns_the_selected_contract_only(reader):
    out = await reader.query_held_rows(
        "OPT_SP_500",
        "P",
        [
            (
                "OPT_FUT_SP_500_EMINI_20260605_7455_P",
                date(2026, 6, 1),
                _EXPIRATION,
            )
        ],
        expiration_cycle=_CYCLE,
    )
    assert out, "the held contract must have settlement rows over its window"
    for d, pairs in out.items():
        assert len(pairs) == 1, "the natural key is unique on v2 — no duplicates"
        assert pairs[0][0].contract_id == "OPT_FUT_SP_500_EMINI_20260605_7455_P"
        assert date(2026, 6, 1) <= d <= _EXPIRATION


@pytest.mark.integration
async def test_live_monthly_cycle_is_refused_before_any_query(reader):
    with pytest.raises(V2UnsupportedCycle):
        await reader.query_chain(
            "OPT_SP_500",
            date(2026, 6, 1),
            "P",
            _EXPIRATION,
            _EXPIRATION,
            expiration_cycle=("M", "W3 Friday"),
        )


@pytest.mark.integration
async def test_live_root_metadata(reader):
    roots = await reader.list_roots()
    assert len(roots) == 1, "the four EW objects are ONE logical collection"
    info = roots[0]
    assert info.collection == "OPT_SP_500"
    assert info.providers == ("DATABENTO",)
    assert info.has_greeks
    assert info.expiration_first is not None and info.expiration_first.year <= 2011
    assert info.doc_count_estimated > 300_000
    first, last = await reader.trade_date_coverage("OPT_SP_500")
    assert first is not None and last is not None and first < last
