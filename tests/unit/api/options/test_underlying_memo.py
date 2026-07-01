"""Perf fix (c): per-resolve underlying memoization must be RESULT-INVARIANT.

The option-stream resolver's ByMoneyness/ByDelta Phase C resolves the underlying
future PER TRADE DATE (find_front_contract_on_or_after + single-date get_prices),
~97% redundant (all dates of an expiration share ONE front-quarterly future).  The
fix memoizes within one resolve: each distinct future's closes are fetched ONCE over
the window (mirroring _batch_underlying_prices) and served by date.

These tests pin BOTH invariants:
  1. VALUE-IDENTITY: the memoized adapter returns the SAME close for every date as a
     naive per-date adapter (no fabrication, no drift).
  2. FEWER ROUND-TRIPS: the memoized adapter issues far fewer get_prices calls (one
     ranged fetch per distinct future, not one single-date fetch per date).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from tcg.core.api._options_wiring import _FuturesDataPortAdapter
from tcg.types.market import PriceSeries


def _fut_series(start: date, end: date, base: float) -> PriceSeries:
    """A daily FUT close series over [start, end] (weekdays), close = base + day idx."""
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    dates = np.array(
        [x.year * 10000 + x.month * 100 + x.day for x in days], dtype=np.int64
    )
    close = np.array([base + i for i in range(len(days))], dtype=np.float64)
    n = len(days)
    return PriceSeries(
        dates=dates,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=np.full(n, 1.0, dtype=np.float64),
    )


class _CountingSvc:
    """Fake MarketDataService: counts get_prices calls; resolves the front future by
    a fixed expiration→FUT map; returns a ranged close series (respecting start/end)."""

    def __init__(self, fut_id: str, series: PriceSeries) -> None:
        self._fut_id = fut_id
        self._series = series
        self.get_prices_calls = 0
        self.find_front_calls = 0

    async def find_front_futures_contract_on_or_after(self, collection, expiration_int):
        self.find_front_calls += 1
        return self._fut_id

    async def find_futures_contract_by_expiration(self, collection, expiration_int):
        return self._fut_id

    async def get_prices(self, collection, instrument_id, start=None, end=None):
        self.get_prices_calls += 1
        s = self._series
        if start is None and end is None:
            return s
        lo = start.year * 10000 + start.month * 100 + start.day if start else -1
        hi = end.year * 10000 + end.month * 100 + end.day if end else 10**9
        mask = (s.dates >= lo) & (s.dates <= hi)
        return PriceSeries(
            dates=s.dates[mask],
            open=s.open[mask],
            high=s.high[mask],
            low=s.low[mask],
            close=s.close[mask],
            volume=s.volume[mask],
        )


_WINDOW = (date(2024, 1, 1), date(2024, 3, 31))
_TRADE_DATES = [
    d
    for d in (date(2024, 1, 1) + timedelta(days=i) for i in range(0, 90))
    if d.weekday() < 5
]


async def test_memoized_underlying_matches_per_date_values():
    """VALUE-IDENTITY: memoized adapter == naive per-date adapter, for every date."""
    fut_id = "FUT_SP_500_EMINI_20240315"
    series = _fut_series(_WINDOW[0], _WINDOW[1], base=5000.0)

    naive_svc = _CountingSvc(fut_id, series)
    memo_svc = _CountingSvc(fut_id, series)

    naive = _FuturesDataPortAdapter(naive_svc)  # no prefetch window → per-date
    memo = _FuturesDataPortAdapter(memo_svc, prefetch_window=_WINDOW)

    exp = date(2024, 3, 15)
    for d in _TRADE_DATES:
        v_naive = await naive.get_futures_close_on_or_after_expiration(
            "FUT_SP_500", exp, d
        )
        v_memo = await memo.get_futures_close_on_or_after_expiration(
            "FUT_SP_500", exp, d
        )
        assert v_naive == v_memo, f"value drift on {d}: naive={v_naive} memo={v_memo}"

    # FEWER ROUND-TRIPS: naive does one get_prices per date; memo does ONE (ranged).
    assert naive_svc.get_prices_calls == len(_TRADE_DATES)
    assert memo_svc.get_prices_calls == 1, (
        f"memo issued {memo_svc.get_prices_calls} get_prices (expected 1 ranged fetch)"
    )
    # Front-FUT id resolution is also memoized (per collection+expiration).
    assert memo_svc.find_front_calls == 1


async def test_memo_missing_date_matches_naive_none():
    """A date with no FUT bar → None in BOTH (memo must not fabricate)."""
    fut_id = "FUT_SP_500_EMINI_20240315"
    series = _fut_series(_WINDOW[0], _WINDOW[1], base=5000.0)
    naive = _FuturesDataPortAdapter(_CountingSvc(fut_id, series))
    memo = _FuturesDataPortAdapter(
        _CountingSvc(fut_id, series), prefetch_window=_WINDOW
    )

    gap = date(2024, 2, 17)  # a Saturday — no bar
    exp = date(2024, 3, 15)
    assert (
        await naive.get_futures_close_on_or_after_expiration("FUT_SP_500", exp, gap)
        is None
    )
    assert (
        await memo.get_futures_close_on_or_after_expiration("FUT_SP_500", exp, gap)
        is None
    )


async def test_memo_by_expiration_also_cached():
    """The exact-match VIX path (get_futures_close_by_expiration) shares the cache."""
    fut_id = "FUT_VIX_20240320"
    series = _fut_series(_WINDOW[0], _WINDOW[1], base=15.0)
    memo_svc = _CountingSvc(fut_id, series)
    memo = _FuturesDataPortAdapter(memo_svc, prefetch_window=_WINDOW)

    exp = date(2024, 3, 20)
    for d in _TRADE_DATES[:10]:
        await memo.get_futures_close_by_expiration("FUT_VIX", exp, d)
    assert memo_svc.get_prices_calls == 1
