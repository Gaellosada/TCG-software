"""Unit tests for the bounded cycle trade-date-span reader (v1 + v2).

``cycle_trade_date_span`` replaces the old ``min/max`` over
``list_expirations_by_date``'s keys on the cycle-scoped coverage path.  The old
path materialised EVERY settlement bar of the cycle (a 3-table
``serie ⋈ fact_value ⋈ contract`` DISTINCT scan — ~14M rows for W3 across ~16
years) only to keep the two extremes.  These tests pin the cheap replacement:

  * a two-value ``min/max`` aggregate (no ``DISTINCT``, no per-date map);
  * v2 issues NO ``contract`` join and routes the SAME objects the map used
    (``_route_objects``), bounded to the SAME half-open ``ts`` window;
  * v1 keeps the constant ``trade_date BETWEEN`` partition-pruning join and the
    same cycle predicate, but selects ``min/max`` not a DISTINCT column;
  * both map the aggregate row back to the ``(first, last)`` tuple and return
    ``(None, None)`` on an all-``NULL`` (empty) aggregate row.

A live-DB byte-identity + timing proof (new ``min/max`` == old map extremes,
old-vs-new wall clock) is captured separately in the task output — these unit
tests lock the SQL shape so a regression goes RED offline.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

import pytest

from tcg.data._sql.options import SqlOptionsDataReader
from tcg.data._v2_compat.options_reader import V2OptionsDataReader, _route_objects
from tcg.types.errors import OptionsDataAccessError


# --------------------------------------------------------------------------- #
# v1 — fake async pool / connection / cursor
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


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


async def test_v1_span_is_bounded_min_max_aggregate_with_cycle():
    cur = _FakeCursor({"lo": date(2016, 2, 22), "hi": date(2026, 6, 30)})
    reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]

    first, last = await reader.cycle_trade_date_span(
        "OPT_SP_500", date(2011, 6, 15), date(2026, 7, 21), cycle="M"
    )
    assert (first, last) == (date(2016, 2, 22), date(2026, 6, 30))

    sql, params = cur.calls[0]
    # A bounded min/max aggregate — NOT a DISTINCT per-date map.
    assert "min(p.trade_date)" in sql
    assert "max(p.trade_date)" in sql
    assert "DISTINCT" not in sql
    # Constant partition-pruning window + cycle predicate pushed to the dim.
    assert "trade_date BETWEEN %s AND %s" in sql
    assert "expiration_cycle = %s" in sql
    # start bound (2x: expiration>=start and BETWEEN start), cycle, end bound.
    assert params == [
        "OPT_SP_500",
        date(2011, 6, 15),  # expiration >= start
        "M",  # cycle bind
        date(2011, 6, 15),  # BETWEEN start
        date(2026, 7, 21),  # BETWEEN end
    ]


async def test_v1_span_empty_aggregate_row_is_none_none():
    cur = _FakeCursor({"lo": None, "hi": None})
    reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]
    assert await reader.cycle_trade_date_span(
        "OPT_SP_500", date(2011, 6, 15), date(2026, 7, 21), cycle="W3 Friday"
    ) == (None, None)


async def test_v1_span_wraps_transport_error():
    class _Boom:
        def connection(self):
            raise RuntimeError("dwh down")

    reader = SqlOptionsDataReader(_Boom())  # type: ignore[arg-type]
    with pytest.raises(OptionsDataAccessError):
        await reader.cycle_trade_date_span(
            "OPT_SP_500", date(2011, 6, 15), date(2026, 7, 21), cycle="M"
        )


# --------------------------------------------------------------------------- #
# v2 — stub ``_fetch`` to capture the emitted SQL + binds
# --------------------------------------------------------------------------- #
def _v2_reader_capturing(rows: list[dict[str, Any]]):
    reader = V2OptionsDataReader(pool=None)  # type: ignore[arg-type]
    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, params: Sequence[Any], *, what: str):
        captured["sql"] = sql
        captured["params"] = list(params)
        captured["what"] = what
        return rows

    reader._fetch = _fake_fetch  # type: ignore[assignment]
    return reader, captured


async def test_v2_span_no_contract_join_routes_cycle_objects_and_window():
    lo = datetime(2016, 2, 22, tzinfo=timezone.utc)
    hi = datetime(2026, 6, 30, tzinfo=timezone.utc)
    reader, captured = _v2_reader_capturing([{"lo": lo, "hi": hi}])

    first, last = await reader.cycle_trade_date_span(
        "OPT_SP_500", date(2011, 6, 15), date(2026, 7, 21), cycle="W3 Friday"
    )
    # timestamptz → UTC date.
    assert (first, last) == (date(2016, 2, 22), date(2026, 6, 30))

    sql = captured["sql"]
    assert "min(fv.ts)" in sql and "max(fv.ts)" in sql
    assert "DISTINCT" not in sql
    # The whole point: NO contract join (the old map's third table).
    assert "contract" not in sql.lower()
    assert "sv.object_id = ANY(%s)" in sql

    objects, ts_lo, ts_hi = captured["params"]
    # SAME routing the per-date map used for this cycle.
    assert objects == _route_objects("W3 Friday", require_filter=False)
    # Half-open [start, end+1day) UTC window (date_int_bounds).
    assert ts_lo == datetime(2011, 6, 15, tzinfo=timezone.utc)
    assert ts_hi == datetime(2026, 7, 22, tzinfo=timezone.utc)


async def test_v2_span_empty_result_is_none_none():
    reader, _ = _v2_reader_capturing([])
    assert await reader.cycle_trade_date_span(
        "OPT_SP_500", date(2011, 6, 15), date(2026, 7, 21), cycle="W3 Friday"
    ) == (None, None)


async def test_v2_span_null_aggregate_is_none_none():
    reader, _ = _v2_reader_capturing([{"lo": None, "hi": None}])
    assert await reader.cycle_trade_date_span(
        "OPT_SP_500", date(2011, 6, 15), date(2026, 7, 21), cycle="W3 Friday"
    ) == (None, None)
