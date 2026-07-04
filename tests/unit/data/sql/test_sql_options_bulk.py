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


class TestQueryChainBulkPartitionPruning:
    """Regression: the bulk query must let PostgreSQL prune fact partitions.

    The fact tables are RANGE-partitioned by ``trade_date`` (yearly, 1980..2050).
    The final LEFT JOINs match ``p.trade_date = k.trade_date`` where ``k.trade_date``
    is a RUNTIME value from the ``keyset`` CTE — a value the planner cannot use for
    plan-time pruning, so it fans the join out across ALL ~71 yearly partitions of
    BOTH facts (~142 partition scans + 142 relation locks PER call).  EXPLAIN ANALYZE
    on live dwh confirmed this is the OPT_SP_500 chain-bulk slowness (planning 34 ms
    + a 60 s ``statement_timeout`` blow-out under a cold cache -> the reported
    PoolTimeout); the single-date ``query_chain`` is fast because ``= %s`` is a
    constant and prunes to one partition.

    FIX (proven live): add a REDUNDANT constant ``AND <fact>.trade_date BETWEEN %s
    AND %s`` (min/max of the requested dates) to EACH fact LEFT JOIN.  The planner
    prunes on the constant range; ``= k.trade_date`` keeps correctness.  Live: rows
    identical (437==437; cross-year 100==100), partitions 142->2, exec 98ms->10ms.

    These tests are DETERMINISTIC (no live dwh): they assert the generated SQL
    carries the constant bound on BOTH joins and that the bound params are the
    min/max of the date list.
    """

    def _make_reader(self):
        def responder(sql: str, params: Any) -> list[dict]:
            return []

        cur = _FakeCursor(responder)
        pool = _FakePool(cur)
        reader = SqlOptionsDataReader(pool)  # type: ignore[arg-type]
        return reader, cur

    @staticmethod
    def _chain_sql(cur: _FakeCursor) -> str:
        call = next(c for c in cur.calls if "WITH ids AS" in c[0] or "FROM ids" in c[0])
        return call[0]

    async def test_both_fact_joins_carry_constant_trade_date_bound(self):
        """Each fact LEFT JOIN ``ON`` clause has a constant ``trade_date BETWEEN``."""
        reader, cur = self._make_reader()
        dates = [date(2024, 3, 15), date(2024, 3, 18), date(2024, 6, 21)]
        await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=dates,
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )
        sql = self._chain_sql(cur)

        # The constant range must appear on BOTH fact aliases (p = price, g =
        # greeks), inside their JOIN ``ON`` — NOT merely on the keyset, which is
        # already prunable via ``= ANY`` but the join is the part that fans out.
        assert "p.trade_date BETWEEN" in sql, (
            "fact_price_eod join lacks the constant trade_date bound -> the "
            "planner cannot prune partitions (the OPT_SP_500 chain-bulk slowness)"
        )
        assert "g.trade_date BETWEEN" in sql, (
            "fact_option_greeks join lacks the constant trade_date bound"
        )

    async def test_bound_params_are_min_and_max_of_dates(self):
        """The BETWEEN bounds bind min(dates) and max(dates)."""
        reader, cur = self._make_reader()
        # Deliberately UNSORTED + duplicate to prove min/max (not first/last).
        dates = [
            date(2024, 6, 21),
            date(2024, 3, 15),
            date(2024, 4, 1),
            date(2024, 3, 15),
        ]
        await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=dates,
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )
        call = next(c for c in cur.calls if "WITH ids AS" in c[0] or "FROM ids" in c[0])
        _sql, params = call
        flat = list(params)
        lo, hi = date(2024, 3, 15), date(2024, 6, 21)
        # Both bounds present (each appears twice — once per fact join).
        assert flat.count(lo) >= 2, f"min(dates) {lo} not bound on both joins: {flat}"
        assert flat.count(hi) >= 2, f"max(dates) {hi} not bound on both joins: {flat}"

    async def test_pruning_bound_does_not_change_rows(self):
        """The constant bound is REDUNDANT (correctness-neutral): rows that match
        ``= k.trade_date`` already lie within [min, max], so adding the bound
        returns exactly the same chain.  Proven live (437==437); here we assert
        the in-range rows still come through after the rewrite."""
        d0, d1 = date(2024, 3, 15), date(2024, 3, 18)

        def responder(sql: str, params: Any) -> list[dict]:
            if "asset_class = 'forex'" in sql or "asset_class='forex'" in sql:
                return []
            return [
                _chain_row(d0, "SPX240621C5000", 5000.0, 0.50),
                _chain_row(d1, "SPX240621C5000", 5000.0, 0.48),
            ]

        cur = _FakeCursor(responder)
        reader = SqlOptionsDataReader(_FakePool(cur))  # type: ignore[arg-type]
        result = await reader.query_chain_bulk(
            root="OPT_SP_500",
            dates=[d0, d1],
            type="C",
            expiration_min=date(2024, 6, 21),
            expiration_max=date(2024, 6, 21),
        )
        assert len(result[d0]) == 1 and len(result[d1]) == 1


