"""Unit tests for ``SqlInstrumentReaderV2.read_serie_facts`` grain mapping (no live DB).

``read_serie_facts`` used to map EVERY ``ts`` through ``_ts_to_int``, collapsing a
timestamptz to a ``YYYYMMDD`` int.  For a ``1m`` serie that put every minute of a
trading day onto ONE abscissa (serie 1116679 returned ``ts: [20260601, 20260601]``).
The fix resolves the representation once, here, from ``serie.freq``.

These tests drive the REAL method through a fake pool/cursor, so reverting

    to_ts = _ts_to_int if grain == "daily" else _ts_to_iso

back to the pre-fix ``to_ts = _ts_to_int`` goes RED here.  The service-level tests
in ``tests/unit/test_data_v2_service.py`` cannot catch that regression: they stub
the reader out entirely and only observe what their own fake handed back.

A live-warehouse integration test covers real-dwh parity; everything below is
offline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from tcg.data._sql.instruments_v2 import SqlInstrumentReaderV2

# --------------------------------------------------------------------------- #
# Fake async pool / connection / cursor.
#
# Same shape as the sibling SQL reader tests in this directory
# (``test_sql_options_bulk.py`` etc.): ``pool.connection()`` -> ``conn.cursor()``
# -> ``execute`` -> ``fetchall``, every level an async context manager.  Defined
# locally rather than shared because the ``tests/`` tree has no ``__init__.py``,
# so a test module cannot import from a sibling or from ``conftest``.
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
    """Minimal stand-in for ``DwhConnectionPool`` exposing ``connection()``."""

    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def connection(self):
        return _FakeConn(self._cursor)


def _mk(rows: list[dict[str, Any]]) -> SqlInstrumentReaderV2:
    """A real reader whose pool serves *rows* from the fact SELECT."""
    return SqlInstrumentReaderV2(_FakePool(_FakeCursor(rows)))  # type: ignore[arg-type]


_UTC = timezone.utc

#: Two rows one minute apart, on the SAME calendar day — precisely the input the
#: pre-fix code flattened to ``[20260601, 20260601]``.
_TWO_MINUTES: list[dict[str, Any]] = [
    {"ts": datetime(2026, 6, 1, 14, 31, tzinfo=_UTC), "value": 1.0},
    {"ts": datetime(2026, 6, 1, 14, 32, tzinfo=_UTC), "value": 2.0},
]

_ISO_TWO_MINUTES = ["2026-06-01T14:31:00Z", "2026-06-01T14:32:00Z"]


# --------------------------------------------------------------------------- #
# Grain mapping
# --------------------------------------------------------------------------- #
async def test_intraday_1m_returns_distinct_iso_timestamps():
    """The headline regression guard, asserted against the real reader."""
    grain, ts, cols = await _mk(_TWO_MINUTES).read_serie_facts(1, "value", freq="1m")
    assert grain == "intraday"
    assert ts == _ISO_TWO_MINUTES
    # Two distinct minutes must not collapse onto a single abscissa.
    assert len(set(ts)) == 2
    assert cols == {"value": [1.0, 2.0]}


async def test_daily_returns_yyyymmdd_ints():
    rows = [
        {"ts": datetime(2026, 6, 1, tzinfo=_UTC), "value": 1.0},
        {"ts": datetime(2026, 6, 2, tzinfo=_UTC), "value": 2.0},
    ]
    grain, ts, cols = await _mk(rows).read_serie_facts(1, "value", freq="daily")
    assert grain == "daily"
    assert ts == [20260601, 20260602]
    assert all(isinstance(t, int) for t in ts)
    assert cols == {"value": [1.0, 2.0]}


@pytest.mark.parametrize("freq", ["5m", "1h", "", "  ", None])
async def test_unknown_freq_falls_back_to_iso(freq):
    """Anything not explicitly daily stays intraday — collapsing is the lossy way.

    A future ``5m``/``1h`` frequency therefore cannot silently reintroduce the
    single-abscissa defect.
    """
    grain, ts, _cols = await _mk(_TWO_MINUTES).read_serie_facts(1, "value", freq=freq)
    assert grain == "intraday"
    assert ts == _ISO_TWO_MINUTES


async def test_daily_freq_is_matched_case_and_space_insensitively():
    grain, ts, _cols = await _mk(_TWO_MINUTES).read_serie_facts(
        1, "value", freq=" Daily "
    )
    assert grain == "daily"
    assert ts == [20260601, 20260601]


async def test_freq_alone_decides_the_representation_of_identical_rows():
    """The two branches must diverge on byte-identical input.

    Collapsing to a date is CORRECT for a daily serie and WRONG for a 1m one, so
    the same two rows have to come back differently. Any implementation that
    ignores ``freq`` fails one of these two assertions.
    """
    _, ts_daily, _ = await _mk(_TWO_MINUTES).read_serie_facts(1, "value", freq="daily")
    _, ts_intraday, _ = await _mk(_TWO_MINUTES).read_serie_facts(1, "value", freq="1m")
    assert ts_daily == [20260601, 20260601]
    assert ts_intraday == _ISO_TWO_MINUTES
    assert len(set(ts_daily)) == 1 and len(set(ts_intraday)) == 2


async def test_naive_ts_is_treated_as_utc():
    """psycopg returns aware datetimes for timestamptz; be defensive anyway."""
    rows = [{"ts": datetime(2026, 6, 1, 14, 31), "value": 1.0}]
    _, ts, _cols = await _mk(rows).read_serie_facts(1, "value", freq="1m")
    assert ts == ["2026-06-01T14:31:00Z"]


async def test_non_utc_ts_is_normalised_to_utc():
    """A ts in a +02:00 offset must shift, not just get its suffix rewritten."""
    tz = timezone(timedelta(hours=2))
    rows = [{"ts": datetime(2026, 6, 1, 16, 31, tzinfo=tz), "value": 1.0}]
    _, ts, _cols = await _mk(rows).read_serie_facts(1, "value", freq="1m")
    assert ts == ["2026-06-01T14:31:00Z"]


async def test_empty_result_keeps_the_grain_and_the_field_keys():
    grain, ts, cols = await _mk([]).read_serie_facts(1, "value", freq="1m")
    assert (grain, ts, cols) == ("intraday", [], {"value": []})


async def test_multi_field_serie_type_maps_every_column():
    """A wider fact table (``bbba``) must fill one list per dispatched field."""
    rows = [
        {
            "ts": datetime(2026, 6, 1, 14, 31, tzinfo=_UTC),
            "best_bid_value": 610.5,
            "best_bid_volume": 15.0,
            "best_ask_value": 612.0,
            "best_ask_volume": 15.0,
        }
    ]
    grain, ts, cols = await _mk(rows).read_serie_facts(1, "bbba", freq="1m")
    assert grain == "intraday"
    assert ts == ["2026-06-01T14:31:00Z"]
    assert cols == {
        "best_bid_value": [610.5],
        "best_bid_volume": [15.0],
        "best_ask_value": [612.0],
        "best_ask_volume": [15.0],
    }


# --------------------------------------------------------------------------- #
# Invariants the grain change must not disturb
# --------------------------------------------------------------------------- #
async def test_decimal_and_null_are_coerced_at_this_boundary():
    """Decimal -> float stays here (``to_float``); SQL NULL stays ``None``."""
    rows = [
        {"ts": datetime(2026, 6, 1, 14, 31, tzinfo=_UTC), "value": Decimal("610.5")},
        {"ts": datetime(2026, 6, 1, 14, 32, tzinfo=_UTC), "value": None},
    ]
    _, _ts, cols = await _mk(rows).read_serie_facts(1, "value", freq="1m")
    assert cols == {"value": [610.5, None]}
    assert isinstance(cols["value"][0], float)


async def test_ts_is_still_bounded_by_a_constant_range():
    """Partition/BRIN invariant: a constant ``>= lower AND < upper`` ts range.

    The grain change must not touch the SQL. ``upper`` is ``end + 1 day`` so an
    inclusive end date is captured.
    """
    cur = _FakeCursor(_TWO_MINUTES)
    reader = SqlInstrumentReaderV2(_FakePool(cur))  # type: ignore[arg-type]
    await reader.read_serie_facts(
        7, "value", freq="1m", start=date(2026, 6, 1), end=date(2026, 6, 2)
    )
    sql, params = cur.calls[0]
    assert "ts >= %s AND ts < %s" in " ".join(sql.split())
    assert params == (7, date(2026, 6, 1), date(2026, 6, 3))
