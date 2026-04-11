"""Back-adjustment math for continuous futures series.

All adjustment processing goes BACKWARD (last roll first) so that
adjustments cascade correctly — earlier prices accumulate all
subsequent adjustment factors.
"""

from __future__ import annotations

import numpy as np

from tcg.types.market import ContractPriceData, PriceSeries


def _find_closest_date_idx(dates: np.ndarray, target: int) -> int:
    """Find the index of the date closest to `target` in sorted `dates`."""
    idx = np.searchsorted(dates, target)
    if idx == len(dates):
        return len(dates) - 1
    if idx == 0:
        return 0
    # Pick whichever is closer
    if abs(dates[idx] - target) <= abs(dates[idx - 1] - target):
        return int(idx)
    return int(idx - 1)


def _get_close_at_roll(
    contract: ContractPriceData,
    roll_date: int,
) -> float:
    """Get the close price of a contract on or nearest to the roll date."""
    if len(contract.prices) == 0:
        return 0.0
    idx = _find_closest_date_idx(contract.prices.dates, roll_date)
    return float(contract.prices.close[idx])


def adjust_proportional(
    prices: PriceSeries,
    roll_dates: list[int],
    contracts: list[ContractPriceData],
) -> PriceSeries:
    """Multiply all prior prices by (new_close / old_close) at each roll boundary.

    Process backwards from the last roll to the first.
    At each roll date, find the close prices of both contracts on that date,
    compute the ratio, and multiply all OHLC prices before that date.
    Volume is left unchanged.

    Parameters
    ----------
    prices : PriceSeries
        The raw concatenated (unadjusted) series.
    roll_dates : list[int]
        YYYYMMDD roll dates (one per roll boundary).
    contracts : list[ContractPriceData]
        Trimmed contracts aligned with roll_dates (len = len(roll_dates) + 1).
        contracts[i] is the outgoing contract at roll_dates[i],
        contracts[i+1] is the incoming contract.
    """
    if not roll_dates:
        return prices

    assert len(contracts) == len(roll_dates) + 1, (
        f"contracts ({len(contracts)}) must be len(roll_dates) + 1 ({len(roll_dates) + 1})"
    )

    adj_open = prices.open.copy()
    adj_high = prices.high.copy()
    adj_low = prices.low.copy()
    adj_close = prices.close.copy()

    # Process BACKWARD: last roll first
    for i in range(len(roll_dates) - 1, -1, -1):
        rd = roll_dates[i]
        old_contract = contracts[i]
        new_contract = contracts[i + 1]

        old_close = _get_close_at_roll(old_contract, rd)
        new_close = _get_close_at_roll(new_contract, rd)

        if old_close == 0.0 or new_close == 0.0:
            continue  # Cannot compute meaningful ratio with zero prices

        ratio = new_close / old_close

        # Apply to all dates before the roll date
        mask = prices.dates < rd
        adj_open[mask] *= ratio
        adj_high[mask] *= ratio
        adj_low[mask] *= ratio
        adj_close[mask] *= ratio

    return PriceSeries(
        dates=prices.dates.copy(),
        open=adj_open,
        high=adj_high,
        low=adj_low,
        close=adj_close,
        volume=prices.volume.copy(),
    )


def adjust_difference(
    prices: PriceSeries,
    roll_dates: list[int],
    contracts: list[ContractPriceData],
) -> PriceSeries:
    """Add (new_close - old_close) to all prior prices at each roll boundary.

    Same backward processing as proportional, but additive instead of
    multiplicative. Volume is left unchanged.

    Parameters
    ----------
    prices : PriceSeries
        The raw concatenated (unadjusted) series.
    roll_dates : list[int]
        YYYYMMDD roll dates (one per roll boundary).
    contracts : list[ContractPriceData]
        Trimmed contracts aligned with roll_dates (len = len(roll_dates) + 1).
    """
    if not roll_dates:
        return prices

    assert len(contracts) == len(roll_dates) + 1, (
        f"contracts ({len(contracts)}) must be len(roll_dates) + 1 ({len(roll_dates) + 1})"
    )

    adj_open = prices.open.copy()
    adj_high = prices.high.copy()
    adj_low = prices.low.copy()
    adj_close = prices.close.copy()

    # Process BACKWARD: last roll first
    for i in range(len(roll_dates) - 1, -1, -1):
        rd = roll_dates[i]
        old_contract = contracts[i]
        new_contract = contracts[i + 1]

        old_close = _get_close_at_roll(old_contract, rd)
        new_close = _get_close_at_roll(new_contract, rd)

        diff = new_close - old_close

        # Apply to all dates before the roll date
        mask = prices.dates < rd
        adj_open[mask] += diff
        adj_high[mask] += diff
        adj_low[mask] += diff
        adj_close[mask] += diff

    return PriceSeries(
        dates=prices.dates.copy(),
        open=adj_open,
        high=adj_high,
        low=adj_low,
        close=adj_close,
        volume=prices.volume.copy(),
    )
