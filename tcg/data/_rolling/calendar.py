"""Roll date computation and overlap trimming for continuous futures."""

from __future__ import annotations

import functools
import logging
from calendar import monthrange
from datetime import date, timedelta

import numpy as np
import pandas_market_calendars as mcal

from tcg.data._utils import date_to_int, int_to_date
from tcg.types.market import ContractPriceData, PriceSeries, RollStrategy

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Last-trading-day-of-month helper
# ---------------------------------------------------------------------------
#
# INTENTIONAL DUPLICATION.  This is a ~12-line copy of
# ``tcg.engine.options.maturity.resolver._last_business_day_of_month`` (+ its
# cached "CME"→"CME_TradeDate" calendar plumbing).  The engine helper is NOT
# imported here on purpose: the import-linter ``engine-data-isolation`` contract
# declares ``tcg.engine`` and ``tcg.data`` mutually independent, so importing
# the engine copy would break the gate.  Duplicating a leaf helper is the
# correct call (vs coupling two sibling layers or polluting the dep-free
# ``tcg.types`` with a calendar dependency).  ``test_rolling_eom.py``'s
# ``test_matches_engine_helper`` pins the two copies to stay identical.
#
# This is also the first ``pandas_market_calendars`` use inside ``tcg.data``;
# every other calendar-aware path lives in ``tcg.engine`` / ``tcg.core``.


@functools.lru_cache(maxsize=1)
def _cme_calendar():  # type: ignore[return]
    """Return the cached CME trade-date calendar.

    "CME" is the spec-level name; "CME_TradeDate" is the registered
    pandas_market_calendars key (mirrors the engine resolver's alias).
    """
    return mcal.get_calendar("CME_TradeDate")


def _last_trading_day_of_month(year: int, month: int) -> date:
    """Return the last trading day of the given (year, month) on the CME
    trade-date calendar.

    Uses the trading calendar (not the naive calendar last day) so a month-end
    weekend/holiday rolls back to the prior trading day — e.g. 2024-03 returns
    the 28th because the 29th (Good Friday) is a CME holiday.
    """
    last_day = date(year, month, monthrange(year, month)[1])
    first_day = date(year, month, 1)
    vd = _cme_calendar().valid_days(start_date=first_day, end_date=last_day)
    if len(vd) == 0:
        raise ValueError(f"No valid trading days in {year}-{month:02d} for calendar")
    return vd[-1].date()


def compute_roll_dates(
    contracts: list[ContractPriceData],
    strategy: RollStrategy,
    roll_offset_days: int = 0,
) -> list[int]:
    """Compute YYYYMMDD roll dates for the chosen roll strategy.

    FRONT_MONTH
        Roll at expiration of each outgoing contract, shifted earlier by
        ``roll_offset_days`` calendar days.

    END_OF_MONTH (Issue #3)
        Roll on the last TRADING day of each outgoing contract's expiration
        month, regardless of the contract's actual expiry, then shifted earlier
        by ``roll_offset_days`` (composes exactly as for FRONT_MONTH).  This is
        the only behavioural change vs FRONT_MONTH — it re-times *where* each
        boundary lands (month-end instead of expiry-day) while keeping the 1:1
        contract↔boundary mapping that ``trim_overlaps`` / ``_concatenate`` /
        adjustment depend on, so those stages are untouched.

    Returns one date per roll boundary (len = len(contracts) - 1) for
    FRONT_MONTH; END_OF_MONTH may return FEWER when two consecutive contracts
    resolve to the same month-end (cycle=None edge — see the duplicate guard).

    Contracts must be sorted by expiration (ascending).
    """
    if len(contracts) <= 1:
        return []

    if strategy == RollStrategy.END_OF_MONTH:
        offset = timedelta(days=roll_offset_days) if roll_offset_days else None
        rolls: list[int] = []
        for c in contracts[:-1]:
            exp = int_to_date(c.expiration)
            eom = _last_trading_day_of_month(exp.year, exp.month)
            if offset is not None:
                eom = eom - offset
            eom_int = date_to_int(eom)
            # cycle=None edge: two consecutive contracts expiring in the SAME
            # month resolve to the same month-end roll date → the boundaries
            # collapse and ``trim_overlaps`` would get a degenerate zero-width
            # window.  Drop the duplicate (non-increasing) boundary and warn;
            # the contract whose boundary we skip is subsumed by its neighbour's
            # window.  Harmless with a proper monthly/quarterly cycle filter.
            if rolls and eom_int <= rolls[-1]:
                _log.warning(
                    "END_OF_MONTH: contract %s resolves to a non-increasing "
                    "month-end roll date %d (<= previous %d) — dropping the "
                    "duplicate boundary (use a cycle filter to avoid same-month "
                    "contracts)",
                    c.contract_id,
                    eom_int,
                    rolls[-1],
                )
                continue
            rolls.append(eom_int)
        return rolls

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
