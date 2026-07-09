"""Back-adjustment math for continuous futures series.

All adjustment processing goes BACKWARD (last roll first) so that
adjustments cascade correctly — earlier prices accumulate all
subsequent adjustment factors.
"""

from __future__ import annotations

import logging

import numpy as np

from tcg.types.market import ContractPriceData, PriceSeries

logger = logging.getLogger(__name__)


def _find_closest_date_idx(dates: np.ndarray, target: int) -> int:
    """Find the index of the date closest to `target` in sorted `dates`.

    NOTE: distance is computed on YYYYMMDD integers, which is non-uniform
    across month boundaries (e.g., 20240131→20240201 = 70 vs 20240201→20240202 = 1).
    In practice this only matters when the exact date is missing and two candidates
    straddle a month boundary — the resulting price difference is negligible.
    """
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
    """Get the close price of a contract on or nearest to the roll date.

    NOTE: this is the *approximate* (nearest-date) lookup. It is only used as
    a last-resort fallback when the two contracts share no common trading day
    at/before the roll (pure abutment / sparse data). For the normal case use
    ``_shared_close_at_roll``, which guarantees both closes are quoted on the
    SAME calendar date — see the docstring there for the correctness rationale.
    """
    if len(contract.prices) == 0:
        return 0.0
    idx = _find_closest_date_idx(contract.prices.dates, roll_date)
    return float(contract.prices.close[idx])


def _shared_close_at_roll(
    old_contract: ContractPriceData,
    new_contract: ContractPriceData,
    roll_date: int,
) -> tuple[float, float] | None:
    """Return (old_close, new_close) quoted on a single SHARED trading day.

    The roll gap must be computed from both contracts' closes on the *same*
    calendar date, otherwise the back-adjustment leaves a residual artificial
    jump at the seam (the factor would mix prices from different days). This
    mirrors the legacy Java oracle ``DayStructure.backAdjustFromDayStructures*``
    (simulator), where ``previousDayStructure`` (old contract) and
    ``currentDayStructure`` (new contract) are both re-derived from the SAME
    generic roll date — the gap is ``currentClose / previousClose`` (geometric)
    or ``currentClose - previousClose`` (arithmetic) on one contemporaneous day.

    We pick the LATEST date present in BOTH contracts' date arrays with
    ``date <= roll_date`` — i.e. the last overlapping trading day at/before the
    roll. If ``roll_date`` itself is shared, that date is used.

    Returns ``None`` when the contracts have no common trading day at/before the
    roll (pure abutment or sparse/disjoint data); callers fall back to the
    approximate nearest-date gap and warn.
    """
    old_dates = old_contract.prices.dates
    new_dates = new_contract.prices.dates
    if len(old_dates) == 0 or len(new_dates) == 0:
        return None

    # Candidate shared dates are the intersection of both date arrays, restricted
    # to dates at/before the roll boundary. np.intersect1d returns a sorted array.
    shared = np.intersect1d(old_dates, new_dates)
    if shared.size == 0:
        return None
    eligible = shared[shared <= roll_date]
    if eligible.size == 0:
        return None

    ref_date = int(eligible[-1])  # latest shared day at/before the roll
    old_idx = int(np.searchsorted(old_dates, ref_date))
    new_idx = int(np.searchsorted(new_dates, ref_date))
    return float(old_contract.prices.close[old_idx]), float(
        new_contract.prices.close[new_idx]
    )


def _gap_closes_at_roll(
    old_contract: ContractPriceData,
    new_contract: ContractPriceData,
    roll_date: int,
) -> tuple[float, float, bool]:
    """Resolve the (old_close, new_close) pair used for one roll gap.

    Returns ``(old_close, new_close, approximate)``. ``approximate`` is True
    when no shared trading day exists and the nearest-date fallback was used
    (the gap mixes prices from different dates and is therefore inexact).
    """
    shared = _shared_close_at_roll(old_contract, new_contract, roll_date)
    if shared is not None:
        old_close, new_close = shared
        return old_close, new_close, False

    # Fallback: no common trading day at/before the roll (pure abutment / sparse
    # data). Use the nearest-date lookup for each contract independently. This
    # leaves the gap approximate because the two closes are on different days.
    old_close = _get_close_at_roll(old_contract, roll_date)
    new_close = _get_close_at_roll(new_contract, roll_date)
    return old_close, new_close, True


