"""Unit tests for ``SqlOptionsDataReader.query_chain_bulk_multi`` (no live DB).

The year-chunk fast path (Wave 3) collapses the per-expiration
``query_chain_bulk`` fan-out into ONE query per calendar year, each expiration
date-restricted to its OWN window via a ``VALUES(exp, lo, hi)`` join.  These
tests pin the query SHAPE (the properties the Wave 2 EXPLAIN proved keep the
plan index-only) without a live warehouse:

  * ONE query for the whole chunk (no per-expiration N+1);
  * a ``win (exp, lo, hi)`` VALUES table joined on ``expiration``;
  * per-expiration ``BETWEEN w.lo AND w.hi`` restriction on BOTH keyset scans
    (the load-bearing restriction);
  * the redundant CONSTANT chunk-min/max ``trade_date BETWEEN`` prune bound on
    EVERY partitioned-fact reference (keyset scans + final joins);
  * result dict keyed by EVERY requested trade_date (``[]`` when empty), rows
    grouped under their fact ``trade_date``;
  * Option A (``strike_windows=None``) pushes NO strike filter (superset);
  * Option B (``strike_windows`` map) pushes per-expiration strike bounds;
  * failures wrap as ``OptionsDataAccessError``.

A live-DB parity/perf profile is Wave 4's job.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import pytest

from tcg.data._sql.options import SqlOptionsDataReader
from tcg.types.errors import OptionsDataAccessError


# --------------------------------------------------------------------------- #
# Fake async pool / connection / cursor (mirrors test_sql_options_bulk.py)
# --------------------------------------------------------------------------- #
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


def _chain_row(trade_date: date, symbol: str, strike: float, expiration: date) -> dict:
    return {
        "option_instrument_id": hash(symbol) & 0xFFFF,
        "trade_date": trade_date,
        "option_symbol": symbol,
        "root_symbol": "IND_SP_500",
        "underlying_symbol": "SPX",
        "strike": strike,
        "option_type": "P",
        "expiration": expiration,
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


def _make_reader(chain_rows: list[dict]):
    def responder(sql: str, params: Any) -> list[dict]:
        if "asset_class = 'forex'" in sql or "asset_class='forex'" in sql:
            return []
        return chain_rows

    cur = _FakeCursor(responder)
    pool = _FakePool(cur)
    reader = SqlOptionsDataReader(pool)  # type: ignore[arg-type]
    return reader, cur


def _main_sql(cur: _FakeCursor) -> tuple[str, Any]:
    return next(c for c in cur.calls if "WITH win" in c[0])


# Two monthly expirations in the SAME calendar year, each with its own dates.
_EXP_A = date(2024, 3, 15)
_EXP_B = date(2024, 6, 21)
_DATES_A = [date(2024, 2, 15), date(2024, 2, 16)]
_DATES_B = [date(2024, 5, 20), date(2024, 5, 21)]
_GROUPS = [(_EXP_A, _DATES_A), (_EXP_B, _DATES_B)]


class TestQueryChainBulkMultiShape:
    async def test_single_query_for_all_expirations_no_n_plus_one(self):
        reader, cur = _make_reader([])
        await reader.query_chain_bulk_multi(root="OPT_SP_500", type="P", groups=_GROUPS)
        main_calls = [c for c in cur.calls if "WITH win" in c[0]]
        assert len(main_calls) == 1, (
            f"expected ONE multi-expiration query, got {len(main_calls)} "
            "(per-expiration N+1 defeats the collapse)"
        )

    async def test_values_win_table_joined_on_expiration(self):
        reader, cur = _make_reader([])
        await reader.query_chain_bulk_multi(root="OPT_SP_500", type="P", groups=_GROUPS)
        sql, params = _main_sql(cur)
        assert "win (exp, lo, hi)" in sql
        assert "VALUES" in sql
        assert "JOIN win w ON d.expiration = w.exp" in sql
        # Each expiration + its window min/max is bound.
        flat = list(params)
        for exp, dts in _GROUPS:
            assert exp in flat
            assert min(dts) in flat
            assert max(dts) in flat

    async def test_per_expiration_window_restriction_on_both_keyset_scans(self):
        """The load-bearing ``BETWEEN w.lo AND w.hi`` must gate BOTH fact scans."""
        reader, cur = _make_reader([])
        await reader.query_chain_bulk_multi(root="OPT_SP_500", type="P", groups=_GROUPS)
        sql, _ = _main_sql(cur)
        assert sql.count("BETWEEN w.lo AND w.hi") == 2, (
            "each keyset scan (price + greeks) must restrict to the expiration's "
            "own window, else the plan seq-scans the 6M-row partitions"
        )

    async def test_constant_chunk_prune_bound_on_every_fact_reference(self):
        """The redundant chunk-min/max constant prunes yearly partitions; it must
        appear on both keyset scans AND both final joins (4 fact references)."""
        reader, cur = _make_reader([])
        await reader.query_chain_bulk_multi(root="OPT_SP_500", type="P", groups=_GROUPS)
        sql, params = _main_sql(cur)
        # Two keyset scans use bare "trade_date BETWEEN"; final joins use
        # "p.trade_date BETWEEN" / "g.trade_date BETWEEN".
        assert "p.trade_date BETWEEN %s AND %s" in sql
        assert "g.trade_date BETWEEN %s AND %s" in sql
        # The chunk bounds = overall min/max across ALL groups' dates.
        chunk_lo = min(min(_DATES_A), min(_DATES_B))
        chunk_hi = max(max(_DATES_A), max(_DATES_B))
        flat = list(params)
        # Bound on both keyset scans (2) + both final joins (2) = >=4 each.
        assert flat.count(chunk_lo) >= 4, (
            f"chunk_lo {chunk_lo} bound {flat.count(chunk_lo)}x"
        )
        assert flat.count(chunk_hi) >= 4, (
            f"chunk_hi {chunk_hi} bound {flat.count(chunk_hi)}x"
        )

    async def test_dates_bound_as_array_any(self):
        reader, cur = _make_reader([])
        await reader.query_chain_bulk_multi(root="OPT_SP_500", type="P", groups=_GROUPS)
        sql, params = _main_sql(cur)
        assert "ANY(" in sql or "ANY (" in sql
        all_dates = sorted(set(_DATES_A) | set(_DATES_B))
        flat = list(params)
        assert any(
            isinstance(p, (list, tuple)) and list(p) == all_dates for p in flat
        ), "the full de-duped/sorted date union must be bound as one array param"

    async def test_option_a_pushes_no_strike_filter(self):
        reader, cur = _make_reader([])
        await reader.query_chain_bulk_multi(
            root="OPT_SP_500", type="P", groups=_GROUPS, strike_windows=None
        )
        sql, _ = _main_sql(cur)
        assert "strike_lo" not in sql and "strike_hi" not in sql
        assert "d.strike >=" not in sql and "d.strike <=" not in sql

    async def test_option_b_pushes_per_expiration_strike_bounds(self):
        reader, cur = _make_reader([])
        windows = {_EXP_A: (4000.0, 5000.0), _EXP_B: (4500.0, 5500.0)}
        await reader.query_chain_bulk_multi(
            root="OPT_SP_500", type="P", groups=_GROUPS, strike_windows=windows
        )
        sql, params = _main_sql(cur)
        assert "win (exp, lo, hi, strike_lo, strike_hi)" in sql
        assert "d.strike >= w.strike_lo" in sql
        assert "d.strike <= w.strike_hi" in sql
        flat = list(params)
        for lo, hi in windows.values():
            assert lo in flat and hi in flat

    async def test_type_and_cycle_pushed(self):
        reader, cur = _make_reader([])
        await reader.query_chain_bulk_multi(
            root="OPT_SP_500", type="P", groups=_GROUPS, expiration_cycle="M"
        )
        sql, params = _main_sql(cur)
        assert "option_type = %s" in sql
        assert "expiration_cycle" in sql
        flat = list(params)
        assert "OPT_SP_500" in flat and "P" in flat and "M" in flat


class TestQueryChainBulkMultiResult:
    async def test_every_requested_date_present_even_when_empty(self):
        reader, cur = _make_reader([])
        result = await reader.query_chain_bulk_multi(
            root="OPT_SP_500", type="P", groups=_GROUPS
        )
        assert set(result.keys()) == set(_DATES_A) | set(_DATES_B)
        assert all(v == [] for v in result.values())

    async def test_rows_grouped_under_their_trade_date(self):
        rows = [
            _chain_row(_DATES_A[0], "SPX240315P4500", 4500.0, _EXP_A),
            _chain_row(_DATES_A[0], "SPX240315P4600", 4600.0, _EXP_A),
            _chain_row(_DATES_B[0], "SPX240621P4700", 4700.0, _EXP_B),
        ]
        reader, _cur = _make_reader(rows)
        result = await reader.query_chain_bulk_multi(
            root="OPT_SP_500", type="P", groups=_GROUPS
        )
        assert len(result[_DATES_A[0]]) == 2
        assert len(result[_DATES_B[0]]) == 1
        assert result[_DATES_A[1]] == []
        _c, row = result[_DATES_A[0]][0]
        assert row.date == _DATES_A[0]

    async def test_empty_groups_returns_empty_no_query(self):
        reader, cur = _make_reader([])
        result = await reader.query_chain_bulk_multi(
            root="OPT_SP_500", type="P", groups=[]
        )
        assert result == {}
        assert cur.calls == []

    async def test_groups_with_only_empty_date_lists_issue_no_query(self):
        reader, cur = _make_reader([])
        result = await reader.query_chain_bulk_multi(
            root="OPT_SP_500", type="P", groups=[(_EXP_A, []), (_EXP_B, [])]
        )
        assert result == {}
        assert cur.calls == []

    async def test_data_access_error_wrapping(self):
        def boom(sql: str, params: Any):
            raise RuntimeError("connection reset")

        cur = _FakeCursor(boom)
        reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]
        with pytest.raises(OptionsDataAccessError):
            await reader.query_chain_bulk_multi(
                root="OPT_SP_500", type="P", groups=_GROUPS
            )
