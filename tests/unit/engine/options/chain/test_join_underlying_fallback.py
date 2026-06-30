"""FAILING test — option-on-future underlying resolution when underlying_ref is None.

The dwh SQL reader does NOT populate ``OptionContractDoc.underlying_ref`` (it is
hardcoded ``None`` — ``tcg/data/_sql/options.py`` ``_meta_to_contract`` /
``_chain_meta_to_contract``; the Mongo per-contract FUT ``_id`` was not carried
through the #57 cutover).  So for EVERY option-on-future root (OPT_SP_500,
OPT_NASDAQ_100, …) the resolver's Branch-3 hits ``underlying_ref is None`` and
returns ``None`` (``_join.py:105-107``).  ByMoneyness / ByDelta then surface
``missing_underlying_price`` on every date → an all-NaN series (reproduced live
on OPT_SP_500).

The underlying IS available in the dwh, derivable WITHOUT ``underlying_ref``:
the matching ``FUT_SP_500`` contract for the option's ``expiration`` exists with
EOD prices (probed live: ``find_contract_by_expiration('FUT_SP_500', 20240621)``
→ ``FUT_SP_500_EMINI_20240621``; that future is the Black-76 forward — the
correct underlying for an option-on-future).  This is exactly the VIX Branch-2
mechanism (``get_futures_close_by_expiration``), which already works.

PROPER FIX: when an option-on-future contract has ``underlying_ref is None``,
fall back to resolving the underlying future BY EXPIRATION
(``futures_port.get_futures_close_by_expiration(FUT_collection, expiration,
target_date)``) instead of returning ``None``.

This test pins that behaviour and is RED on current code (the resolver returns
None instead of the futures close).  NOTE: the existing
``test_join.py::TestFuturesBranch.test_futures_branch_with_missing_underlying_ref_returns_none``
asserts the OPPOSITE (return None) — that test encodes the current gap as
intended behaviour and must be updated by the fix wave (the futures-by-expiration
fallback is the correct join, not a guess).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from tcg.engine.options.chain._join import resolve_underlying_price
from tcg.types.options import OptionContractDoc, OptionDailyRow


def _contract(
    *,
    collection: str,
    root_underlying: str,
    expiration: date = date(2024, 6, 21),
) -> OptionContractDoc:
    """Option-on-future contract shaped like what the SQL reader actually
    produces: ``underlying_ref=None`` (the cutover gap), ``root_underlying`` set,
    and an ``expiration`` (the key to the front-quarterly future)."""
    return OptionContractDoc(
        collection=collection,
        contract_id=f"{collection}|M",
        root_underlying=root_underlying,
        underlying_ref=None,  # <- the dwh reality
        underlying_symbol="ES",
        expiration=expiration,
        expiration_cycle="M",
        strike=5000.0,
        type="C",
        contract_size=None,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _row(target_date: date) -> OptionDailyRow:
    return OptionDailyRow(
        date=target_date,
        open=None,
        high=None,
        low=None,
        close=None,
        bid=1.0,
        ask=1.5,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=None,
        mid=1.25,
        iv_stored=None,
        delta_stored=None,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=None,
    )


@pytest.mark.asyncio
async def test_opt_sp500_quarterly_resolves_via_front_quarterly_future() -> None:
    """OPT_SP_500 with underlying_ref=None resolves via the FRONT-QUARTERLY future
    (nearest FUT_SP_500 with expiration >= the option's).  For a quarterly-month
    option the front-quarterly IS its own expiration's future."""
    contract = _contract(
        collection="OPT_SP_500",
        root_underlying="IND_SP_500",
        expiration=date(2024, 9, 20),  # a quarterly expiration
    )
    row = _row(date(2024, 6, 21))
    index_port = AsyncMock()
    futures_port = AsyncMock()
    futures_port.get_futures_close_on_or_after_expiration.return_value = 5504.0
    futures_port.get_futures_close_on_date.return_value = None  # legacy path unused

    result = await resolve_underlying_price(
        contract=contract,
        row=row,
        target_date=date(2024, 6, 21),
        index_port=index_port,
        futures_port=futures_port,
    )

    assert result == 5504.0
    futures_port.get_futures_close_on_or_after_expiration.assert_awaited_once_with(
        "FUT_SP_500", date(2024, 9, 20), date(2024, 6, 21)
    )


@pytest.mark.asyncio
async def test_opt_sp500_serial_month_resolves_via_front_quarterly_future() -> None:
    """THE GAP fix-D missed: a SERIAL-month SP500 option (e.g. July) has NO July
    future — SP500 futures are quarterly (Mar/Jun/Sep/Dec).  Per CME it references
    the FRONT-QUARTERLY future (September).  The resolver must call
    ``get_futures_close_on_or_after_expiration`` (>= the option's expiration), which
    the adapter maps to the Sep future's close — NOT exact-match (which would miss).
    """
    contract = _contract(
        collection="OPT_SP_500",
        root_underlying="IND_SP_500",
        expiration=date(2024, 7, 19),  # serial month — no matching future
    )
    row = _row(date(2024, 6, 21))
    index_port = AsyncMock()
    futures_port = AsyncMock()
    # The adapter resolves the front-quarterly (Sep) future and returns its close.
    futures_port.get_futures_close_on_or_after_expiration.return_value = 5534.25

    result = await resolve_underlying_price(
        contract=contract,
        row=row,
        target_date=date(2024, 6, 21),
        index_port=index_port,
        futures_port=futures_port,
    )

    assert result == 5534.25, (
        "serial-month option must resolve via the front-quarterly future; got "
        f"{result!r}"
    )
    futures_port.get_futures_close_on_or_after_expiration.assert_awaited_once_with(
        "FUT_SP_500", date(2024, 7, 19), date(2024, 6, 21)
    )


@pytest.mark.asyncio
async def test_fallback_returns_none_when_no_future_on_or_after() -> None:
    """Residual edge: an option expiring AFTER the last listed future → no future
    with expiration >= the option's → graceful None (→ missing_underlying_price on
    those dates), via the on-or-after path, not an early bail-out."""
    contract = _contract(collection="OPT_SP_500", root_underlying="IND_SP_500")
    row = _row(date(2024, 6, 21))
    index_port = AsyncMock()
    futures_port = AsyncMock()
    futures_port.get_futures_close_on_or_after_expiration.return_value = None

    result = await resolve_underlying_price(
        contract=contract,
        row=row,
        target_date=date(2024, 6, 21),
        index_port=index_port,
        futures_port=futures_port,
    )

    assert result is None
    futures_port.get_futures_close_on_or_after_expiration.assert_awaited_once()


@pytest.mark.asyncio
async def test_weekly_option_resolves_via_front_quarterly_future() -> None:
    """WEEKLY now resolves too: a SP500 weekly (e.g. expiring 2024-06-07) has no
    matching listed future, but the FRONT-QUARTERLY future (the Jun future,
    expiration 2024-06-21 >= 2024-06-07) IS its forward.  The on-or-after lookup
    returns that future's close — so weeklies are NO LONGER all-NaN (the fix-D
    exact-match gap).  The only residual None is an option past the last listed
    future (covered above)."""
    contract = OptionContractDoc(
        collection="OPT_SP_500",
        contract_id="OPT_SP_500|W",
        root_underlying="IND_SP_500",
        underlying_ref=None,
        underlying_symbol="EW1",
        expiration=date(2024, 6, 7),  # a Friday weekly
        expiration_cycle="W",
        strike=5000.0,
        type="C",
        contract_size=None,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )
    row = _row(date(2024, 6, 7))
    index_port = AsyncMock()
    futures_port = AsyncMock()
    # Adapter resolves the front-quarterly (Jun) future for the weekly's expiration.
    futures_port.get_futures_close_on_or_after_expiration.return_value = 5350.0

    result = await resolve_underlying_price(
        contract=contract,
        row=row,
        target_date=date(2024, 6, 7),
        index_port=index_port,
        futures_port=futures_port,
    )
    assert result == 5350.0
    futures_port.get_futures_close_on_or_after_expiration.assert_awaited_once_with(
        "FUT_SP_500", date(2024, 6, 7), date(2024, 6, 7)
    )


@pytest.mark.asyncio
async def test_opt_eth_crypto_does_not_use_futures_fallback() -> None:
    """Crypto roots are spot/perp-settled, NOT options-on-futures: a coincidental
    FUT_ETH is the WRONG underlying, so the futures fallback must be SKIPPED for
    OPT_ETH (it returns None, as before — the pricer blocks it as
    ``missing_deribit_feed``). Guards against the fix over-reaching to crypto."""
    contract = _contract(collection="OPT_ETH", root_underlying="ETH")
    row = _row(date(2024, 6, 21))
    index_port = AsyncMock()
    futures_port = AsyncMock()
    # Even if a FUT_ETH close were available, it must NOT be consulted.
    futures_port.get_futures_close_on_or_after_expiration.return_value = 3500.0

    result = await resolve_underlying_price(
        contract=contract,
        row=row,
        target_date=date(2024, 6, 21),
        index_port=index_port,
        futures_port=futures_port,
    )
    assert result is None
    futures_port.get_futures_close_on_or_after_expiration.assert_not_awaited()
    futures_port.get_futures_close_on_date.assert_not_awaited()
