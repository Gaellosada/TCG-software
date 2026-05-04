"""Shared option-stream materialisation logic.

Extracted from ``tcg.core.api.indicators`` so both the indicators and
options routers (and future consumers like the portfolio router) can
reuse the same materialisation path without circular imports.

Public API
----------
* ``materialise_option_streams``  -- bulk materialiser (N labels)
* ``_materialise_option_stream``  -- single-label convenience wrapper
* ``_business_dates_in_range``    -- CME business-day enumeration
* ``PRICE_LIKE_STREAMS``          -- streams representing price-like values
* ``LEVEL_STREAMS``               -- streams representing level / greek values
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas_market_calendars as mcal

from tcg.core.api._models import OptionStreamRef
from tcg.core.api._options_wiring import build_stream_resolver_wiring
from tcg.data._utils import date_to_int
from tcg.data.protocols import MarketDataService
from tcg.engine.options.series.stream_resolver import resolve_option_stream


# ---------------------------------------------------------------------------
# Stream classification constants
# ---------------------------------------------------------------------------

PRICE_LIKE_STREAMS: frozenset[str] = frozenset({"mid"})
"""Streams whose values are denominated in the same units as the underlying
price -- i.e. option premium (mid mark).  Portfolio legs using these streams
produce returns that can be weighted alongside spot / continuous legs."""

LEVEL_STREAMS: frozenset[str] = frozenset(
    {
        "iv",
        "delta",
        "gamma",
        "vega",
        "theta",
        "open_interest",
        "volume",
    }
)
"""Streams whose values are dimensionless levels or greeks -- not directly
comparable to price returns.  Useful as indicator inputs but not as
standalone portfolio legs without explicit conversion logic."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
) -> dict[str, tuple[np.ndarray, np.ndarray, list[str | None]]] | str:
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
    A dict ``{label: (dates_arr, values, diagnostics)}`` on success,
    or a string error message when the date range is missing.
    """
    # Lazy import to avoid circular dependency with options.py which
    # defines these converters and also imports from this module.
    from tcg.core.api.options import (
        _criterion_pydantic_to_dataclass,
        _maturity_pydantic_to_dataclass,
    )

    trade_dates = _business_dates_in_range(start_date, end_date)
    if not trade_dates:
        return "option_stream requires explicit ISO 'start' and 'end' dates"

    chain_reader, mat_resolver, ul_resolver, bulk_reader = build_stream_resolver_wiring(
        svc
    )

    results: dict[str, tuple[np.ndarray, np.ndarray, list[str | None]]] = {}
    for label, ref in refs_with_labels:
        # Pre-fetch available expirations filtered by the requested type
        # and cycle.  The unfiltered variant returned expirations for ALL
        # types / cycles, causing the bulk resolver to pick expirations
        # that had no matching contracts -- empty chains -> spurious NaN
        # holes.
        all_expirations = await svc.list_option_expirations_filtered(
            ref.collection,
            option_type=ref.option_type,
            cycle=ref.cycle,
        )
        values, diagnostics = await resolve_option_stream(
            dates=trade_dates,
            collection=ref.collection,
            option_type=ref.option_type,
            cycle=ref.cycle,
            maturity=_maturity_pydantic_to_dataclass(ref.maturity),
            selection=_criterion_pydantic_to_dataclass(ref.selection),
            stream=ref.stream,
            chain_reader=chain_reader,
            maturity_resolver=mat_resolver,
            underlying_price_resolver=ul_resolver,
            progress_callback=progress_callback,
            bulk_chain_reader=bulk_reader,
            available_expirations=all_expirations,
        )
        dates_arr = np.array([date_to_int(d) for d in trade_dates], dtype=np.int64)
        results[label] = (dates_arr, values, diagnostics)

    return results


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
    return result["_single"]
