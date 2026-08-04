"""Unit tests for ``SqlInstrumentReaderV2.list_series_filtered`` (no live DB).

This is the paginated replacement for the 38 MB / 38 s full-series dump, so two
kinds of property matter and neither is visible from the service tests (which
fake the reader out and can only re-read their own literals):

  1. **Statement structure**, which only an assertion over the emitted SQL can
     see: the join must be a ``LEFT JOIN`` (an ``INNER`` one silently drops the
     ``contract_id IS NULL`` series that index and rate objects consist of); the
     count and the page must share one ``WHERE`` clause (or a filter applies to
     the rows but not the total, and the pager shows phantom pages); the
     ``ORDER BY`` must end on ``s.serie_id``; ``LIMIT``/``OFFSET`` must not be
     transposed; and no fact table may be touched.
  2. **Row shaping**: ``date -> isoformat``, ``Decimal -> float`` at this
     boundary, and NULL contract columns surviving as ``None``.

A fake cursor cannot EXECUTE any of it, so the semantics of the ``WHERE`` /
``ORDER BY`` / ``LIMIT`` are proved against the live warehouse in
``tests/integration/data/test_instruments_v2_integration.py``
(``test_series_page_filters_live``, ``test_series_page_paging_is_stable_live``,
``test_series_page_lists_object_level_series_live``). What is pinned here and
what is pinned there is deliberately disjoint.

Fake pool/cursor is defined locally and matches the sibling readers'
(``test_sql_options_bulk.py``, ``test_sql_instruments_v2_facets.py``): the
``tests/`` tree has no ``__init__.py``, so a test module cannot import from a
sibling.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tcg.data._sql.instruments_v2 import SqlInstrumentReaderV2
from tcg.types.errors import DataAccessError


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


#: An option serie joined to its contract (Decimal strike, real ``date``) and an
#: object-level serie with no contract at all — the LEFT JOIN's reason to exist.
_OPTION_ROW = {
    "serie_id": 1433194,
    "contract_id": 77,
    "type": "bbba",
    "freq": "1m",
    "source": "DATABENTO:GLBX.MDP3:bbo-1m",
    "contract_code": "EW2H6 P6260.20260313",
    "expiration": date(2026, 3, 13),
    "strike": Decimal("6260.0"),
    "option_type": "put",
}
_OBJECT_LEVEL_ROW = {
    "serie_id": 4,
    "contract_id": None,
    "type": "bar",
    "freq": "daily",
    "source": "DATABENTO",
    "contract_code": None,
    "expiration": None,
    "strike": None,
    "option_type": None,
}


def _where_of(sql: str) -> str:
    """The WHERE clause, normalized — up to ORDER BY / end of statement."""
    body = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    return re.sub(r"\s+", " ", body).strip()


# --------------------------------------------------------------------------- #
# Statement structure
# --------------------------------------------------------------------------- #
async def test_page_issues_a_count_then_a_page_over_dimensions_only():
    reader, cur = _mk([{"total": 195}, [_OPTION_ROW]])
    items, total = await reader.list_series_filtered(12, serie_type="bbba")

    assert total == 195
    assert len(items) == 1
    assert len(cur.calls) == 2, "expected exactly one COUNT and one page read"
    count_sql, page_sql = cur.calls[0][0], cur.calls[1][0]
    assert "COUNT(*)" in count_sql
    assert "LIMIT" not in count_sql, "the COUNT must not be paginated"
    assert "s.serie_id" in page_sql and "COUNT(" not in page_sql
    for sql, _ in cur.calls:
        # Both reads are dimension-only. Joining a fact table would return the
        # same numbers while turning a sub-second page into a fact scan — the
        # very regression this endpoint exists to prevent.
        assert "fact_" not in sql, f"series page touches a fact table: {sql}"


async def test_page_join_is_left_not_inner():
    """An INNER join would silently drop every ``contract_id IS NULL`` serie.

    That is the entire series list of an index or rate object, so the bug shows
    up as "this object has no data" rather than as an error.
    """
    reader, cur = _mk([{"total": 1}, [_OBJECT_LEVEL_ROW]])
    await reader.list_series_filtered(5)
    for sql, _ in cur.calls:
        joins = re.findall(r"\b(LEFT JOIN|INNER JOIN|(?<!LEFT )\bJOIN)\b", sql)
        assert joins == ["LEFT JOIN"], (
            f"expected a single LEFT JOIN, got {joins}: {sql}"
        )


async def test_count_and_page_share_one_where_clause():
    """A filter must narrow the total as well as the rows.

    Applied to only one of the two, the pager offers pages that return nothing
    (total too large) or hides rows (total too small). Comparing the two clause
    strings is what catches it; comparing each against a literal would not, since
    a drifted pair could still each match their own expected literal.
    """
    reader, cur = _mk([{"total": 3}, [_OPTION_ROW]])
    await reader.list_series_filtered(
        12,
        expiration_min=date(2026, 3, 1),
        expiration_max=date(2026, 3, 31),
        strike_min=6000.0,
        strike_max=7000.0,
        option_type="put",
        serie_type="bbba",
        freq="1m",
    )
    count_where = _where_of(cur.calls[0][0])
    page_where = _where_of(cur.calls[1][0])
    assert count_where == page_where
    # And it really is the full conjunction, on the right table aliases: `s` for
    # serie columns, `c` for contract columns. Rebinding one to the other table
    # (or to the wrong bound) is the mutation this pins.
    assert count_where == (
        "s.object_id = %s AND s.type = %s AND s.freq = %s "
        "AND c.option_type = %s AND c.expiration >= %s AND c.expiration <= %s "
        "AND c.strike >= %s AND c.strike <= %s"
    )


async def test_page_orders_on_a_total_key_ending_in_serie_id():
    """``serie_id`` must be the LAST ORDER BY key, not merely present somewhere.

    Under a non-total order LIMIT/OFFSET may repeat or skip rows between pages,
    and object 12 really does carry duplicate leading keys (two series per
    contract: ``bar:1m`` and ``bbba:1m``). Leading with ``serie_id`` instead
    would be total but would scramble the chain into id order, so the sequence is
    pinned, not the membership.
    """
    reader, cur = _mk([{"total": 1}, [_OPTION_ROW]])
    await reader.list_series_filtered(12)
    order_by = re.sub(
        r"\s+", " ", cur.calls[1][0].split("ORDER BY", 1)[1].split("LIMIT", 1)[0]
    ).strip()
    assert order_by == (
        "c.expiration NULLS FIRST, c.strike NULLS FIRST, "
        "c.option_type NULLS FIRST, s.serie_id"
    )


async def test_page_binds_limit_then_offset_in_that_order():
    """Transposed, ``LIMIT 100 OFFSET 50`` becomes ``LIMIT 50 OFFSET 100``.

    Page 2 would then be page 1 shifted by the wrong stride — rows skipped, and
    silently: the shape of the response is unchanged. The two values differ here
    precisely so a swap is observable.
    """
    reader, cur = _mk([{"total": 500}, [_OPTION_ROW]])
    await reader.list_series_filtered(12, serie_type="bbba", skip=50, limit=100)
    page_sql, page_params = cur.calls[1]
    assert re.search(r"LIMIT %s\s+OFFSET %s", page_sql)
    assert page_params == (12, "bbba", 100, 50)  # object_id, serie_type, LIMIT, OFFSET
    # The COUNT is unfiltered by paging: same filters, no limit/offset appended.
    assert cur.calls[0][1] == (12, "bbba")


async def test_filter_values_are_bound_never_interpolated():
    """Every filter value reaches SQL as a parameter, none as statement text."""
    reader, cur = _mk([{"total": 1}, [_OPTION_ROW]])
    await reader.list_series_filtered(
        12,
        expiration_min=date(2026, 3, 1),
        expiration_max=date(2026, 3, 31),
        strike_min=6000.0,
        strike_max=7000.0,
        option_type="put",
        serie_type="bbba",
        freq="1m",
        skip=7,
        limit=9,
    )
    for sql, params in cur.calls:
        for literal in ("2026-03-01", "2026-03-31", "6000", "7000", "'put'", "'bbba'"):
            assert literal not in sql, f"{literal!r} interpolated into SQL: {sql}"
        assert isinstance(params, tuple) and params[0] == 12
    assert cur.calls[1][1] == (
        12,
        "bbba",
        "1m",
        "put",
        date(2026, 3, 1),
        date(2026, 3, 31),
        6000.0,
        7000.0,
        9,
        7,
    )


async def test_omitted_filters_add_no_predicate():
    """Defaults must be permissive — an ``any``/``both`` default that emitted a
    predicate would return an empty page for every unfiltered request."""
    reader, cur = _mk([{"total": 201027}, []])
    await reader.list_series_filtered(12)
    for sql, params in cur.calls:
        assert _where_of(sql) == "s.object_id = %s"
        assert params[0] == 12
    assert cur.calls[0][1] == (12,)


# --------------------------------------------------------------------------- #
# Whitelisting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"serie_type": "fact_bar; DROP"}, "serie_type"),
        ({"serie_type": "both"}, "serie_type"),  # valid option_type, wrong domain
        ({"freq": "5m"}, "freq"),
        ({"freq": "any OR 1=1"}, "freq"),
        ({"option_type": "any"}, "option_type"),  # valid serie_type, wrong domain
        ({"option_type": "call'"}, "option_type"),
    ],
)
async def test_unknown_enum_is_rejected_before_any_statement_runs(kwargs, match):
    """Enum values are the only filter inputs spliced into the clause STRING.

    They are whitelisted, and the rejection has to happen before the pool is
    touched — asserting only that it raises would also pass if the value reached
    the database and the driver rejected it there.
    """
    reader, cur = _mk([{"total": 0}, []])
    with pytest.raises(DataAccessError, match=match):
        await reader.list_series_filtered(12, **kwargs)
    assert cur.calls == [], "a rejected filter must not reach the database"


@pytest.mark.parametrize("serie_type", ["bar", "value", "greeks", "bbba", "any"])
async def test_every_dispatchable_serie_type_is_accepted(serie_type):
    """The whitelist is derived from ``FACT_DISPATCH``; a type the fact reader can
    dispatch must be filterable, or the filter form offers a dead option."""
    reader, _ = _mk([{"total": 0}, []])
    items, total = await reader.list_series_filtered(12, serie_type=serie_type)
    assert (items, total) == ([], 0)


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #
async def test_page_shapes_rows_for_the_result_list():
    reader, _ = _mk([{"total": 195}, [_OPTION_ROW, _OBJECT_LEVEL_ROW]])
    items, total = await reader.list_series_filtered(12)

    assert total == 195
    # Contract metadata is joined in, so the frontend needs no contract map and
    # no second round-trip.
    assert items[0] == {
        "serie_id": 1433194,
        "contract_id": 77,
        "type": "bbba",
        "freq": "1m",
        "source": "DATABENTO:GLBX.MDP3:bbo-1m",
        "contract_code": "EW2H6 P6260.20260313",
        "expiration": "2026-03-13",  # date -> ISO string, JSON-ready
        "strike": 6260.0,
        "option_type": "put",
    }
    # Decimal -> float AT THIS BOUNDARY: a Decimal is not JSON-serializable and
    # poisons NumPy downstream. `== 6260.0` alone is true of the Decimal too.
    assert type(items[0]["strike"]) is float
    # An object-level serie survives with NULL contract columns as None, not as a
    # crash on ``None.isoformat()`` and not dropped.
    assert items[1]["contract_id"] is None
    assert items[1]["expiration"] is None
    assert items[1]["strike"] is None
    assert items[1]["option_type"] is None
    assert items[1]["serie_id"] == 4


async def test_page_tolerates_a_missing_count_row():
    """Defensive: a ``fetchone()`` of ``None`` must not blow up the total."""
    reader, _ = _mk([None, []])
    items, total = await reader.list_series_filtered(12)
    assert (items, total) == ([], 0)


async def test_page_wraps_driver_errors_as_data_access_error():
    reader, _ = _mk([{"total": 1}])  # 2nd execute has no result set left
    with pytest.raises(DataAccessError, match="filtered series for object 12"):
        await reader.list_series_filtered(12)
