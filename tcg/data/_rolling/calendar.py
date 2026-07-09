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


def _first_tradeable_int(contract: ContractPriceData) -> int | None:
    """First YYYYMMDD date on which the contract has a POSITIVE close.

    Non-positive-close rows are unlisted/untraded placeholders that
    ``trim_overlaps`` strips — exact-0 rows and the ``-2.147480`` (INT32_MIN/1e9)
    "no quote" sentinel that real weekly FUT_VIX contracts carry on their first
    listed day. A futures close is a price, so ``<= 0`` is never a real quote; a
    contract's *usable* history starts at its first ``close > 0``.
    Returns ``None`` when the contract never traded (no positive closes).
    """
    ps = contract.prices
    nz = ps.close > 0.0
    if not np.any(nz):
        return None
    return int(ps.dates[nz][0])  # dates are ascending


def _month_representative(members: list[ContractPriceData]) -> ContractPriceData:
    """Pick the one contract that represents an expiration month.

    Preference order (see :func:`collapse_to_one_per_month`):
    1. Restrict to contracts that actually traded (≥1 nonzero close); fall back
       to all members if none did.
    2. Prefer the canonical MONTHLY-cycle contract (``expiration_cycle == 'M'``).
    3. Among the survivors, take the latest expiration; break ties on
       ``contract_id`` for determinism.
    """
    with_data = [c for c in members if _first_tradeable_int(c) is not None]
    pool = with_data or members
    monthly = [c for c in pool if c.expiration_cycle == "M"]
    candidates = monthly or pool
    return max(candidates, key=lambda c: (c.expiration, c.contract_id))


def collapse_to_one_per_month(
    contracts: list[ContractPriceData],
) -> list[ContractPriceData]:
    """Keep exactly one contract per expiration month (END_OF_MONTH pre-step).

    END_OF_MONTH rolls at each month-end and relies on a 1:1 contract↔boundary
    mapping — ``compute_roll_dates`` returns ``len(contracts) - 1`` boundaries
    and ``trim_overlaps`` / ``_concatenate`` index ``roll_dates`` by contract
    position. Roots with sub-monthly listings break that: VIX lists ~5 WEEKLY
    futures per month (BTC/ETH list DAILY ones), so several contracts resolve to
    the SAME month-end. The old duplicate-boundary guard dropped those collapsed
    boundaries, leaving ``len(roll_dates) < len(contracts) - 1``; ``trim_overlaps``
    then read ``roll_dates[i - 1]`` past the end of the list → ``IndexError`` →
    HTTP 500 on the Data page ("End of month" roll on FUT_VIX).

    Fix: before rolling, keep ONE contract per expiration month via
    :func:`_month_representative` — the canonical MONTHLY-cycle contract
    (``expiration_cycle == 'M'``) when the root marks one, else the latest-
    expiring contract that actually traded. Preferring the monthly (not merely
    the latest expiry) matters two ways: (a) it is the contract the rest of the
    platform treats as "the" VIX/crypto future, not an end-of-month weekly a day
    from expiry; (b) the monthly carries far more listed history (~9 months vs a
    weekly's ~7 weeks), so a large ``roll_offset`` does not push its window
    before it was listed. No-op for single-contract-per-month roots (ES
    quarterly, pre-2015 VIX). Does not touch ``trim_overlaps`` / ``_concatenate``
    / adjustment.

    Deterministic and order-independent: the per-month winner depends only on
    ``(expiration_cycle, expiration, contract_id)``, not input order; the result
    is returned sorted ascending by expiration (as the builder requires).
    """
    groups: dict[tuple[int, int], list[ContractPriceData]] = {}
    for c in contracts:
        exp = int_to_date(c.expiration)
        groups.setdefault((exp.year, exp.month), []).append(c)
    winners = [_month_representative(members) for members in groups.values()]
    return sorted(winners, key=lambda c: c.expiration)


