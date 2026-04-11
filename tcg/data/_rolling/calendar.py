"""Roll date computation and overlap trimming for continuous futures."""

from __future__ import annotations

import numpy as np

from tcg.types.market import ContractPriceData, PriceSeries, RollStrategy


def compute_roll_dates(
    contracts: list[ContractPriceData],
    strategy: RollStrategy,
) -> list[int]:
    """Compute YYYYMMDD roll dates for FRONT_MONTH strategy.

    For FRONT_MONTH: roll happens at expiration of each contract.
    The roll date is the expiration date of the outgoing contract.
    Returns one date per roll boundary (len = len(contracts) - 1).

    Contracts must be sorted by expiration (ascending).
    """
    if len(contracts) <= 1:
        return []

    if strategy != RollStrategy.FRONT_MONTH:
        raise ValueError(f"Unsupported roll strategy: {strategy}")

    return [c.expiration for c in contracts[:-1]]


def trim_overlaps(
    contracts: list[ContractPriceData],
    roll_dates: list[int],
) -> list[ContractPriceData]:
    """Trim each contract's data at the next roll boundary.

    Contract i keeps dates <= roll_dates[i].
    Last contract keeps all its data.
    Strip rows with close == 0 (unlisted/untraded dates).

    Contracts with no remaining data after filtering are excluded.
    """
    if not contracts:
        return []

    trimmed: list[ContractPriceData] = []

    for i, contract in enumerate(contracts):
        ps = contract.prices

        # Strip zero-close rows first
        nonzero_mask = ps.close != 0.0

        # Apply roll boundary: contract i keeps dates <= roll_dates[i]
        if i < len(roll_dates):
            date_mask = ps.dates <= roll_dates[i]
            mask = nonzero_mask & date_mask
        else:
            # Last contract keeps all data (after zero stripping)
            mask = nonzero_mask

        if not np.any(mask):
            continue

        filtered = PriceSeries(
            dates=ps.dates[mask],
            open=ps.open[mask],
            high=ps.high[mask],
            low=ps.low[mask],
            close=ps.close[mask],
            volume=ps.volume[mask],
        )
        trimmed.append(
            ContractPriceData(
                contract_id=contract.contract_id,
                expiration=contract.expiration,
                prices=filtered,
            )
        )

    return trimmed
