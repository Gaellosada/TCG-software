"""Unit tests for ``SqlInstrumentReaderV2.fetch_object_facets`` (no live DB).

``fetch_object_facets`` is the cheap aggregate the Database v2 filter form is
built from. Two properties matter and neither is observable from the service
tests (which fake the reader out entirely and can only re-read their own
literals):

  1. **It must never touch a fact table.** The whole point is that populating the
     filter form costs three grouped dimension reads (0.33 s + 0.37 s on object
     12) instead of the 34 s / 38 MB full series dump it replaces. A join onto
     ``fact_bar`` or friends would still return the right numbers, so only an
     assertion over the emitted SQL catches it.
  2. **The row shaping is real work**: ``date -> isoformat``, ``Decimal ->
     float`` at this boundary, distinct option types sorted, and the series total
     summed across the ``(type, freq)`` groups.

These tests drive the REAL method through a fake pool/cursor that serves a
different result set per ``execute``, in order.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tcg.data._sql.instruments_v2 import SqlInstrumentReaderV2
from tcg.types.errors import DataAccessError

# --------------------------------------------------------------------------- #
# Fake async pool / connection / cursor.
#
# Same shape as the sibling SQL reader tests in this directory
# (``test_sql_options_bulk.py``, ``test_sql_instruments_v2_grain.py``):
# ``pool.connection()`` -> ``conn.cursor()`` -> ``execute`` -> ``fetchall`` /
# ``fetchone``, every level an async context manager. Defined locally rather than
# shared because the ``tests/`` tree has no ``__init__.py``, so a test module
# cannot import from a sibling or from ``conftest``.
#
# Unlike those siblings this cursor serves a QUEUE of result sets: the method
# under test issues three statements on one cursor, and a cursor that replayed
# the same rows for all three could not tell the reads apart.
# --------------------------------------------------------------------------- #


class _FakeCursor:
    """Serves one canned result set per ``execute``; records SQL + params."""

    def __init__(self, result_sets: list[Any]) -> None:
        self._result_sets = list(result_sets)
        self._current: Any = None
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        if not self._result_sets:
            raise AssertionError(
                f"unexpected {len(self.calls)}th statement, no result set left: {sql}"
            )
        self._current = self._result_sets.pop(0)

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._current or [])

    async def fetchone(self) -> dict[str, Any] | None:
        return self._current


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
    """Minimal stand-in for ``DwhConnectionPool`` exposing ``connection()``."""

    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def connection(self):
        return _FakeConn(self._cursor)


def _mk(result_sets: list[Any]) -> tuple[SqlInstrumentReaderV2, _FakeCursor]:
    """A real reader whose pool serves *result_sets*, one per statement."""
    cur = _FakeCursor(result_sets)
    return SqlInstrumentReaderV2(_FakePool(cur)), cur  # type: ignore[arg-type]


#: An option root: two expirations, Decimal strikes, option types out of order,
#: and two (type, freq) series groups whose counts differ from every other number
#: in the fixture so a wrong reduction cannot coincide with the right one.
_OPTION_EXPIRATIONS = [
    {"expiration": date(2026, 9, 11), "contracts": 146},
    {"expiration": date(2026, 9, 4), "contracts": 140},
]
_OPTION_AGG = {
    "strike_min": Decimal("15.0"),
    "strike_max": Decimal("10600.0"),
    "contracts": 96106,
    "option_types": ["put", "call"],
}
_OPTION_SERIE_TYPES = [
    {"type": "bar", "freq": "1m", "series": 96106},
    {"type": "value", "freq": "daily", "series": 8},
]


async def test_facets_never_reads_a_fact_table():
    """The cheapness guarantee: dimension tables only.

    Aggregating over a join onto ``fact_bar`` would return identical counts while
    turning a sub-second form fetch back into a fact-table scan, so the SQL text
    is the only thing that discriminates.
    """
    reader, cur = _mk([_OPTION_EXPIRATIONS, _OPTION_AGG, _OPTION_SERIE_TYPES])
    await reader.fetch_object_facets(12)

    assert len(cur.calls) == 3, "expected exactly three grouped dimension reads"
    for sql, params in cur.calls:
        assert "fact_" not in sql, f"facets query touches a fact table: {sql}"
        assert params == (12,), "every read must be parameterized on object_id"
    tables = [sql for sql, _ in cur.calls]
    assert "tcg_instruments_v2.contract" in tables[0]
    assert "tcg_instruments_v2.contract" in tables[1]
    assert "tcg_instruments_v2.serie" in tables[2]


async def test_facets_shapes_rows_for_the_filter_form():
    reader, _ = _mk([_OPTION_EXPIRATIONS, _OPTION_AGG, _OPTION_SERIE_TYPES])
    out = await reader.fetch_object_facets(12)

    # Dates are JSON-ready ISO strings, in the order the query returned them.
    assert out["expirations"] == [
        {"expiration": "2026-09-11", "contracts": 146},
        {"expiration": "2026-09-04", "contracts": 140},
    ]
    # Decimal -> float at this boundary (a Decimal is not JSON-serializable and
    # poisons NumPy downstream).
    assert out["strike_min"] == 15.0
    assert out["strike_max"] == 10600.0
    assert type(out["strike_min"]) is float
    assert type(out["strike_max"]) is float
    # ARRAY_AGG(DISTINCT ...) ordering is not contractual; the form needs stable
    # option types, so they are sorted here.
    assert out["option_types"] == ["call", "put"]
    assert out["serie_types"] == _OPTION_SERIE_TYPES
    # The series total is the SUM over the (type, freq) groups — not the largest
    # group (96106), not the group count (2), not the contract count (96106).
    assert out["totals"] == {"contracts": 96106, "series": 96114}


async def test_facets_for_an_object_without_contracts_is_empty_not_an_error():
    """An index / rate object (e.g. object 5, IND_SP_500) has no contracts.

    Empty ``expirations`` and ``None`` strike bounds are the correct answer, so
    the NULL aggregate row must not raise (``sorted(None)`` would).
    """
    reader, _ = _mk(
        [
            [],
            {
                "strike_min": None,
                "strike_max": None,
                "contracts": 0,
                "option_types": None,
            },
            [{"type": "value", "freq": "daily", "series": 1}],
        ]
    )
    out = await reader.fetch_object_facets(5)
    assert out["expirations"] == []
    assert out["strike_min"] is None
    assert out["strike_max"] is None
    assert out["option_types"] == []
    assert out["totals"] == {"contracts": 0, "series": 1}


async def test_facets_tolerates_a_missing_aggregate_row():
    """Defensive: a ``fetchone()`` of ``None`` must not blow up the aggregate."""
    reader, _ = _mk([[], None, []])
    out = await reader.fetch_object_facets(5)
    assert out["strike_min"] is None
    assert out["option_types"] == []
    assert out["totals"] == {"contracts": 0, "series": 0}


async def test_facets_wraps_driver_errors_as_data_access_error():
    reader, _ = _mk([_OPTION_EXPIRATIONS])  # 2nd execute has no result set left
    with pytest.raises(DataAccessError, match="facets for object 12"):
        await reader.fetch_object_facets(12)
