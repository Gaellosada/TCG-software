"""Unit tests for ``SqlOptionsDataReader.query_held_rows`` (no live DB).

Hold-leg two-phase Phase 2: an IDENTITY keyset fetch of already-SELECTED held
option SYMBOLS over per-symbol date windows.  These tests pin the query SHAPE
(the properties that keep the plan index-only + byte-identical to the full-chain
hold path) without a live warehouse:

  * ONE query, a ``heldwin (sym, lo, hi)`` VALUES table joined on ``d.symbol``
    (symbol-granular → ALL duplicate ``instrument_id`` rows of a symbol return);
  * per-symbol ``BETWEEN h.lo AND h.hi`` restriction on BOTH keyset scans;
  * the redundant CONSTANT chunk-min/max ``trade_date BETWEEN`` partition prune
    on every partitioned-fact reference (2 keyset scans + 2 final joins);
  * ordered by ``k.trade_date, i.instrument_id`` (first-by-instrument_id pick);
  * the SAME expiration_cycle predicate as the full-chain path when a cycle is
    given (LOAD-BEARING — a symbol is NOT unique across cycles); none when None;
  * rows grouped under their fact ``trade_date``; empty windows → no query;
  * failures wrap as ``OptionsDataAccessError``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from tcg.data._sql.options import SqlOptionsDataReader
from tcg.types.errors import OptionsDataAccessError


class _FakeCursor:
    def __init__(self, responder) -> None:
        self._responder = responder
        self._last_rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        self._last_rows = self._responder(sql, params)

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._last_rows


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


def _held_row(trade_date: date, symbol: str, iid: int, strike: float) -> dict:
    return {
        "option_instrument_id": iid,
        "trade_date": trade_date,
        "option_symbol": symbol,
        "root_symbol": "IND_SP_500",
        "underlying_symbol": "SPX",
        "strike": strike,
        "option_type": "P",
        "expiration": date(2024, 3, 15),
        "expiration_cycle": "M",
        "bid": 1.0,
        "ask": 1.5,
        "option_close": 1.2,
        "volume": 10,
        "open_interest": 100,
        "delta": -0.10,
        "gamma": 0.01,
        "vega": 0.2,
        "theta": -0.05,
        "implied_vol": 0.25,
        "underlying_price": 5000.0,
        "contract_size": 100.0,
        "currency": "USD",
        "provider": "IVOL",
    }


def _make_reader(rows: list[dict]):
    def responder(sql: str, params: Any) -> list[dict]:
        if "asset_class = 'forex'" in sql or "asset_class='forex'" in sql:
            return []
        return rows

    cur = _FakeCursor(responder)
    reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]
    return reader, cur


_SYM_A = "SPX240315P4500"
_SYM_B = "SPX240315P4600"
# ``hi`` includes the NEXT roll date (the seam), so B's window overlaps A's tail.
_WINDOWS = [
    (_SYM_A, date(2024, 2, 1), date(2024, 2, 20)),
    (_SYM_B, date(2024, 2, 20), date(2024, 3, 8)),
]


def _sql(cur: _FakeCursor) -> tuple[str, Any]:
    return next(c for c in cur.calls if "WITH heldwin" in c[0])


class TestQueryHeldRowsShape:
    async def test_single_query_symbol_keyed_values(self):
        reader, cur = _make_reader([])
        await reader.query_held_rows(root="OPT_SP_500", type="P", held_windows=_WINDOWS)
        main = [c for c in cur.calls if "WITH heldwin" in c[0]]
        assert len(main) == 1, "expected ONE identity keyset query"
        sql, params = main[0]
        assert "heldwin (sym, lo, hi)" in sql
        assert "VALUES" in sql
        assert "JOIN heldwin h ON d.symbol = h.sym" in sql
        flat = list(params)
        for sym, lo, hi in _WINDOWS:
            assert sym in flat and lo in flat and hi in flat

    async def test_per_symbol_window_on_both_keyset_scans(self):
        reader, cur = _make_reader([])
        await reader.query_held_rows(root="OPT_SP_500", type="P", held_windows=_WINDOWS)
        sql, _ = _sql(cur)
        assert sql.count("BETWEEN h.lo AND h.hi") == 2

    async def test_constant_chunk_prune_on_every_fact_reference(self):
        reader, cur = _make_reader([])
        await reader.query_held_rows(root="OPT_SP_500", type="P", held_windows=_WINDOWS)
        sql, params = _sql(cur)
        assert "p.trade_date BETWEEN %s AND %s" in sql
        assert "g.trade_date BETWEEN %s AND %s" in sql
        chunk_lo = min(w[1] for w in _WINDOWS)
        chunk_hi = max(w[2] for w in _WINDOWS)
        flat = list(params)
        # 2 keyset scans + 2 final joins.
        assert flat.count(chunk_lo) >= 4
        assert flat.count(chunk_hi) >= 4

    async def test_ordered_by_trade_date_then_instrument_id(self):
        reader, cur = _make_reader([])
        await reader.query_held_rows(root="OPT_SP_500", type="P", held_windows=_WINDOWS)
        sql, _ = _sql(cur)
        assert "ORDER BY k.trade_date, i.instrument_id" in sql

    async def test_no_cycle_predicate_when_none(self):
        """cycle=None (default) applies NO filter — matching the full-chain path's
        None case.  (The SELECT still PROJECTS ``i.expiration_cycle``; only the
        ``expiration_cycle = %s`` WHERE predicate must be absent.)"""
        reader, cur = _make_reader([])
        await reader.query_held_rows(root="OPT_SP_500", type="P", held_windows=_WINDOWS)
        sql, _ = _sql(cur)
        assert "expiration_cycle = %s" not in sql
        assert "expiration_cycle = ANY(%s)" not in sql

    async def test_scalar_cycle_predicate_applied(self):
        """LOAD-BEARING, NOT redundant (audit_d4 P0): a held SYMBOL is NOT unique
        across cycles — the ~2.68% duplicate-instrument_id quirk is ONE symbol
        double-tagged (``"M"`` + ``"W3 Friday"``) with different quotes.
        ``query_held_rows`` MUST apply the SAME cycle predicate as the full-chain
        path so the off-cycle sibling is dropped and ``_row_for_contract``'s
        first-by-instrument_id pick is byte-identical (live 4970_P/2024-03-06:
        7.40 vs 4.15).  Reverting the R3 fix (the ``_cycle_predicate`` call in
        ``query_held_rows``) makes this go RED."""
        reader, cur = _make_reader([])
        await reader.query_held_rows(
            root="OPT_SP_500", type="P", held_windows=_WINDOWS, expiration_cycle="M"
        )
        sql, params = _sql(cur)
        assert "expiration_cycle = %s" in sql
        assert "M" in list(params)

    async def test_multi_tag_cycle_predicate_applied(self):
        """A monthly 3rd-Friday series expands to a two-tag sequence at the wiring
        layer; the ``= ANY(%s)`` form must bind the tag LIST (same R3 guarantee)."""
        reader, cur = _make_reader([])
        await reader.query_held_rows(
            root="OPT_SP_500",
            type="P",
            held_windows=_WINDOWS,
            expiration_cycle=("M", "W3 Friday"),
        )
        sql, params = _sql(cur)
        assert "expiration_cycle = ANY(%s)" in sql
        assert ["M", "W3 Friday"] in [p for p in params if isinstance(p, list)]

    async def test_type_pushed_when_scalar(self):
        reader, cur = _make_reader([])
        await reader.query_held_rows(root="OPT_SP_500", type="P", held_windows=_WINDOWS)
        sql, params = _sql(cur)
        assert "option_type = %s" in sql
        assert "P" in list(params)

    async def test_type_both_no_option_type_filter(self):
        reader, cur = _make_reader([])
        await reader.query_held_rows(
            root="OPT_SP_500", type="both", held_windows=_WINDOWS
        )
        sql, _ = _sql(cur)
        assert "option_type = %s" not in sql


class TestQueryHeldRowsResult:
    async def test_all_duplicate_instrument_ids_of_a_symbol_returned(self):
        """BYTE-IDENTITY: the dwh stores duplicate instrument_ids per symbol; the
        symbol-keyed join returns EVERY physical row, ordered by instrument_id, so
        ``_row_for_contract``'s first-by-instrument_id pick matches the full
        chain."""
        d = date(2024, 2, 5)
        rows = [
            _held_row(d, _SYM_A, iid=1001, strike=4500.0),
            _held_row(d, _SYM_A, iid=1002, strike=4500.0),  # duplicate iid
        ]
        reader, _cur = _make_reader(rows)
        result = await reader.query_held_rows(
            root="OPT_SP_500", type="P", held_windows=_WINDOWS
        )
        assert len(result[d]) == 2
        # Both are the SAME contract_id (symbol) — the dup set.
        assert {c.contract_id for c, _r in result[d]} == {_SYM_A}

    async def test_rows_grouped_under_trade_date(self):
        d1, d2 = date(2024, 2, 5), date(2024, 2, 25)
        rows = [
            _held_row(d1, _SYM_A, 1001, 4500.0),
            _held_row(d2, _SYM_B, 2001, 4600.0),
        ]
        reader, _cur = _make_reader(rows)
        result = await reader.query_held_rows(
            root="OPT_SP_500", type="P", held_windows=_WINDOWS
        )
        assert list(result.keys()) == [d1, d2] or set(result.keys()) == {d1, d2}
        assert result[d1][0][0].contract_id == _SYM_A
        assert result[d2][0][0].contract_id == _SYM_B

    async def test_empty_windows_no_query(self):
        reader, cur = _make_reader([])
        result = await reader.query_held_rows(
            root="OPT_SP_500", type="P", held_windows=[]
        )
        assert result == {}
        assert cur.calls == []

    async def test_empty_and_none_symbols_dropped(self):
        reader, cur = _make_reader([])
        result = await reader.query_held_rows(
            root="OPT_SP_500",
            type="P",
            held_windows=[("", date(2024, 1, 1), date(2024, 1, 2))],
        )
        assert result == {}
        assert cur.calls == []

    async def test_duplicate_symbol_window_widened_not_duplicated(self):
        reader, cur = _make_reader([])
        await reader.query_held_rows(
            root="OPT_SP_500",
            type="P",
            held_windows=[
                (_SYM_A, date(2024, 2, 1), date(2024, 2, 10)),
                (_SYM_A, date(2024, 2, 8), date(2024, 2, 20)),
            ],
        )
        sql, params = _sql(cur)
        # Exactly ONE VALUES row for the symbol (de-duped), widened to the union.
        assert sql.count("::text") == 1
        flat = list(params)
        assert date(2024, 2, 1) in flat and date(2024, 2, 20) in flat

    async def test_data_access_error_wrapping(self):
        def boom(sql: str, params: Any):
            raise RuntimeError("connection reset")

        cur = _FakeCursor(boom)
        reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]
        with pytest.raises(OptionsDataAccessError):
            await reader.query_held_rows(
                root="OPT_SP_500", type="P", held_windows=_WINDOWS
            )