def clamp_roll_dates_to_data(
    contracts: list[ContractPriceData],
    roll_dates: list[int],
) -> list[int]:
    """Clamp each roll boundary so it never precedes the incoming contract's data.

    ``roll_dates[i]`` hands ownership from ``contracts[i]`` to ``contracts[i+1]``.
    A large ``roll_offset_days`` shifts every boundary earlier; once a boundary
    lands before the incoming contract's first listed (tradeable) day, that
    contract's ``trim_overlaps`` window is entirely before its data → the mask is
    empty → the contract is dropped, leaving a multi-year hole in a "continuous"
    series that is still returned as HTTP 200 (silent corruption). This is acute
    for short-history roots (VIX weeklies ~7 weeks of data): a 90-day offset —
    the "~3 months out" the feature advertises — otherwise disintegrates the
    series.

    Fix: clamp ``roll_dates[i]`` UP to ``contracts[i+1]``'s first tradeable date,
    so the roll happens as early as the data actually allows (never earlier).
    Clamping up preserves monotonicity (both the base boundaries and the incoming
    first-dates are ascending); a boundary is left unchanged when the incoming
    contract has no tradeable data (it would be dropped regardless). At the
    default offset of 0 the boundary sits at/after expiry, well after the
    incoming contract's listing, so this is a no-op for normal series.
    """
    clamped: list[int] = []
    prev = None
    for i, rd in enumerate(roll_dates):
        first_incoming = _first_tradeable_int(contracts[i + 1])
        value = rd if first_incoming is None else max(rd, first_incoming)
        if prev is not None and value < prev:
            value = prev  # keep non-decreasing
        clamped.append(value)
        prev = value
    return clamped


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

    Returns one date per roll boundary (len = len(contracts) - 1). For
    END_OF_MONTH the builder first runs :func:`collapse_to_one_per_month`, so
    each contract is in a distinct month and the month-ends are strictly
    increasing — the duplicate guard below therefore never fires via the real
    pipeline. It is retained only to keep a *direct* caller that passes
    same-month contracts from producing a degenerate boundary (and
    ``trim_overlaps`` additionally tolerates a short list without IndexError).

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


def prepare_nth_nearest(
    contracts: list[ContractPriceData],
    rank: int,
    roll_offset_days: int = 0,
) -> tuple[list[ContractPriceData], list[int]]:
    """Return the (held_contracts, roll_dates) for the NTH_NEAREST strategy.

    ``contracts`` must be sorted ascending by expiration (the builder guarantees
    this) and already restricted to the desired cycle (the SQL fetch applies the
    ``cycle`` filter, so ``rank`` counts WITHIN the cycle-filtered set — the
    documented convention: a ~3-month VIX is ``rank=3`` with the monthly cycle).

    Semantics: between the expiry of front contract ``p-1`` and front contract
    ``p`` the live contracts are ``contracts[p:]``, so the rank-th nearest is
    ``contracts[p + rank - 1]``. A roll therefore fires at each front-contract
    expiry (shifted earlier by ``roll_offset_days``, composing exactly as for
    FRONT_MONTH) and ownership shifts up by one contract. Consequently:

    - ``held_contracts = contracts[rank - 1:]`` (the contracts that ever become
      the rank-th nearest, in order);
    - ``roll_dates[p] = contracts[p].expiration (− offset)`` for
      ``p in 0 .. len(held) - 2`` — the FRONT expiry that triggers each shift,
      NOT the held contract's own (later) expiry.

    ``rank == 1`` reproduces FRONT_MONTH exactly (held == contracts, roll dates ==
    front expiries). Fewer than ``rank`` contracts → no rank-th nearest ever
    exists → ``([], [])``. Exactly ``rank`` contracts → a single held contract and
    no rolls.
    """
    if rank < 1:
        raise ValueError(f"NTH_NEAREST rank must be >= 1, got {rank}")
    if len(contracts) < rank:
        return [], []

    held = contracts[rank - 1 :]
    n_rolls = len(held) - 1  # one roll per front-contract expiry that shifts us
    offset = timedelta(days=roll_offset_days) if roll_offset_days else None
    roll_dates: list[int] = []
    for p in range(n_rolls):
        exp = int_to_date(contracts[p].expiration)
        if offset is not None:
            exp = exp - offset
        roll_dates.append(date_to_int(exp))
    return held, roll_dates


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

    Strip rows with close <= 0 (unlisted/untraded dates and no-quote sentinels).

    Contracts with no remaining data after filtering are excluded.
    """
    if not contracts:
        return []

    trimmed: list[ContractPriceData] = []

    for i, contract in enumerate(contracts):
        ps = contract.prices

        # Strip non-positive-close rows first: exact-0 unlisted placeholders AND
        # the ``-2.147480`` no-quote sentinel weekly FUT_VIX contracts carry on
        # their first listed day. A futures close <= 0 is never a real price.
        nonzero_mask = ps.close > 0.0

        # Front-month window: upper bound = this contract's roll boundary;
        # lower bound = the PREVIOUS contract's roll boundary, INCLUSIVE so the
        # one shared seam day survives for the back-adjustment gap (see
        # docstring). Everything earlier (long pre-window back-month history) is
        # dropped — that is what fixes the deferred-riding bug.
        # The bounds are indexed by contract position, which assumes
        # ``len(roll_dates) == len(contracts) - 1`` (the builder guarantees this,
        # incl. the END_OF_MONTH collapse). The ``i - 1 < len(roll_dates)`` guard
        # is defensive belt-and-braces: a direct caller passing a shorter
        # roll_dates list must degrade to a no-lower-bound window, never
        # IndexError (the original FUT_VIX crash was exactly this over-index).
        mask = nonzero_mask
        if i < len(roll_dates):
            mask = mask & (ps.dates <= roll_dates[i])
        if 0 < i <= len(roll_dates):
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
                expiration_cycle=contract.expiration_cycle,
            )
        )

    return trimmed
