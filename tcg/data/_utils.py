"""Shared utilities for the data module."""

from __future__ import annotations

from datetime import date

import numpy as np

from tcg.types.market import PriceSeries


def date_to_int(d: date) -> int:
    """Convert a date to YYYYMMDD integer."""
    return d.year * 10000 + d.month * 100 + d.day


def filter_date_range(
    series: PriceSeries,
    start: date | None,
    end: date | None,
) -> PriceSeries | None:
    """Slice a ``PriceSeries`` to the given date range.

    Dates in the series are YYYYMMDD integers.
    Returns None if the filtered result is empty.
    """
    mask = np.ones(len(series), dtype=bool)

    if start is not None:
        mask &= series.dates >= date_to_int(start)

    if end is not None:
        mask &= series.dates <= date_to_int(end)

    if not mask.any():
        return None

    return PriceSeries(
        dates=series.dates[mask],
        open=series.open[mask],
        high=series.high[mask],
        low=series.low[mask],
        close=series.close[mask],
        volume=series.volume[mask],
    )
