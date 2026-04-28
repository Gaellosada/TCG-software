"""Unit tests for ``tcg.engine.options.chain._join`` — underlying-price resolver.

Module 6 owns the canonical underlying-price resolver (per Decision H).
Three branches:

1. **OPT_BTC** (Decision H — field-level join). The underlying price is
   inside the INTERNAL provider's eodGreeks row itself — Module 1
   surfaces it on ``OptionDailyRow.underlying_price_stored``.  No data
   port call.
2. **OPT_VIX** (root_underlying == "IND_VIX"). Look up the INDEX
   collection ``IND_VIX`` doc and find the row matching ``target_date``.
3. **All other roots** (option-on-future). Look up the FUT_* document
   per ``contract.underlying_ref``, find the row matching
   ``target_date``, return the close.

Returns ``None`` when the join fails (no row, no doc) — caller surfaces
``K_over_S = None``.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from tcg.engine.options.chain._join import resolve_underlying_price
from tcg.types.options import OptionContractDoc, OptionDailyRow


def _make_contract(
    *,
    collection: str,
    root_underlying: str,
    underlying_ref: str | None,
) -> OptionContractDoc:
    return OptionContractDoc(
        collection=collection,
        contract_id="dummy|M",
        root_underlying=root_underlying,
        underlying_ref=underlying_ref,
        underlying_symbol=None,
        expiration=date(2024, 6, 21),
        expiration_cycle="M",
        strike=100.0,
        type="C",
        contract_size=None,
        currency=None,
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _make_row(
    *,
    target_date: date,
    underlying_price_stored: float | None = None,
) -> OptionDailyRow:
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
        underlying_price_stored=underlying_price_stored,
    )


class TestOptBTCBranch:
    """Decision H — OPT_BTC reads underlying price directly off the row."""

    @pytest.mark.asyncio
    async def test_opt_btc_returns_row_underlying_price_stored(self) -> None:
        contract = _make_contract(
            collection="OPT_BTC",
            root_underlying="BTC",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21), underlying_price_stored=7484.58)
        index_port = AsyncMock()
        futures_port = AsyncMock()

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result == 7484.58
        # No data-port call was made.
        index_port.get_index_value_on_date.assert_not_awaited()
        futures_port.get_futures_close_on_date.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opt_btc_with_missing_row_underlying_price_returns_none(self) -> None:
        contract = _make_contract(
            collection="OPT_BTC",
            root_underlying="BTC",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21), underlying_price_stored=None)
        index_port = AsyncMock()
        futures_port = AsyncMock()

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result is None


class TestOptVIXBranch:
    """OPT_VIX (root_underlying == "IND_VIX") joins to the INDEX collection."""

    @pytest.mark.asyncio
    async def test_opt_vix_returns_index_value(self) -> None:
        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        index_port.get_index_value_on_date.return_value = 18.0
        futures_port = AsyncMock()

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result == 18.0
        index_port.get_index_value_on_date.assert_awaited_once_with(
            "IND_VIX", date(2024, 6, 21)
        )
        futures_port.get_futures_close_on_date.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opt_vix_index_miss_returns_none(self) -> None:
        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        index_port.get_index_value_on_date.return_value = None
        futures_port = AsyncMock()

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result is None


class TestFuturesBranch:
    """All other roots — option-on-future. Use ``contract.underlying_ref``."""

    @pytest.mark.asyncio
    async def test_opt_sp_500_returns_futures_close(self) -> None:
        contract = _make_contract(
            collection="OPT_SP_500",
            root_underlying="IND_SP_500",
            underlying_ref="FUT_SP_500_EMINI_20240621",
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()
        futures_port.get_futures_close_on_date.return_value = 5500.0

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result == 5500.0
        futures_port.get_futures_close_on_date.assert_awaited_once_with(
            "FUT_SP_500", "FUT_SP_500_EMINI_20240621", date(2024, 6, 21)
        )
        index_port.get_index_value_on_date.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_futures_miss_returns_none(self) -> None:
        contract = _make_contract(
            collection="OPT_GOLD",
            root_underlying="GOLD",
            underlying_ref="FUT_GOLD_20240828",
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()
        futures_port.get_futures_close_on_date.return_value = None

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_futures_branch_with_missing_underlying_ref_returns_none(self) -> None:
        # Defensive: option-on-future contract lacking the underlying_ref
        # cannot be joined; do not attempt a guess.
        contract = _make_contract(
            collection="OPT_NASDAQ_100",
            root_underlying="IND_NASDAQ_100",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result is None
        futures_port.get_futures_close_on_date.assert_not_awaited()


class TestOptETHBranch:
    """OPT_ETH has no confirmed underlying source (DB §2). Returns None."""

    @pytest.mark.asyncio
    async def test_opt_eth_returns_none(self) -> None:
        contract = _make_contract(
            collection="OPT_ETH",
            root_underlying="ETH",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result is None
