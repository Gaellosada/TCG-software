"""Unit tests for the EXACT cycle trade-date-span reader (v1 + v2).

``cycle_trade_date_span`` returns ``(first, last)`` settlement ``trade_date`` for
ONE ``expiration_cycle``. It is an EXACT ``min/max`` (identical to
``min``/``max`` of ``list_expirations_by_date``'s keys) computed WITHOUT
materialising every settlement bar:

  * v1 replaces the partition-wide hash join over ALL cycle ``instrument_id``s
    (which seq-scanned every ``fact_price_eod`` partition) with a per-contract
    ``LATERAL`` PK-index ``min/max`` whose extremes are aggregated — byte-
    identical, index-only, and inherently robust to phantom dim contracts
    (a listed-but-never-traded contract yields ``NULL``, dropped by the outer
    aggregate; the reason a single "representative contract" is NOT safe);
  * v2 keeps a two-value aggregate over the cycle's routed objects (NO contract
    join, NO per-date map);
  * both accept OPTIONAL ``start``/``end`` bounds and return ``(None, None)`` on
    an all-``NULL`` (empty) aggregate.

A live-DB byte-identity + timing proof (new EXACT reader == old full-scan
extremes, all 26 real portfolios' params) is captured in the task output and in
``tests/integration/data/options/test_coverage_heuristic_equivalence.py`` —
these unit tests lock the SQL SHAPE so a regression goes RED offline.
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


async def test_v1_span_is_lateral_per_contract_min_max_with_cycle():
    cur = _FakeCursor({"lo": date(2016, 2, 22), "hi": date(2026, 6, 30)})
    reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]

    first, last = await reader.cycle_trade_date_span(
        "OPT_SP_500", date(2011, 6, 15), date(2026, 7, 21), cycle="M"
    )
    assert (first, last) == (date(2016, 2, 22), date(2026, 6, 30))

    sql, params = cur.calls[0]
    # A per-contract LATERAL PK min/max, aggregated — NOT a hash join over all
    # cycle bars, NOT a DISTINCT per-date map.
    assert "LATERAL" in sql
    assert "min(m.lo)" in sql and "max(m.hi)" in sql
    assert "min(f.trade_date)" in sql and "max(f.trade_date)" in sql
    assert "f.instrument_id = i.instrument_id" in sql
    assert "DISTINCT" not in sql
    # Cycle predicate pushed to the dim CTE; bounds present when given.
    assert "expiration_cycle = %s" in sql
    assert "expiration >= %s" in sql
    assert "f.trade_date >= %s" in sql and "f.trade_date <= %s" in sql
    # root, expiration>=start (dim), cycle, trade_date>=start, trade_date<=end.
    assert params == [
        "OPT_SP_500",
        date(2011, 6, 15),
        "M",
        date(2011, 6, 15),
        date(2026, 7, 21),
    ]


async def test_v1_span_unbounded_omits_expiration_and_trade_date_bounds():
    cur = _FakeCursor({"lo": date(2009, 12, 15), "hi": date(2026, 6, 12)})
    reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]

    first, last = await reader.cycle_trade_date_span(
        "OPT_SP_500", cycle="W1 Friday"
    )
    assert (first, last) == (date(2009, 12, 15), date(2026, 6, 12))

    sql, params = cur.calls[0]
    assert "LATERAL" in sql
    assert "expiration >= %s" not in sql
    assert "trade_date >=" not in sql and "trade_date <=" not in sql
    # Only the root and the cycle bind survive.
    assert params == ["OPT_SP_500", "W1 Friday"]


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
    # The whole point: NO contract join (the old per-date map's third table).
    assert "contract" not in sql.lower()
    assert "sv.object_id = ANY(%s)" in sql

    objects, ts_lo, ts_hi = captured["params"]
    # SAME routing the per-date map used for this cycle.
    assert objects == _route_objects("W3 Friday", require_filter=False)
    # Half-open [start, end+1day) UTC window (date_int_bounds).
    assert ts_lo == datetime(2011, 6, 15, tzinfo=timezone.utc)
    assert ts_hi == datetime(2026, 7, 22, tzinfo=timezone.utc)


async def test_v2_span_unbounded_omits_ts_window():
    lo = datetime(2010, 6, 7, tzinfo=timezone.utc)
    hi = datetime(2026, 7, 31, tzinfo=timezone.utc)
    reader, captured = _v2_reader_capturing([{"lo": lo, "hi": hi}])

    first, last = await reader.cycle_trade_date_span(
        "OPT_SP_500", cycle="W3 Friday"
    )
    assert (first, last) == (date(2010, 6, 7), date(2026, 7, 31))
    assert "fv.ts >=" not in captured["sql"] and "fv.ts <" not in captured["sql"]
    # Only the routed-objects bind survives.
    assert captured["params"] == [_route_objects("W3 Friday", require_filter=False)]


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
