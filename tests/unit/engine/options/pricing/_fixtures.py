"""Shared fixture helpers for Module 2 unit tests."""

from __future__ import annotations

from datetime import date

from tcg.types.options import OptionContractDoc, OptionDailyRow


def make_contract(
    *,
    collection: str = "OPT_SP_500",
    contract_id: str = "OPT_SP_500_TEST_C",
    expiration: date = date(2024, 6, 21),
    strike: float = 100.0,
    type_: str = "C",
    strike_factor_verified: bool = True,
    root_underlying: str = "IND_SP_500",
    underlying_ref: str | None = None,
    provider: str = "IVOLATILITY",
) -> OptionContractDoc:
    return OptionContractDoc(
        collection=collection,
        contract_id=contract_id,
        root_underlying=root_underlying,
        underlying_ref=underlying_ref,
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle="M",
        strike=strike,
        type=type_,  # type: ignore[arg-type]
        contract_size=None,
        currency="USD",
        provider=provider,
        strike_factor_verified=strike_factor_verified,
    )


def make_row(
    *,
    row_date: date = date(2024, 3, 22),
    bid: float | None = 3.95,
    ask: float | None = 4.03,
    mid: float | None = 3.99,
    iv_stored: float | None = None,
    delta_stored: float | None = None,
    gamma_stored: float | None = None,
    theta_stored: float | None = None,
    vega_stored: float | None = None,
) -> OptionDailyRow:
    return OptionDailyRow(
        date=row_date,
        open=None,
        high=None,
        low=None,
        close=None,
        bid=bid,
        ask=ask,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=None,
        mid=mid,
        iv_stored=iv_stored,
        delta_stored=delta_stored,
        gamma_stored=gamma_stored,
        theta_stored=theta_stored,
        vega_stored=vega_stored,
        underlying_price_stored=None,
    )
