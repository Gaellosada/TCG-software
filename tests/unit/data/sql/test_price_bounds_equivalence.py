"""Cache-status latency fix — instrument price BOUNDS equivalence guard.

The portfolio cache-status label needs each leg's resolved ``start``/``end`` to
key the SAME backend cache entry Compute wrote. Wave 1 showed the frontend was
hydrating the WHOLE OHLCV history per leg just to read ``dates[0]``/``dates[-1]``
(a cold N×M dwh fan-out → "checking… for minutes"). The fix reads a CHEAP
min/max-``trade_date`` bounds aggregate instead.

Correctness is non-negotiable: the bounds MUST equal the endpoints the full read
produces, or the label lies. ``read_prices`` returns EVERY fact row in the
(collection, symbol) window with NO row filtering, so its first/last date are
exactly MIN/MAX ``trade_date`` over that identical row set. These tests prove:

  * ``read_price_bounds`` emits an aggregate over the SAME predicate as
    ``read_prices`` (same join + ``source_collection``/``symbol`` filter +
    ``trade_date`` clamp), so it scans the identical row set;
  * over a shared in-memory model of that row set, ``read_price_bounds`` returns
    exactly ``(read_prices.dates[0], read_prices.dates[-1])`` — regardless of
    row insertion order and for the empty case — through the REAL reader code on
    both sides (no live DB; parity on a live warehouse is covered separately by
    the out-of-tree parity harness / the ephemeral-pg equivalence suite);
  * ``DefaultMarketDataService.get_price_bounds`` mirrors ``get_prices``'
    unknown-collection guard and otherwise returns the reader's bounds.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from tcg.data._sql.instruments import SqlInstrumentReader, _MAX_DATE, _MIN_DATE
from tcg.data._utils import date_to_int
from tcg.data.service import DefaultMarketDataService
from tcg.types.errors import DataNotFoundError


# --------------------------------------------------------------------------- #
# In-memory model of one instrument's fact rows, served to BOTH real readers.
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, rows: list[date]) -> None:
        self._rows = rows  # the seed: this instrument's trade_dates
        self._last: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        if "MIN(" in sql or "min(" in sql:
            # Bounds aggregate. Model MIN/MAX over the SAME row set — order
            # independent, exactly as a real engine computes it.
            if not self._rows:
                self._last = [{"min_date": None, "max_date": None}]
            else:
                self._last = [
                    {"min_date": min(self._rows), "max_date": max(self._rows)}
                ]
        else:
            # Full read: the real query has ``ORDER BY f.trade_date``, so the
            # engine returns rows ascending — model that here.
            self._last = [
                {
                    "trade_date": d,
                    "close_val": 1.0,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "volume": 0.0,
                }
                for d in sorted(self._rows)
            ]

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._last

    async def fetchone(self) -> dict[str, Any] | None:
        return self._last[0] if self._last else None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def connection(self):
        return _FakeConn(self._cursor)


def _reader(rows: list[date]) -> tuple[SqlInstrumentReader, _FakeCursor]:
    cur = _FakeCursor(rows)
    return SqlInstrumentReader(_FakePool(cur)), cur


# --------------------------------------------------------------------------- #
# 1. Reader-level equivalence: bounds == full-series endpoints.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bounds_equal_full_series_endpoints_out_of_order():
    """Bounds == (dates[0], dates[-1]) even when facts land out of order."""
    # Deliberately unsorted seed — proves the bounds path does not lean on the
    # full read's ORDER BY (it aggregates independently).
    seed = [
        date(2020, 6, 19),
        date(2018, 1, 2),
        date(2021, 12, 31),
        date(2019, 3, 15),
    ]
    reader, _ = _reader(seed)

    series = await reader.read_prices("INDEX", "SPX")
    lo, hi = await reader.read_price_bounds("INDEX", "SPX")

    assert series is not None
    assert (lo, hi) == (int(series.dates[0]), int(series.dates[-1]))
    assert (lo, hi) == (20180102, 20211231)


@pytest.mark.asyncio
async def test_bounds_empty_instrument_matches_none_series():
    """No bars → full read is None and bounds is (None, None) — they agree."""
    reader, _ = _reader([])

    series = await reader.read_prices("INDEX", "SPX")
    lo, hi = await reader.read_price_bounds("INDEX", "SPX")

    assert series is None
    assert (lo, hi) == (None, None)


@pytest.mark.asyncio
async def test_bounds_single_bar_collapses_start_eq_end():
    """A one-bar instrument: start == end == that single date."""
    only = date(2022, 5, 4)
    reader, _ = _reader([only])

    series = await reader.read_prices("INDEX", "SPX")
    lo, hi = await reader.read_price_bounds("INDEX", "SPX")

    assert lo == hi == date_to_int(only)
    assert (lo, hi) == (int(series.dates[0]), int(series.dates[-1]))


# --------------------------------------------------------------------------- #
# 2. Predicate parity: bounds scans the SAME row set as the full read.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bounds_query_shares_full_read_predicate():
    """Same join + collection/symbol filter + trade_date clamp, as an aggregate."""
    reader, cur = _reader([date(2020, 1, 1)])

    await reader.read_prices("INDEX", "SPX")
    full_sql, full_params = cur.calls[-1]
    await reader.read_price_bounds("INDEX", "SPX")
    bounds_sql, bounds_params = cur.calls[-1]

    lowered = bounds_sql.lower()
    # Aggregate over trade_date (the cheap replacement for hydrating the series).
    assert "min(f.trade_date)" in lowered
    assert "max(f.trade_date)" in lowered
    # SAME row set: same tables, same join key, same identity predicate.
    assert "fact_price_eod" in bounds_sql and "dim_instrument" in bounds_sql
    assert "d.instrument_id = f.instrument_id" in bounds_sql
    assert "d.source_collection = %s" in bounds_sql
    assert "d.symbol = %s" in bounds_sql
    # Same trade_date clamp the full read uses, so the scanned window is identical.
    assert "f.trade_date between %s and %s" in lowered
    assert bounds_params == ("INDEX", "SPX", _MIN_DATE, _MAX_DATE)
    # The full read binds the same 4 params in the same order (defaults clamp).
    assert full_params == ("INDEX", "SPX", _MIN_DATE, _MAX_DATE)


# --------------------------------------------------------------------------- #
# 3. Service wiring: get_price_bounds mirrors get_prices' collection guard.
# --------------------------------------------------------------------------- #
class _StubReader:
    def __init__(self, exists: bool, bounds: tuple[int | None, int | None]) -> None:
        self._exists = exists
        self._bounds = bounds
        self.seen: tuple | None = None

    async def collection_exists(self, collection: str) -> bool:
        return self._exists

    async def read_price_bounds(
        self, collection: str, instrument_id: str, *, provider: str | None = None
    ) -> tuple[int | None, int | None]:
        self.seen = (collection, instrument_id, provider)
        return self._bounds


def _service_with(stub: _StubReader) -> DefaultMarketDataService:
    svc = DefaultMarketDataService.__new__(DefaultMarketDataService)
    svc._sql = stub  # type: ignore[attr-defined]
    return svc


@pytest.mark.asyncio
async def test_service_get_price_bounds_returns_reader_bounds():
    stub = _StubReader(exists=True, bounds=(20180102, 20211231))
    svc = _service_with(stub)

    assert await svc.get_price_bounds("INDEX", "SPX") == (20180102, 20211231)
    assert stub.seen == ("INDEX", "SPX", None)


@pytest.mark.asyncio
async def test_service_get_price_bounds_unknown_collection_raises():
    stub = _StubReader(exists=False, bounds=(None, None))
    svc = _service_with(stub)

    with pytest.raises(DataNotFoundError):
        await svc.get_price_bounds("NOPE", "SPX")
