"""Unit tests for ``DailySeriesReader`` — the P0.3 generic daily-series seam.

Drive the REAL reader through a fake async pool/cursor (no live DB). The dwh is
unreachable from this egress IP (see the task PROBLEMS.md), so green here rests
on SQL-shape + boundary-coercion assertions, NOT on a live warehouse. A sibling
``tests/integration/data/test_daily_series_reader_integration.py`` exercises the
real path once an allowlisted IP is available.

Fake pool/conn/cursor: same shape as the other SQL reader tests in this
directory (``test_sql_instruments_v2_grain.py`` etc.) — ``pool.connection()`` ->
``conn.cursor()`` -> ``execute`` -> ``fetchall``, each an async context manager.
Defined locally because the ``tests/`` tree has no ``__init__.py`` (a module
cannot import from a sibling / ``conftest``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tcg.data._sql.daily_series import (
    ALLOWED_FIELDS,
    DEFAULT_FIELD,
    DailySeriesReader,
)
from tcg.types.daily_series import DailySeries
from tcg.types.errors import DataAccessError


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeCursor:
    """Returns canned rows; records the SQL+params it was handed."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


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
    """Minimal stand-in for ``DwhConnectionPool`` exposing ``connection()``.

    Records EVERY cursor it hands out so a multi-call test can inspect the SQL /
    params of each independent read.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.cursors: list[_FakeCursor] = []

    def connection(self) -> _FakeConn:
        cur = _FakeCursor(list(self._rows))
        self.cursors.append(cur)
        return _FakeConn(cur)


def _mk(rows: list[dict[str, Any]]) -> tuple[DailySeriesReader, _FakePool]:
    pool = _FakePool(rows)
    return DailySeriesReader(pool), pool  # type: ignore[arg-type]


def _norm(sql: str) -> str:
    return " ".join(sql.split())


_ROWS = [
    {"trade_date": date(2025, 1, 2), "value": Decimal("100.5")},
    {"trade_date": date(2025, 1, 3), "value": Decimal("101.25")},
    {"trade_date": date(2025, 1, 6), "value": Decimal("99.0")},
]


# --------------------------------------------------------------------------- #
# SQL shape / params
# --------------------------------------------------------------------------- #
async def test_default_field_is_coalesced_close_and_schema_bound():
    reader, pool = _mk(_ROWS)
    await reader.read_series("IND_VVIX")
    sql, _params = pool.cursors[0].calls[0]
    flat = _norm(sql)
    assert "COALESCE(f.adj_close, f.close) AS value" in flat  # gotcha [3]
    assert "FROM tcg_instruments.fact_price_eod f" in flat  # PARENT + schema-bound
    assert "JOIN tcg_instruments.dim_instrument d" in flat
    assert "d.symbol = %s" in flat  # symbol-only filter (durable id)
    assert "f.trade_date BETWEEN %s AND %s" in flat  # gotcha [2] constant range
    assert "ORDER BY f.trade_date" in flat


async def test_params_pass_symbol_and_inclusive_bounds():
    reader, pool = _mk(_ROWS)
    await reader.read_series(
        "IND_SP_500", start=date(2025, 1, 1), end=date(2025, 6, 30)
    )
    _sql, params = pool.cursors[0].calls[0]
    assert params == ("IND_SP_500", date(2025, 1, 1), date(2025, 6, 30))


async def test_open_ended_range_uses_partition_span_sentinels():
    """Open start/end must clamp to the dwh partition span, not NULL/unbounded."""
    reader, pool = _mk(_ROWS)
    await reader.read_series("IND_VVIX")
    _sql, params = pool.cursors[0].calls[0]
    assert params == ("IND_VVIX", date(1980, 1, 1), date(2050, 12, 31))


# --------------------------------------------------------------------------- #
# Result shape / ordering / coercion
# --------------------------------------------------------------------------- #
async def test_rows_map_to_ordered_points_with_yyyymmdd_dates():
    reader, _pool = _mk(_ROWS)
    series = await reader.read_series("IND_VVIX")
    assert isinstance(series, DailySeries)
    assert series.symbol == "IND_VVIX"
    assert series.field == DEFAULT_FIELD
    assert series.dates == [20250102, 20250103, 20250106]  # order preserved
    assert series.values == [100.5, 101.25, 99.0]
    assert all(isinstance(v, float) for v in series.values)  # Decimal -> float
    assert len(series) == 3


async def test_null_value_rows_are_dropped_at_the_boundary():
    rows = [
        {"trade_date": date(2025, 1, 2), "value": Decimal("100.5")},
        {"trade_date": date(2025, 1, 3), "value": None},  # SQL NULL -> dropped
        {"trade_date": date(2025, 1, 6), "value": 99.0},
    ]
    reader, _pool = _mk(rows)
    series = await reader.read_series("IND_VVIX")
    assert series.dates == [20250102, 20250106]
    assert series.values == [100.5, 99.0]


async def test_empty_range_returns_wellformed_empty_series_not_none():
    reader, _pool = _mk([])
    series = await reader.read_series("IND_VVIX", start=date(2099, 1, 1))
    assert series == DailySeries(symbol="IND_VVIX", field="close", points=())
    assert series.points == ()
    assert series.dates == [] and series.values == []
    assert len(series) == 0


# --------------------------------------------------------------------------- #
# Field selector (injection-proof allow-list)
# --------------------------------------------------------------------------- #
async def test_raw_field_selector_uses_that_column_without_coalesce():
    reader, pool = _mk(_ROWS)
    series = await reader.read_series("IND_SP_500", field="volume")
    sql, _params = pool.cursors[0].calls[0]
    flat = _norm(sql)
    assert "f.volume AS value" in flat
    assert "COALESCE" not in flat  # raw column, no adj_close preference
    assert series.field == "volume"


async def test_unknown_field_raises_before_touching_the_db():
    reader, pool = _mk(_ROWS)
    with pytest.raises(DataAccessError) as ei:
        await reader.read_series("IND_VVIX", field="; DROP TABLE x")
    assert "unknown daily-series field" in str(ei.value)
    # No query was ever emitted — the guard fires before any connection.
    assert pool.cursors == []


@pytest.mark.parametrize("field", sorted(ALLOWED_FIELDS))
async def test_every_allowed_field_is_accepted_and_recorded(field):
    reader, _pool = _mk(_ROWS)
    series = await reader.read_series("IND_VVIX", field=field)
    assert series.field == field


# --------------------------------------------------------------------------- #
# Multi-symbol reuse + VIX1D drop-in
# --------------------------------------------------------------------------- #
async def test_same_reader_serves_multiple_symbols_independently():
    reader, pool = _mk(_ROWS)
    s_vvix = await reader.read_series("IND_VVIX", start=date(2025, 1, 1))
    s_spx = await reader.read_series("IND_SP_500", start=date(2024, 1, 1))
    assert s_vvix.symbol == "IND_VVIX"
    assert s_spx.symbol == "IND_SP_500"
    # Two distinct reads, each with its own symbol/start param.
    assert pool.cursors[0].calls[0][1][0] == "IND_VVIX"
    assert pool.cursors[0].calls[0][1][1] == date(2025, 1, 1)
    assert pool.cursors[1].calls[0][1][0] == "IND_SP_500"
    assert pool.cursors[1].calls[0][1][1] == date(2024, 1, 1)


async def test_vix1d_is_a_pure_symbol_string_drop_in():
    """Proof of the seam's headline property: VIX1D needs NO code change here.

    ``IND_VIX1D`` is not in the dwh today, but the reader treats it identically
    to any other symbol — same SQL, the symbol flows straight to the bound
    ``d.symbol = %s`` param. When the series lands, this exact call returns its
    points with zero edit to reader or types.
    """
    reader, pool = _mk(_ROWS)
    series = await reader.read_series("IND_VIX1D", start=date(2025, 1, 1))
    _sql, params = pool.cursors[0].calls[0]
    assert params[0] == "IND_VIX1D"
    assert series.symbol == "IND_VIX1D"
