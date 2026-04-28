"""DefaultOptionsSelector orchestration tests.

Mocks
-----
- ``ChainReaderPort`` via ``unittest.mock.AsyncMock``.
- ``MaturityResolver`` via ``unittest.mock.MagicMock`` (returns a fixed
  expiration / nearest-from-chain).
- ``OptionsPricer`` via ``unittest.mock.MagicMock`` for opt-in tests.
- ``UnderlyingPriceResolver`` via async lambda.

No Mongo, no Module 1/2/4 internals.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from tcg.engine.options.selection.selector import DefaultOptionsSelector
from tcg.types.options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    ComputedGreeks,
    ComputeResult,
    FixedDate,
    GreekKind,
    NearestToTarget,
)
from ._fixtures import (
    DEFAULT_DATE,
    DEFAULT_EXPIRATION,
    make_chain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_maturity_resolver(expiration: date = DEFAULT_EXPIRATION) -> MagicMock:
    m = MagicMock()
    m.resolve.return_value = expiration
    m.resolve_with_chain.return_value = expiration
    return m


def _make_reader(chain: list) -> AsyncMock:
    reader = AsyncMock()
    reader.query_chain.return_value = chain
    return reader


def _missing_delta(error_code: str = "missing_forward_vix_curve") -> ComputedGreeks:
    miss = ComputeResult(
        value=None,
        source="missing",
        model=None,
        inputs_used=None,
        missing_inputs=("forward_vix_curve",),
        error_code=error_code,
        error_detail=None,
    )
    return ComputedGreeks(iv=miss, delta=miss, gamma=miss, theta=miss, vega=miss)


def _computed_delta(value: float) -> ComputedGreeks:
    """Build a ComputedGreeks where only delta is computed."""
    delta = ComputeResult(
        value=value,
        source="computed",
        model="Black-76",
        inputs_used={"r": 0.0, "iv": 0.2, "ttm": 0.25},
        missing_inputs=None,
        error_code=None,
        error_detail=None,
    )
    not_req = ComputeResult(
        value=None,
        source="missing",
        model=None,
        inputs_used=None,
        missing_inputs=(),
        error_code="not_requested",
        error_detail=None,
    )
    return ComputedGreeks(
        iv=not_req,
        delta=delta,
        gamma=not_req,
        theta=not_req,
        vega=not_req,
    )


# ---------------------------------------------------------------------------
# ByStrike
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_strike_exact_match() -> None:
    chain = make_chain([(95, None), (100, None), (105, None), (110, None), (115, None)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByStrike(strike=105.0),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.strike == 105.0
    reader.query_chain.assert_awaited_once()
    # Module 4 invoked once with FixedDate.
    resolver.resolve.assert_called_once()


@pytest.mark.asyncio
async def test_by_strike_no_match_in_chain() -> None:
    chain = make_chain([(95, None), (100, None), (105, None)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByStrike(strike=999.0),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.contract is None
    assert out.error_code == "strike_not_in_chain"


# ---------------------------------------------------------------------------
# ByDelta — stored-only path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_delta_stored_closest_wins() -> None:
    chain = make_chain([(95, 0.70), (100, 0.50), (105, 0.30), (110, 0.10)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByDelta(target_delta=0.30, tolerance=0.05),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
        compute_missing_for_delta=False,
    )
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.strike == 105


@pytest.mark.asyncio
async def test_by_delta_all_none_no_opt_in_returns_missing_no_compute() -> None:
    chain = make_chain([(95, None), (100, None), (105, None)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByDelta(target_delta=0.30),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
        compute_missing_for_delta=False,
    )
    assert out.contract is None
    assert out.error_code == "missing_delta_no_compute"
    assert out.diagnostic is not None
    # Diagnostic mentions chain size and how many had None delta.
    assert "3" in out.diagnostic


@pytest.mark.asyncio
async def test_by_delta_strict_tolerance_miss() -> None:
    chain = make_chain([(95, 0.70), (100, 0.50)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByDelta(target_delta=0.10, tolerance=0.05, strict=True),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.contract is None
    assert out.error_code == "no_match_within_tolerance"


@pytest.mark.asyncio
async def test_by_delta_strict_false_tolerance_miss_returns_closest() -> None:
    chain = make_chain([(95, 0.70), (100, 0.50)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByDelta(target_delta=0.10, tolerance=0.05, strict=False),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.strike == 100
    assert out.matched_value == 0.50


# ---------------------------------------------------------------------------
# ByDelta — compute opt-in path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_delta_opt_in_pricer_fills_missing() -> None:
    """compute_missing_for_delta=True: pricer fills missing-delta rows only."""
    chain = make_chain([(95, 0.70), (100, None), (105, None), (110, 0.10)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    pricer = MagicMock()
    # K=100 → delta 0.50; K=105 → delta 0.30
    delta_by_strike = {100.0: 0.50, 105.0: 0.30}

    def _compute(contract, row, underlying_price, which):  # noqa: ARG001
        return _computed_delta(delta_by_strike[contract.strike])

    pricer.compute.side_effect = _compute

    async def _underlying(_contract, _on_date):
        return 100.0

    selector = DefaultOptionsSelector(
        reader=reader,
        maturity_resolver=resolver,
        pricer=pricer,
        underlying_price_resolver=_underlying,
    )
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByDelta(target_delta=0.30, tolerance=0.05),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
        compute_missing_for_delta=True,
    )
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.strike == 105
    # Pricer called only for the 2 missing rows (K=100, K=105).
    assert pricer.compute.call_count == 2
    called_strikes = {
        call.kwargs.get("contract", call.args[0] if call.args else None).strike
        for call in pricer.compute.call_args_list
    }
    assert called_strikes == {100.0, 105.0}
    # Pricer must have been asked specifically for DELTA only.
    for call in pricer.compute.call_args_list:
        which = call.kwargs.get("which")
        assert which == (GreekKind.DELTA,)


@pytest.mark.asyncio
async def test_by_delta_opt_in_vix_root_pricer_returns_missing() -> None:
    """OPT_VIX: pricer returns source="missing" → reflected as missing_delta_no_compute."""
    chain = make_chain(
        [(15, None), (16, None), (17, None)],
        collection="OPT_VIX",
    )
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    pricer = MagicMock()
    pricer.compute.return_value = _missing_delta(error_code="missing_forward_vix_curve")

    async def _underlying(_c, _d):
        return 17.0  # synthetic VIX spot — won't be used productively

    selector = DefaultOptionsSelector(
        reader=reader,
        maturity_resolver=resolver,
        pricer=pricer,
        underlying_price_resolver=_underlying,
    )
    out = await selector.select(
        root="OPT_VIX",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByDelta(target_delta=0.30),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
        compute_missing_for_delta=True,
    )
    assert out.contract is None
    assert out.error_code == "missing_delta_no_compute"
    assert out.diagnostic is not None
    # Cascade: surface the underlying Module-2 reason in the diagnostic.
    assert "missing_forward_vix_curve" in out.diagnostic


@pytest.mark.asyncio
async def test_compute_missing_with_no_pricer_raises() -> None:
    chain = make_chain([(100, None)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(
        reader=reader,
        maturity_resolver=resolver,
        pricer=None,
    )
    with pytest.raises(NotImplementedError):
        await selector.select(
            root="OPT_SP_500",
            date=DEFAULT_DATE,
            type="C",
            criterion=ByDelta(target_delta=0.30),
            maturity=FixedDate(date=DEFAULT_EXPIRATION),
            compute_missing_for_delta=True,
        )


# ---------------------------------------------------------------------------
# ByMoneyness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_moneyness_with_underlying_join() -> None:
    chain = make_chain([(95, None), (100, None), (105, None), (110, None)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    async def _underlying(_c, _d):
        return 100.0

    selector = DefaultOptionsSelector(
        reader=reader,
        maturity_resolver=resolver,
        underlying_price_resolver=_underlying,
    )
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByMoneyness(target_K_over_S=1.05, tolerance=0.01),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.strike == 105
    assert out.matched_value == 1.05


@pytest.mark.asyncio
async def test_by_moneyness_no_resolver_returns_missing_underlying() -> None:
    chain = make_chain([(95, None), (100, None)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(
        reader=reader,
        maturity_resolver=resolver,
        underlying_price_resolver=None,
    )
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByMoneyness(target_K_over_S=1.0),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.contract is None
    assert out.error_code == "missing_underlying_price"


@pytest.mark.asyncio
async def test_by_moneyness_resolver_returns_none() -> None:
    chain = make_chain([(95, None), (100, None)])
    reader = _make_reader(chain)
    resolver = _make_maturity_resolver()

    async def _underlying(_c, _d):
        return None

    selector = DefaultOptionsSelector(
        reader=reader,
        maturity_resolver=resolver,
        underlying_price_resolver=_underlying,
    )
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByMoneyness(target_K_over_S=1.0),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.contract is None
    assert out.error_code == "missing_underlying_price"


# ---------------------------------------------------------------------------
# Empty chain / maturity edge-cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_chain_returns_no_chain_for_date() -> None:
    reader = _make_reader([])
    resolver = _make_maturity_resolver()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByStrike(strike=100.0),
        maturity=FixedDate(date=DEFAULT_EXPIRATION),
    )
    assert out.contract is None
    assert out.error_code == "no_chain_for_date"
    assert out.diagnostic is not None
    assert "OPT_SP_500" in out.diagnostic
    assert DEFAULT_DATE.isoformat() in out.diagnostic


@pytest.mark.asyncio
async def test_maturity_resolver_picks_no_chain_expiration() -> None:
    """Module 4 returns an expiration with no chain rows → no_chain_for_date."""
    # Reader returns nothing on the resolved-expiration query.
    reader = AsyncMock()
    reader.query_chain.return_value = []
    resolver = _make_maturity_resolver(expiration=date(2024, 7, 19))

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByStrike(strike=100.0),
        maturity=FixedDate(date=date(2024, 7, 19)),
    )
    assert out.contract is None
    assert out.error_code == "no_chain_for_date"


@pytest.mark.asyncio
async def test_nearest_to_target_uses_resolve_with_chain() -> None:
    """NearestToTarget probes the chain, then resolves nearest expiration."""
    # Probe returns 2 expirations; the resolver picks the nearer one.
    far = date(2024, 9, 20)
    near = date(2024, 6, 21)
    probe_chain = (
        make_chain([(100, None)], expiration=near)
        + make_chain([(100, None)], expiration=far)
    )
    final_chain = make_chain([(100, None)], expiration=near)

    reader = AsyncMock()
    # First call: probe (wide window).  Second call: actual chain on resolved expiration.
    reader.query_chain.side_effect = [probe_chain, final_chain]

    resolver = MagicMock()
    resolver.resolve_with_chain.return_value = near

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByStrike(strike=100.0),
        maturity=NearestToTarget(target_dte_days=90),
    )
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.expiration == near
    # resolve_with_chain was called with the deduplicated, sorted expirations.
    call_args = resolver.resolve_with_chain.call_args
    assert sorted(call_args.kwargs["available_expirations"]) == [near, far]
    # query_chain called exactly twice (probe + final).
    assert reader.query_chain.await_count == 2


@pytest.mark.asyncio
async def test_nearest_to_target_empty_probe_returns_no_chain() -> None:
    reader = AsyncMock()
    reader.query_chain.return_value = []  # both calls (probe) return nothing
    resolver = MagicMock()

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=resolver)
    out = await selector.select(
        root="OPT_SP_500",
        date=DEFAULT_DATE,
        type="C",
        criterion=ByStrike(strike=100.0),
        maturity=NearestToTarget(target_dte_days=30),
    )
    assert out.contract is None
    assert out.error_code == "no_chain_for_date"
    # resolve_with_chain should NOT have been called when probe is empty.
    resolver.resolve_with_chain.assert_not_called()
