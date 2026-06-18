"""Unit tests for ``SqlOptionsDataReader.query_chain_bulk`` (no live DB).

These exercise the bulk-chain path that the options *rolling* feature relies
on (``stream_resolver._fetch_exp`` -> ``query_chain_bulk``).  The #57 cutover
shipped ``SqlOptionsDataReader`` with ``query_chain`` but WITHOUT
``query_chain_bulk`` (which the ``OptionsDataReader`` Protocol declares and the
roll resolver requires), so every option roll raised ``AttributeError`` ->
HTTP 500.  These tests pin the contract:

  * the method exists and is awaitable;
  * it issues ONE query for all dates (no per-date N+1);
  * it binds the date list (``= ANY``) rather than a scalar date;
  * the result is a ``dict`` keyed by EVERY requested date (``[]`` when a date
    had no contracts) -- the same semantics the removed Mongo reader had
    (``results = {d: [] for d in dates}``), so it is a drop-in for what
    ``_fetch_exp`` expects;
  * rows are grouped under the fact ``trade_date`` they came from.

A live-DB integration test (``tests/integration/data/options/
test_sql_options_bulk_integration.py``) covers real-warehouse parity.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import pytest

from tcg.data._sql.options import SqlOptionsDataReader


# --------------------------------------------------------------------------- #
# Fake async pool / connection / cursor
# --------------------------------------------------------------------------- #
class _FakeCursor:
    """Records executed SQL+params; returns canned rows per call.

    ``rows_for(sql, params) -> list[dict]`` lets a test drive different
    result sets for the main chain query vs the coin-spot lookup.
    """

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

    async def fetchone(self) -> dict[str, Any] | None:
        return self._last_rows[0] if self._last_rows else None


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
        self.connection_opens = 0

    def connection(self):
        self.connection_opens += 1
        return _FakeConn(self._cursor)


def _chain_row(
    trade_date: date, symbol: str, strike: float, delta: float | None
) -> dict:
    """A merged-chain row shaped like the bulk SELECT projection."""
    return {
        "option_instrument_id": hash(symbol) & 0xFFFF,
        "trade_date": trade_date,
        "option_symbol": symbol,
        "root_symbol": "IND_SP_500",
        "underlying_symbol": "SPX",
        "strike": strike,
        "option_type": "C",
        "expiration": date(2024, 6, 21),
        "expiration_cycle": "M",
        "bid": 1.0,
        "ask": 1.5,
        "option_close": 1.2,
        "volume": 10,
        "open_interest": 100,
        "delta": delta,
        "gamma": 0.01,
        "vega": 0.2,
        "theta": -0.05,
        "implied_vol": 0.25,
        "underlying_price": 5000.0,
        "contract_size": 100.0,
        "currency": "USD",
        "provider": "IVOL",
    }


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class TestQueryChainBulk:
    @pytest.fixture
    def dates(self) -> list[date]:
        return [date(2024, 3, 15), date(2024, 3, 18), date(2024, 3, 19)]

    def _make_reader(self, chain_rows: list[dict]):
        """Build a reader whose pool returns ``chain_rows`` for the main
        bulk query and ``[]`` for the coin-spot lookup (non-crypto root)."""

        def responder(sql: str, params: Any) -> list[dict]:
            # The coin-spot helper selects from fact_price_eod joined to
            # dim_instrument on asset_class='forex'; the main bulk query has
            # the ids CTE.  Distinguish by a marker present only in the main
            # query.
            if "asset_class = 'forex'" in sql or "asset_class='forex'" in sql:
                return []
            return chain_rows

        cur = _FakeCursor(responder)
        pool = _FakePool(cur)
        reader = SqlOptionsDataReader(pool)  # type: ignore[arg-type]
        return reader, cur, pool

    async def test_method_exists_and_returns_dict_keyed_by_every_date(self, dates):
        """Drop-in contract: every requested date is a key (``[]`` if empty)."""
        rows = [
            _chain_row(dates[0], "SPX240621C5000", 5000.0, 0.50),
            _chain_row(dates[1], "SPX240621C5000", 5000.0, 0.48),
        ]
        reader, _cur, _pool = self._make_reader(rows)

        result = await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=dates,
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )

        assert isinstance(result, dict)
        # EVERY requested date present, even the one with no rows.
        assert set(result.keys()) == set(dates)
        assert len(result[dates[0]]) == 1
        assert len(result[dates[1]]) == 1
        assert result[dates[2]] == []  # no rows for this date -> empty list

    async def test_rows_grouped_under_their_trade_date(self, dates):
        """A row's (contract, row) lands under the fact ``trade_date``."""
        rows = [
            _chain_row(dates[0], "SPX240621C5000", 5000.0, 0.50),
            _chain_row(dates[0], "SPX240621C5100", 5100.0, 0.30),
            _chain_row(dates[1], "SPX240621C5000", 5000.0, 0.48),
        ]
        reader, _cur, _pool = self._make_reader(rows)

        result = await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=dates,
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )

        assert len(result[dates[0]]) == 2
        assert len(result[dates[1]]) == 1
        assert result[dates[2]] == []
        # The grouped row carries the right date on the OptionDailyRow.
        _contract, row = result[dates[0]][0]
        assert row.date == dates[0]
        # Strikes preserved on the contract side.
        strikes = {c.strike for c, _r in result[dates[0]]}
        assert strikes == {5000.0, 5100.0}

    async def test_single_query_for_all_dates_no_n_plus_one(self, dates):
        """The bulk path must issue ONE chain query, not one per date."""
        rows = [_chain_row(d, "SPX240621C5000", 5000.0, 0.5) for d in dates]
        reader, cur, _pool = self._make_reader(rows)

        await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=dates,
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )

        # Exactly one execute() carries the ids CTE (the chain query); the
        # only other execute is the coin-spot lookup (non-crypto -> still one
        # call but against the forex branch).  Crucially: NOT one chain query
        # per date.
        chain_calls = [
            c for c in cur.calls if "WITH ids AS" in c[0] or "FROM ids" in c[0]
        ]
        assert len(chain_calls) == 1, (
            f"expected exactly 1 chain query, got {len(chain_calls)} "
            "(N+1 per-date querying is forbidden)"
        )

    async def test_dates_bound_as_list_not_scalar(self, dates):
        """The date list must be bound for an ``= ANY`` match (one query),
        not a scalar ``= %s`` (which would force per-date queries)."""
        rows = [_chain_row(dates[0], "SPX240621C5000", 5000.0, 0.5)]
        reader, cur, _pool = self._make_reader(rows)

        await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=dates,
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )

        chain_call = next(
            c for c in cur.calls if "WITH ids AS" in c[0] or "FROM ids" in c[0]
        )
        sql, params = chain_call
        assert "ANY(" in sql or "ANY (" in sql, "dates must be bound via = ANY(...)"
        # The full date list appears as a single bound parameter (a list).
        flat = list(params)
        assert any(
            isinstance(p, (list, tuple)) and set(p) == set(dates) for p in flat
        ), "the whole date list must be bound as one parameter"

    async def test_type_and_strike_filters_pushed_to_sql(self, dates):
        """Type / strike / expiration filters are pushed into the dim CTE."""
        rows = [_chain_row(dates[0], "SPX240621C5000", 5000.0, 0.5)]
        reader, cur, _pool = self._make_reader(rows)

        await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=dates,
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
            strike_min=4900.0,
            strike_max=5100.0,
            expiration_cycle="M",
        )

        chain_call = next(
            c for c in cur.calls if "WITH ids AS" in c[0] or "FROM ids" in c[0]
        )
        sql, params = chain_call
        assert "source_collection" in sql
        assert "option_type" in sql
        assert "strike >=" in sql and "strike <=" in sql
        assert "expiration_cycle" in sql
        # Pushed values are bound.
        flat = list(params)
        assert "OPT_SP_500" in flat
        assert 4900.0 in flat and 5100.0 in flat
        assert "M" in flat

    async def test_data_access_error_wrapping(self, dates):
        """Underlying failures surface as OptionsDataAccessError."""
        from tcg.types.errors import OptionsDataAccessError

        def boom(sql: str, params: Any):
            raise RuntimeError("connection reset")

        cur = _FakeCursor(boom)
        pool = _FakePool(cur)
        reader = SqlOptionsDataReader(pool)  # type: ignore[arg-type]

        with pytest.raises(OptionsDataAccessError):
            await reader.query_chain_bulk(
                root="OPT_SP_500",
                dates=dates,
                type="C",
                expiration_min=date(2024, 6, 21),
                expiration_max=date(2024, 6, 21),
            )

    async def test_empty_dates_returns_empty_dict(self):
        """No dates -> empty dict, no query issued."""
        rows: list[dict] = []
        reader, cur, _pool = self._make_reader(rows)

        result = await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=[],
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )
        assert result == {}
        assert cur.calls == []
