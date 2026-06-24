"""Roll date computation and overlap trimming for continuous futures."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from tcg.data._utils import date_to_int, int_to_date
from tcg.types.market import ContractPriceData, PriceSeries, RollStrategy


def compute_roll_dates(
    contracts: list[ContractPriceData],
    strategy: RollStrategy,
    roll_offset_days: int = 0,
) -> list[int]:
    """Compute YYYYMMDD roll dates for FRONT_MONTH strategy.

    For FRONT_MONTH: roll happens at expiration of each contract,
    shifted earlier by ``roll_offset_days`` calendar days.
    The roll date is the (possibly shifted) expiration date of the
    outgoing contract.
    Returns one date per roll boundary (len = len(contracts) - 1).

    Contracts must be sorted by expiration (ascending).
    """
    if len(contracts) <= 1:
        return []

    if strategy != RollStrategy.FRONT_MONTH:
        raise ValueError(f"Unsupported roll strategy: {strategy}")

    if roll_offset_days == 0:
        return [c.expiration for c in contracts[:-1]]

    offset = timedelta(days=roll_offset_days)
    return [date_to_int(int_to_date(c.expiration) - offset) for c in contracts[:-1]]


def trim_overlaps(
    contracts: list[ContractPriceData],
    roll_dates: list[int],
) -> list[ContractPriceData]:
    """Trim each contract's data to its FRONT-MONTH ownership window.

    Each contract owns only the dates over which it is the front (active)
    contract — the window ``[roll_dates[i-1], roll_dates[i]]``:

    - Contract 0:          keep ``dates <= roll_dates[0]``.
    - Contract i (middle): keep ``roll_dates[i-1] <= dates <= roll_dates[i]``.
    - Last contract:       keep ``dates >= roll_dates[-1]``.

    The LOWER bound is essential for a full forward curve. Real futures (e.g.
    ES) list and trade each contract for ~1-2 years *before* it becomes the
    front month, so a deferred contract carries a long history of nonzero
    back-month quotes. Without a lower bound, a deferred contract kept ALL of
    that early data and ``_concatenate``'s high-water (later-contract-wins)
    dedup awarded every shared date to the MOST-DEFERRED contract — the
    continuous series rode the wrong (illiquid, far-dated) contract, rolls
    fired ~a year early, and ``roll_offset`` had no effect. Constraining each
    contract to its front-month window makes every date owned by (essentially)
    one front contract, so rolls land at expiry(−offset) and ``roll_offset`` is
    meaningful.

    The lower bound is INCLUSIVE of ``roll_dates[i-1]`` (the previous contract's
    roll boundary) ON PURPOSE. The previous contract keeps that boundary day too
    (its ``dates <= roll_dates[i-1]`` upper bound), so adjacent front-month
    contracts overlap on EXACTLY that one seam day when both quote it. That
    single shared day is what the back-adjustment needs: ``_shared_close_at_roll``
    computes the roll gap from both contracts' closes on one contemporaneous day,
    giving a clean seam (no cross-date artifact) — the real VIX/ES near-expiry
    case where front and next trade together. ``_concatenate``'s high-water dedup
    then resolves that lone shared day to the new contract (later index wins) and
    is otherwise a harmless no-op. An EXCLUSIVE lower bound would strip the seam
    day and force the approximate nearest-date gap at every roll, degrading the
    adjustment; the inclusive bound preserves shared-day continuity while still
    discarding the long pre-window back-month history that caused the bug.

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

        # Front-month window: upper bound = this contract's roll boundary;
        # lower bound = the PREVIOUS contract's roll boundary, INCLUSIVE so the
        # one shared seam day survives for the back-adjustment gap (see
        # docstring). Everything earlier (long pre-window back-month history) is
        # dropped — that is what fixes the deferred-riding bug.
        mask = nonzero_mask
        if i < len(roll_dates):
            mask = mask & (ps.dates <= roll_dates[i])
        if i > 0:
            mask = mask & (ps.dates >= roll_dates[i - 1])

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