class TestListExpirationsByDate:
    """Unit contract for ``list_expirations_by_date`` (Issue #2 fix): the
    per-trade-date LISTED-expiration map used by the stream resolver to snap
    NearestToTarget to an expiration actually quoted that day."""

    def _make_reader(self, rows: list[dict]):
        def responder(sql: str, params: Any) -> list[dict]:
            return rows

        cur = _FakeCursor(responder)
        pool = _FakePool(cur)
        reader = SqlOptionsDataReader(pool)  # type: ignore[arg-type]
        return reader, cur, pool

    @pytest.mark.asyncio
    async def test_groups_expirations_by_trade_date(self):
        d1, d2 = date(2021, 1, 5), date(2021, 1, 6)
        rows = [
            {"trade_date": d1, "expiration": date(2021, 1, 29)},
            {"trade_date": d1, "expiration": date(2021, 2, 26)},
            {"trade_date": d2, "expiration": date(2021, 1, 29)},
        ]
        reader, cur, _pool = self._make_reader(rows)
        out = await reader.list_expirations_by_date(
            "OPT_BTC", date(2021, 1, 5), date(2021, 1, 6), option_type="C"
        )
        assert out == {
            d1: [date(2021, 1, 29), date(2021, 2, 26)],
            d2: [date(2021, 1, 29)],
        }
        # Partition-pruning: a CONSTANT trade_date BETWEEN must be in the SQL
        # (a runtime-only join fans out over all yearly partitions -> 60s timeout).
        main_sql = cur.calls[-1][0]
        assert "trade_date BETWEEN" in main_sql
        assert "fact_price_eod" in main_sql

    @pytest.mark.asyncio
    async def test_empty_when_no_rows(self):
        reader, _cur, _pool = self._make_reader([])
        out = await reader.list_expirations_by_date(
            "OPT_BTC", date(2021, 1, 5), date(2021, 1, 6)
        )
        assert out == {}

    @pytest.mark.asyncio
    async def test_expiration_max_pushed_as_upper_bound(self):
        """MINOR-6: when ``expiration_max`` is supplied it is pushed into the dim
        WHERE as ``expiration <= %s`` so the LEAPS scan is bounded, and the
        bound value is passed as a param."""
        reader, cur, _pool = self._make_reader([])
        cap = date(2021, 7, 4)
        await reader.list_expirations_by_date(
            "OPT_BTC",
            date(2021, 1, 5),
            date(2021, 1, 6),
            option_type="C",
            expiration_max=cap,
        )
        sql, params = cur.calls[-1]
        assert "expiration <= %s" in sql
        assert cap in list(params)

    @pytest.mark.asyncio
    async def test_no_upper_bound_when_expiration_max_none(self):
        """Legacy behaviour: no ``expiration_max`` ⇒ no upper-bound predicate."""
        reader, cur, _pool = self._make_reader([])
        await reader.list_expirations_by_date(
            "OPT_BTC", date(2021, 1, 5), date(2021, 1, 6)
        )
        sql, _params = cur.calls[-1]
        assert "expiration <= %s" not in sql
