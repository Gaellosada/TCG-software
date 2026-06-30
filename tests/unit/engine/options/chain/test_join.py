"""Unit tests for ``tcg.engine.options.chain._join`` — underlying-price resolver.

Module 6 owns the canonical underlying-price resolver (per Decision H).
Three branches:

1. **OPT_BTC** (Decision H — field-level join). The underlying price is
   inside the INTERNAL provider's eodGreeks row itself — Module 1
   surfaces it on ``OptionDailyRow.underlying_price_stored``.  No data
   port call.
2. **OPT_VIX** (root_underlying == "IND_VIX"). Look up the matching
   ``FUT_VIX`` contract by expiration and return its close on the
   trade date (the Black-76 forward). Returns ``None`` when no
   FUT_VIX exists for the option's expiration (weekly options).
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
    """OPT_VIX joins to the matching FUT_VIX contract by expiration.

    Phase 2 of the VIX greeks rollout — monthly VIX options get a forward
    from the FUT_VIX close (the Black-76 forward); weekly options (no
    matching FUT_VIX expiration) get ``None`` so the pricer surfaces
    ``missing_forward_vix_curve``.
    """

    @pytest.mark.asyncio
    async def test_opt_vix_monthly_returns_fut_vix_close(self) -> None:
        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()
        futures_port.get_futures_close_by_expiration.return_value = 18.0

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result == 18.0
        futures_port.get_futures_close_by_expiration.assert_awaited_once_with(
            "FUT_VIX", date(2024, 6, 21), date(2024, 6, 21)
        )
        # Index port is not used in Phase 2 — FUT_VIX is the forward.
        index_port.get_index_value_on_date.assert_not_awaited()
        futures_port.get_futures_close_on_date.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opt_vix_weekly_returns_none(self) -> None:
        """No matching FUT_VIX expiry (weekly) → adapter returns None →
        resolver propagates None so the pricer surfaces
        ``missing_forward_vix_curve``.
        """
        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()
        futures_port.get_futures_close_by_expiration.return_value = None

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result is None
        futures_port.get_futures_close_by_expiration.assert_awaited_once()


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
    async def test_futures_branch_missing_underlying_ref_falls_back_by_expiration(
        self,
    ) -> None:
        # NEW CONTRACT (PR #67 fix D): the dwh SQL reader never populates
        # ``underlying_ref``, so an option-on-future with ``underlying_ref=None``
        # must FALL BACK to the FUT_* contract matching the option's expiration
        # (the Black-76 forward — the same by-expiration resolution Branch 2 uses
        # for VIX), NOT return None.  (Previously this asserted ``return None``,
        # which made every SP500/NASDAQ by-moneyness/delta series all-NaN.)
        contract = _make_contract(
            collection="OPT_NASDAQ_100",
            root_underlying="IND_NASDAQ_100",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()
        futures_port.get_futures_close_by_expiration.return_value = 19850.0

        result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )

        assert result == 19850.0
        futures_port.get_futures_close_by_expiration.assert_awaited_once_with(
            "FUT_NASDAQ_100", date(2024, 6, 21), date(2024, 6, 21)
        )
        # The legacy per-contract-ref path is NOT used when underlying_ref is None.
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


class TestSharedVixForward:
    """Both call sites (chain _join + API _batch_underlying_prices) must
    use the same VIX forward dispatch (Wave 2 / triage #4). This test
    pins that they delegate to the same shared helper so the contract-
    detail endpoint and chain endpoint agree on the forward.
    """

    @pytest.mark.asyncio
    async def test_chain_join_delegates_to_resolve_vix_forward(self) -> None:
        from tcg.engine.options.chain._forward import resolve_vix_forward

        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        row = _make_row(target_date=date(2024, 6, 21))
        index_port = AsyncMock()
        futures_port = AsyncMock()
        futures_port.get_futures_close_by_expiration.return_value = 18.0

        # Call _join.resolve_underlying_price and the shared helper with
        # the same inputs; both must return the same forward.
        join_result = await resolve_underlying_price(
            contract=contract,
            row=row,
            target_date=date(2024, 6, 21),
            index_port=index_port,
            futures_port=futures_port,
        )
        helper_result = await resolve_vix_forward(
            contract, futures_port, date(2024, 6, 21)
        )

        assert join_result == helper_result == 18.0

    @pytest.mark.asyncio
    async def test_resolve_vix_forward_short_circuits_non_vix(self) -> None:
        """Non-VIX contract → None so the API/chain fall through to spot
        or per-contract paths.
        """
        from tcg.engine.options.chain._forward import resolve_vix_forward

        contract = _make_contract(
            collection="OPT_SP_500",
            root_underlying="IND_SP_500",
            underlying_ref="FUT_SP_500_EMINI_20240621",
        )
        futures_port = AsyncMock()

        result = await resolve_vix_forward(contract, futures_port, date(2024, 6, 21))

        assert result is None
        futures_port.get_futures_close_by_expiration.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_vix_futures_ref_returns_contract_id(self) -> None:
        """The API bulk path uses :func:`resolve_vix_futures_ref` to get
        the FUT_VIX contract id (then drives a date-range fetch).
        """
        from tcg.engine.options.chain._forward import resolve_vix_futures_ref

        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        svc = AsyncMock()
        svc.find_futures_contract_by_expiration.return_value = "FUT_VIX_20240621"

        result = await resolve_vix_futures_ref(contract, svc)

        assert result == "FUT_VIX_20240621"
        svc.find_futures_contract_by_expiration.assert_awaited_once_with(
            "FUT_VIX", 20240621
        )

    @pytest.mark.asyncio
    async def test_resolve_vix_futures_ref_swallows_data_error(self) -> None:
        """Underlying data error → None (mirrors the API endpoint policy
        of "do not 502 on a single missing underlying").
        """
        from tcg.engine.options.chain._forward import resolve_vix_futures_ref

        contract = _make_contract(
            collection="OPT_VIX",
            root_underlying="IND_VIX",
            underlying_ref=None,
        )
        svc = AsyncMock()
        svc.find_futures_contract_by_expiration.side_effect = RuntimeError("mongo down")

        result = await resolve_vix_futures_ref(contract, svc)

        assert result is None
