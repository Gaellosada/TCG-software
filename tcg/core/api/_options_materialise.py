"""Shared option-stream materialisation logic.

Extracted from ``tcg.core.api.indicators`` so both the indicators and
options routers (and future consumers like the portfolio router) can
reuse the same materialisation path without circular imports.

Public API
----------
* ``materialise_option_streams``  -- bulk materialiser (N labels)
* ``_materialise_option_stream``  -- single-label convenience wrapper
* ``_business_dates_in_range``    -- CME business-day enumeration
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas_market_calendars as mcal

from tcg.core.api._models import OptionStreamRef
from tcg.core.api._options_wiring import build_stream_resolver_wiring
from tcg.data._utils import date_to_int
from tcg.data.protocols import MarketDataService
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    MaturityRule,
    NearestToTarget,
    OptionContractDoc,
    expand_cycle,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def fetch_nearest_target_expirations_by_date(
    *,
    svc: MarketDataService,
    maturity: MaturityRule,
    collection: str,
    option_type: str,
    cycle: str | Sequence[str] | None,
    trade_dates: list[date],
) -> dict[date, list[date]] | None:
    """Per-date LISTED-expiration map for a ``NearestToTarget`` option stream.

    ONE shared fetch used by every option-stream materialisation path
    (``materialise_option_streams`` — the /api/options/stream chart, Indicators,
    and portfolio level legs — plus the signals/basket fetcher in
    ``_series_fetch``) so the daily-expiration global-snap fix (Issue #2) is
    applied uniformly instead of only on the signals path.

    Only ``NearestToTarget`` consults the map (arithmetic maturity rules snap
    via the resolver's ``_snap_to_listed`` on the global list already), so for
    every other rule — and for an empty window — this returns ``None`` and the
    caller skips the scan.

    ``cycle`` MUST already be ``expand_cycle``-broadened by the caller (the same
    expanded value feeds the expiration list AND the chain fetch, so the two
    never disagree).

    The scan is capped at ``trade_dates[-1] + max(3*target_dte_days, 180)`` — the
    SAME upper bound the resolver's own probe window uses (``far_future``), so no
    expiration the resolver could pick is dropped, while far-dated LEAPS no
    longer inflate the price-join scan.
    """
    if not isinstance(maturity, NearestToTarget) or not trade_dates:
        return None
    probe_days = max(maturity.target_dte_days * 3, 180)
    expiration_max = trade_dates[-1] + timedelta(days=probe_days)
    return await svc.list_option_expirations_by_date(
        collection,
        trade_dates[0],
        trade_dates[-1],
        option_type=option_type,
        cycle=cycle,
        expiration_max=expiration_max,
    )


def _business_dates_in_range(start: date | None, end: date | None) -> list[date] | None:
    """Enumerate CME business days in [start, end].

    ``OptionStreamRef`` materialisation needs an explicit date axis
    (no underlying price series in the request to borrow it from).
    We enumerate business days on the same calendar Module 4 uses
    (``CME_TradeDate``).  ``None`` is returned when the range is
    invalid or empty -- the caller surfaces a 400 in that case.
    """
    if start is None or end is None or start > end:
        return None
    cal = mcal.get_calendar("CME_TradeDate")
    vd = cal.valid_days(start_date=start, end_date=end)
    return [ts.date() for ts in vd]


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------


async def materialise_option_streams(
    refs_with_labels: list[tuple[str, OptionStreamRef]],
    *,
    svc: MarketDataService,
    start_date: date | None,
    end_date: date | None,
    progress_callback=None,
) -> (
    dict[
        str,
        tuple[
            np.ndarray,
            np.ndarray,
            list[str | None],
            list[OptionContractDoc | None],
        ],
    ]
    | str
):
    """Materialise one or more ``OptionStreamRef`` into keyed results.

    Parameters
    ----------
    refs_with_labels:
        List of ``(label, ref)`` tuples.  Each ref is resolved
        independently; results are keyed by label.
    svc:
        The ``MarketDataService`` providing MongoDB access.
    start_date, end_date:
        ISO date boundaries.  Both required for option streams.
    progress_callback:
        Invoked once per resolved trade date per ref -- the caller
        wires it to ``_progress_tick`` for FE progress polling.

    Returns
    -------
    A dict ``{label: (dates_arr, values, diagnostics, contracts)}`` on
    success, or a string error message when the date range is missing.
    ``contracts[i]`` is the ``OptionContractDoc`` selected on date i
    (or ``None`` when selection failed); used by the API layer to
    derive ``rolls`` at ``contract_id`` transitions.
    """
    # Lazy import to avoid circular dependency with options.py which
    # defines these converters and also imports from this module.
    from tcg.core.api.options import (
        _criterion_pydantic_to_dataclass,
        _maturity_pydantic_to_dataclass,
        _roll_offset_pydantic_to_dataclass,
    )

    trade_dates = _business_dates_in_range(start_date, end_date)
    if not trade_dates:
        return "option_stream requires explicit ISO 'start' and 'end' dates"

    # Pass the resolve window so the futures adapter memoizes the underlying: one
    # ranged fetch per distinct future over the window instead of one single-date
    # fetch per trade date (the ByMoneyness/ByDelta Phase-C N+1).  Result-invariant.
    # trade_dates is non-empty here, so the window spans every date we look up.
    _prefetch = (trade_dates[0], trade_dates[-1])
    chain_reader, mat_resolver, ul_resolver, bulk_reader = build_stream_resolver_wiring(
        svc, underlying_prefetch_window=_prefetch
    )

    # Process-wide dwh-pool concurrency gate: streams here resolve sequentially,
    # but OTHER requests (basket series, a second chart panel) may resolve
    # concurrently against the SAME 4-slot pool — the shared gate bounds the SUM
    # so the pool is never over-subscribed (see _options_concurrency).
    from tcg.core.api._options_concurrency import get_dwh_concurrency_gate

    gate = get_dwh_concurrency_gate()

    results: dict[
        str,
        tuple[
            np.ndarray,
            np.ndarray,
            list[str | None],
            list[OptionContractDoc | None],
        ],
    ] = {}
    for label, ref in refs_with_labels:
        # Pre-fetch available expirations filtered by the requested type
        # and cycle.  The unfiltered variant returned expirations for ALL
        # types / cycles, causing the bulk resolver to pick expirations
        # that had no matching contracts -- empty chains -> spurious NaN
        # holes.
        #
        # ``expand_cycle`` broadens the "Monthly" filter ('M') to the full
        # 3rd-Friday series ({'M','W3 Friday'}) so an option-stream series tracks
        # the real monthly across eras (see expand_cycle); the same expanded value
        # feeds the expiration list AND the chain fetch.  Other cycles unchanged.
        _cycle = expand_cycle(ref.cycle)
        all_expirations = await svc.list_option_expirations_filtered(
            ref.collection,
            option_type=ref.option_type,
            cycle=_cycle,
        )
        _maturity = _maturity_pydantic_to_dataclass(ref.maturity)
        # Issue #2 fix (was signals-path-only): for NearestToTarget, fetch the
        # per-date LISTED-expiration map so the resolver snaps to an expiration
        # actually quoted on each trade date instead of the whole-window global
        # nearest.  Applies now to /api/options/stream, Indicators and portfolio
        # level legs — every consumer of this shared materialiser.
        available_by_date = await fetch_nearest_target_expirations_by_date(
            svc=svc,
            maturity=_maturity,
            collection=ref.collection,
            option_type=ref.option_type,
            cycle=_cycle,
            trade_dates=trade_dates,
        )
        values, diagnostics, contracts = await resolve_option_stream(
            dates=trade_dates,
            collection=ref.collection,
            option_type=ref.option_type,
            cycle=_cycle,
            maturity=_maturity,
            selection=_criterion_pydantic_to_dataclass(ref.selection),
            stream=ref.stream,
            roll_offset=_roll_offset_pydantic_to_dataclass(ref.roll_offset),
            chain_reader=chain_reader,
            maturity_resolver=mat_resolver,
            underlying_price_resolver=ul_resolver,
            progress_callback=progress_callback,
            bulk_chain_reader=bulk_reader,
            available_expirations=all_expirations,
            available_expirations_by_date=available_by_date,
            concurrency_gate=gate,
        )
        dates_arr = np.array([date_to_int(d) for d in trade_dates], dtype=np.int64)
        results[label] = (dates_arr, values, diagnostics, contracts)

    return results


# ---------------------------------------------------------------------------
# Roll-event derivation
# ---------------------------------------------------------------------------


def _contract_meta(c: OptionContractDoc, v: float | None) -> dict[str, Any]:
    """Format one side of a roll event (sold or bought).

    Single helper used for both sides — no duplicated formatting.
    The ``value`` field is the plotted-series value on the relevant
    date (``values[i-1]`` for sold, ``values[i]`` for bought).  May
    be ``None`` when the corresponding ``values[i]`` is NaN.

    ``root`` is ``OptionContractDoc.root_underlying`` (e.g.
    ``"IND_SP_500"``), NOT ``collection`` — the human-readable asset
    name.  ``contract_id`` is included to disambiguate same-strike
    same-expiration multi-cycle contracts (SPX vs SPXW).
    """
    return {
        "contract_id": c.contract_id,
        "root": c.root_underlying,
        "expiration": c.expiration.isoformat(),
        "strike": c.strike,
        "type": c.type,
        "value": v,
    }


def derive_rolls(
    dates: list[str],
    values: list[float | None],
    contracts: list[OptionContractDoc | None],
) -> list[dict[str, Any]]:
    """Derive roll events from a per-date contract array.

    A roll event is emitted on date ``dates[i]`` when both
    ``contracts[i-1]`` and ``contracts[i]`` are non-None AND their
    ``expiration`` differs — i.e. a true *maturity* roll.  Within one
    stream the root/type/cycle are fixed, so ``expiration`` is the
    maturity discriminator.  Same-expiration strike re-selection (the
    daily strike churn produced by delta/moneyness tracking) is NOT a
    roll and emits no marker.  Missing chain on either side ⇒ no roll.
    (With no ``cycle`` filter the chain may mix cycles — e.g. a weekly and
    a monthly contract sharing a settlement Friday; two consecutive
    selections on the SAME ``expiration`` date are treated as the same
    maturity and emit no marker even if their ``contract_id``/cycle
    differ, since a roll tracks maturity, not contract identity.)

    The roll cadence is governed by the maturity target (and
    ``roll_offset``), which is what changes the selected expiration; it
    is no longer driven by per-date strike identity.  For ByStrike this
    is unchanged behaviour (a fixed strike only changes ``contract_id``
    when the expiration rolls anyway).  Each event's ``sold``/``bought``
    payload still carries the per-side ``contract_id`` for display.

    Parameters
    ----------
    dates:
        ISO ``YYYY-MM-DD`` strings, one per trade date (parallel to
        the other two arrays).
    values:
        The plotted-series value on each date (``None`` where NaN).
        Used to populate ``sold.value`` (= ``values[i-1]``) and
        ``bought.value`` (= ``values[i]``).
    contracts:
        ``OptionContractDoc | None``, parallel.  ``None`` where
        selection failed.

    Returns
    -------
    A list of roll-event dicts; see ``_contract_meta`` for side shape.
    Empty list when no transitions are detected.
    """
    out: list[dict[str, Any]] = []
    n = len(dates)
    if n != len(values) or n != len(contracts):  # pragma: no cover (defensive)
        raise ValueError(
            "derive_rolls: dates, values, contracts must be the same length"
        )
    for i in range(1, n):
        prev = contracts[i - 1]
        curr = contracts[i]
        if prev is None or curr is None:
            continue
        # Maturity-only roll: a marker fires only when the *expiration*
        # changes, not on same-expiration strike re-selection (delta/
        # moneyness tracking churns the strike — hence contract_id —
        # nearly every trading day, which is not a roll).
        if prev.expiration == curr.expiration:
            continue
        out.append(
            {
                "date": dates[i],
                "sold": _contract_meta(prev, values[i - 1]),
                "bought": _contract_meta(curr, values[i]),
            }
        )
    return out


async def _materialise_option_stream(
    ref: OptionStreamRef,
    *,
    svc: MarketDataService,
    start_date: date | None,
    end_date: date | None,
    progress_callback=None,
) -> tuple[np.ndarray, np.ndarray, list[str | None]] | str:
    """Materialise a single ``OptionStreamRef`` into ``(dates, values, diagnostics)``.

    Thin wrapper around :func:`materialise_option_streams` for backward
    compatibility with the ``/api/indicators/compute`` handler.

    Returns the triple on success or a string error message when the
    date range is missing.
    """
    result = await materialise_option_streams(
        [("_single", ref)],
        svc=svc,
        start_date=start_date,
        end_date=end_date,
        progress_callback=progress_callback,
    )
    if isinstance(result, str):
        return result
    dates_arr, values, diagnostics, _contracts = result["_single"]
    return dates_arr, values, diagnostics
