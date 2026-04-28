"""Shared synthetic-chain fixtures for Module 3 unit tests.

No Mongo, no Module 1, no Module 4 internals.  Just data shapes.
"""

from __future__ import annotations

from datetime import date

from tcg.types.options import OptionContractDoc, OptionDailyRow

DEFAULT_DATE = date(2024, 3, 22)
DEFAULT_EXPIRATION = date(2024, 6, 21)


def make_contract(
    *,
    strike: float,
    type_: str = "C",
    collection: str = "OPT_SP_500",
    expiration: date = DEFAULT_EXPIRATION,
    contract_id: str | None = None,
    strike_factor_verified: bool = True,
    root_underlying: str = "IND_SP_500",
    underlying_ref: str | None = "FUT_SP_500_EMINI",
    provider: str = "IVOLATILITY",
) -> OptionContractDoc:
    cid = contract_id or f"{collection}_K{strike}_{type_}"
    return OptionContractDoc(
        collection=collection,
        contract_id=cid,
        root_underlying=root_underlying,
        underlying_ref=underlying_ref,
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle="M",
        strike=float(strike),
        type=type_,  # type: ignore[arg-type]
        contract_size=None,
        currency="USD",
        provider=provider,
        strike_factor_verified=strike_factor_verified,
    )


def make_row(
    *,
    row_date: date = DEFAULT_DATE,
    bid: float | None = 1.0,
    ask: float | None = 1.1,
    mid: float | None = 1.05,
    delta_stored: float | None = None,
    iv_stored: float | None = None,
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
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=None,
    )


def make_chain(
    strikes_and_deltas: list[tuple[float, float | None]],
    *,
    type_: str = "C",
    collection: str = "OPT_SP_500",
    expiration: date = DEFAULT_EXPIRATION,
    row_date: date = DEFAULT_DATE,
) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
    """Build a synthetic chain — list of (contract, row) tuples."""
    return [
        (
            make_contract(
                strike=K,
                type_=type_,
                collection=collection,
                expiration=expiration,
            ),
            make_row(row_date=row_date, delta_stored=d),
        )
        for K, d in strikes_and_deltas
    ]