def adjust_ratio(
    prices: PriceSeries,
    roll_dates: list[int],
    contracts: list[ContractPriceData],
) -> PriceSeries:
    """Multiply all prior prices by (new_close / old_close) at each roll boundary.

    Ratio adjustment (formerly called "proportional"). Process backwards
    from the last roll to the first. At each roll boundary the gap is computed
    from both contracts' closes on a single SHARED trading day at/before the
    roll date (``_shared_close_at_roll``) so that no residual artificial jump
    remains at the seam; the resulting ratio multiplies all OHLC prices before
    the roll date. Volume is left unchanged.

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

        # Gap from a single shared trading day (legacy-Java parity). Falls back
        # to the approximate nearest-date gap only when the contracts share no
        # common day at/before the roll.
        old_close, new_close, approximate = _gap_closes_at_roll(
            old_contract, new_contract, rd
        )
        if approximate:
            logger.warning(
                "Ratio roll at %d: no shared trading day between %s and %s; "
                "gap is APPROXIMATE (old_close=%.4f, new_close=%.4f on "
                "different dates).",
                rd,
                old_contract.contract_id,
                new_contract.contract_id,
                old_close,
                new_close,
            )

        # Non-positive/NaN guard (symmetric with adjust_difference): a
        # reference close that is <= 0 or NaN cannot produce a meaningful gap.
        # This covers exact-0 unlisted rows, NaN, AND the negative ``-2.147480``
        # no-quote sentinel weekly FUT_VIX contracts carry: a negative reference
        # close would make ``new/old < 0`` and SIGN-FLIP all prior history (the
        # root of the ~1e63 FUT_VIX ratio blow-up). Skip the roll instead.
        if (
            old_close <= 0.0
            or new_close <= 0.0
            or not np.isfinite(old_close)
            or not np.isfinite(new_close)
        ):
            logger.warning(
                "Ratio roll skipped at %d: old_close=%.4f, new_close=%.4f "
                "(contracts %s → %s). Unadjusted gap remains.",
                rd,
                old_close,
                new_close,
                old_contract.contract_id,
                new_contract.contract_id,
            )
            continue

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

    Same backward processing as ratio adjustment, but additive instead of
    multiplicative. The gap is computed from a single SHARED trading day at/
    before the roll date (``_shared_close_at_roll``), matching the legacy Java
    arithmetic oracle. Volume is left unchanged.

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

        # Gap from a single shared trading day (legacy-Java parity), with the
        # same nearest-date fallback as ratio when no shared day exists.
        old_close, new_close, approximate = _gap_closes_at_roll(
            old_contract, new_contract, rd
        )
        if approximate:
            logger.warning(
                "Difference roll at %d: no shared trading day between %s and %s; "
                "gap is APPROXIMATE (old_close=%.4f, new_close=%.4f on "
                "different dates).",
                rd,
                old_contract.contract_id,
                new_contract.contract_id,
                old_close,
                new_close,
            )

        # Non-positive/NaN guard — SYMMETRIC with adjust_ratio. A close <= 0
        # (exact-0 unlisted row, or the negative ``-2.147480`` no-quote sentinel
        # weekly FUT_VIX carries) is not a real price: treating diff against it
        # would shift all history by a spurious amount. A NaN would poison the
        # entire series. Skip such rolls rather than corrupt the output.
        if (
            old_close <= 0.0
            or new_close <= 0.0
            or not np.isfinite(old_close)
            or not np.isfinite(new_close)
        ):
            logger.warning(
                "Difference roll skipped at %d: old_close=%.4f, new_close=%.4f "
                "(contracts %s → %s). Unadjusted gap remains.",
                rd,
                old_close,
                new_close,
                old_contract.contract_id,
                new_contract.contract_id,
            )
            continue

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
