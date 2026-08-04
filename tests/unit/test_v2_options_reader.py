"""Unit tests for :class:`tcg.data._v2_compat.options_reader.V2OptionsDataReader`.

No live warehouse: a fake pool captures the SQL and replays canned rows shaped
exactly like the live projection (verified against the real dwh — see the
integration test alongside).

What is worth testing here, and why:

* **Cycle routing, both ERROR shapes.** Silently answering a monthly request
  with weeklies-only would compare two different strategies (spec §3.3 / D4).
* **The ``serie_id`` fan-out.** A contract's ``value`` serie and ``greeks``
  serie are DIFFERENT ``serie_id``s; joining them on a shared id pairs the
  wrong greeks to the wrong price, silently (guardrail Sign 7). Asserted on
  the emitted SQL because the bug is invisible in the result shape.
* **Delta rank ≡ ``match_by_delta``.** The pushdown is only sound if the
  retained set contains the matcher's winner.
* **``mid`` is never fabricated.** Settlement must not leak onto ``mid``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any

import pytest

from tcg.data._v2_compat._mapping import (
    date_int_bounds,
    ew_object_for_cycle,
    expiration_int_from_futures_symbol,
    futures_symbol_from_expiration,
    option_parts_from_symbol,
    option_symbol_from_parts,
    option_type_from_v2,
    option_type_to_v2,
    ts_to_date_int,
    v2_supports_collection,
)
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2MissingCycleFilter,
    V2SymbolError,
    V2UnsupportedCycle,
    V2UnsupportedField,
)
from tcg.data._v2_compat.options_reader import (
    V2OptionsDataReader,
    assert_option_stream_available,
)
from tcg.engine.options.selection._match import match_by_delta

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Fake pool
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    async def execute(self, sql: str, params: Any) -> _FakeCursor:
        self._pool.calls.append((sql, params))
        return _FakeCursor(self._pool.rows)


class _FakePool:
    """Captures every statement and replays one canned row set."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = rows or []
        self.calls: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def connection(self):
        yield _FakeConn(self)

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]


def _chain_row(
    *,
    contract_id: int,
    strike: float,
    delta: float | None,
    ts: datetime = datetime(2026, 6, 1, tzinfo=UTC),
    expiration: date = date(2026, 6, 5),
    option_type: str = "put",
    object_id: int = 11,
    settle: float = 6.2,
) -> dict[str, Any]:
    """One row of the live chain projection."""
    return {
        "ts": ts,
        "contract_id": contract_id,
        "object_id": object_id,
        "expiration": expiration,
        "strike": strike,
        "option_type": option_type,
        "multiplier": 50.0,
        "settle": settle,
        "delta": delta,
        "gamma": 0.001,
        "vega": 1.5,
        "theta": -300.0,
        "implied_vol": 0.21,
        "rn": 1,
    }


# --------------------------------------------------------------------------- #
# Mapping — symbol grammar (spec §2.4)
# --------------------------------------------------------------------------- #
def test_option_symbol_round_trips():
    sym = option_symbol_from_parts(20260605, 7455.0, "P")
    assert sym == "OPT_FUT_SP_500_EMINI_20260605_7455_P"
    assert option_parts_from_symbol(sym) == (20260605, 7455, "P")


def test_option_symbol_accepts_both_type_spellings():
    assert option_symbol_from_parts(20260605, 100, "call").endswith("_100_C")
    assert option_symbol_from_parts(20260605, 100, "C").endswith("_100_C")
    assert option_type_to_v2("P") == "put"
    assert option_type_from_v2("call") == "C"


def test_futures_symbol_round_trips():
    assert futures_symbol_from_expiration(20260619) == "FUT_SP_500_EMINI_20260619"
    assert expiration_int_from_futures_symbol("FUT_SP_500_EMINI_20260619") == 20260619


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "OPT_SP_500_20260605_7455_P",
        "OPT_FUT_SP_500_EMINI_2026065_7455_P",  # 7-digit date
        "OPT_FUT_SP_500_EMINI_20260605_7455.5_P",  # fractional strike
        "OPT_FUT_SP_500_EMINI_20260605_7455",  # missing type
    ],
)
def test_malformed_option_symbols_raise_with_the_offending_value(bad):
    with pytest.raises(V2SymbolError) as exc:
        option_parts_from_symbol(bad)
    assert repr(bad) in str(exc.value) or "not an E-mini option symbol" in str(
        exc.value
    )


def test_v2_supports_only_the_three_mapped_collections():
    assert v2_supports_collection("OPT_SP_500")
    assert v2_supports_collection("FUT_SP_500")
    assert v2_supports_collection("INDEX")
    assert not v2_supports_collection("OPT_VIX")


def test_ts_and_date_bounds_are_utc_and_half_open():
    assert ts_to_date_int(datetime(2026, 6, 5, tzinfo=UTC)) == 20260605
    lo, hi = date_int_bounds(date(2026, 6, 1), date(2026, 6, 5))
    assert lo == datetime(2026, 6, 1, tzinfo=UTC)
    # Half-open: the upper bound is the day AFTER `end`, so `end` is included.
    assert hi == datetime(2026, 6, 6, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Cycle routing (spec §3.2 / §3.3)
# --------------------------------------------------------------------------- #
def test_cycle_routes_to_the_documented_object_ids():
    assert ew_object_for_cycle("W1 Friday") == 11
    assert ew_object_for_cycle("W2 Friday") == 12
    assert ew_object_for_cycle("W3 Friday") == 7  # NOT contiguous with the rest
    assert ew_object_for_cycle("W4 Friday") == 13


@pytest.mark.parametrize("bad", ["M", "", "Q", "D"])
def test_unavailable_cycle_raises_and_names_the_fix(bad):
    with pytest.raises(V2UnsupportedCycle) as exc:
        ew_object_for_cycle(bad)
    msg = str(exc.value)
    assert "W1 Friday" in msg, "the message must name a cycle that WOULD work"
    assert '"v1"' in msg, "the message must offer the v1 fallback"


async def test_monthly_cycle_on_a_chain_read_is_a_hard_error():
    """A UI "M" expands to ("M", "W3 Friday"). Serving only the W3 half would
    silently drop 74,930 monthly contracts — spec §3.3b."""
    reader = V2OptionsDataReader(_FakePool())
    with pytest.raises(V2UnsupportedCycle):
        await reader.query_chain(
            "OPT_SP_500",
            date(2026, 6, 1),
            "P",
            date(2026, 6, 5),
            date(2026, 6, 5),
            expiration_cycle=("M", "W3 Friday"),
        )


async def test_absent_cycle_filter_on_a_chain_read_is_a_hard_error():
    """spec §3.3c — v1 would return monthlies AND weeklies for no filter.

    Raises ``V2MissingCycleFilter`` (E4), NOT ``V2UnsupportedCycle`` (E3).
    E3 names an offending cycle VALUE and interpolates it into its message;
    this case has no value to name, and passing the sentence to E3 nested the
    whole paragraph inside "...expiration cycle \'{cycle}\'...".
    """
    reader = V2OptionsDataReader(_FakePool())
    with pytest.raises(V2MissingCycleFilter) as exc:
        await reader.query_chain_bulk(
            "OPT_SP_500",
            [date(2026, 6, 1)],
            "P",
            date(2026, 6, 5),
            date(2026, 6, 5),
            expiration_cycle=None,
        )
    message = str(exc.value)
    assert "not be comparable" in message
    # Not nested inside E3's wording, and stated exactly once.
    assert "This leg requests expiration cycle" not in message
    assert message.count('Data source "v2" requires an explicit') == 1
    for literal in ("'W1 Friday'", "'W2 Friday'", "'W3 Friday'", "'W4 Friday'"):
        assert literal in message


@pytest.mark.parametrize(
    "call, expected_once",
    [
        (
            lambda r: r.query_chain(
                "FUT_VIX", date(2026, 6, 1), "P", date(2026, 6, 5), date(2026, 6, 5)
            ),
            "does not have data for collection",
        ),
    ],
)
async def test_unsupported_option_root_message_is_not_nested(call, expected_once):
    """The root guard used to pass its whole sentence to a constructor that
    interpolates the COLLECTION NAME, tripling the message."""
    reader = V2OptionsDataReader(_FakePool())
    with pytest.raises(V2CollectionUnavailable) as exc:
        await call(reader)
    message = str(exc.value)
    assert "FUT_VIX" in message
    assert message.count(expected_once) == 1


@pytest.mark.parametrize("stream", ["mid", "volume", "open_interest"])
def test_unavailable_stream_message_is_not_nested(stream):
    with pytest.raises(V2UnsupportedField) as exc:
        assert_option_stream_available(stream)
    message = str(exc.value)
    assert message.count('Data source "v2" has no') == 1
    assert f"no {stream} data" in message


async def test_generic_weekly_tag_routes_to_every_option_object():
    """OPT_SP_500 has zero rows under the literal "W" on BOTH sources, so
    dropping it and serving the union is source-NEUTRAL (spec §3.3a). The union
    of every weekly cycle is every option object — the quarterly object (14),
    which serves the W3 slot in the quarterly months, included."""
    pool = _FakePool()
    reader = V2OptionsDataReader(pool)
    await reader.query_chain(
        "OPT_SP_500",
        date(2026, 6, 1),
        "P",
        date(2026, 6, 5),
        date(2026, 6, 5),
        expiration_cycle=("W", "W1 Friday", "W2 Friday", "W3 Friday", "W4 Friday"),
    )
    objects = [p for p in pool.calls[-1][1] if isinstance(p, list)][0]
    assert sorted(objects) == [7, 11, 12, 13, 14]


async def test_inventory_reads_tolerate_an_absent_cycle():
    """``list_expirations`` has no cycle parameter at all; "everything v2 has"
    is a complete answer to an inventory question, unlike a chain read."""
    pool = _FakePool([{"expiration": date(2026, 6, 5)}])
    reader = V2OptionsDataReader(pool)
    assert await reader.list_expirations("OPT_SP_500") == [date(2026, 6, 5)]


async def test_inventory_reads_still_reject_an_explicit_monthly():
    reader = V2OptionsDataReader(_FakePool())
    with pytest.raises(V2UnsupportedCycle):
        await reader.list_expirations_filtered("OPT_SP_500", "P", "M")


async def test_unmapped_collection_raises():
    reader = V2OptionsDataReader(_FakePool())
    with pytest.raises(V2CollectionUnavailable) as exc:
        await reader.list_expirations("OPT_VIX")
    assert "OPT_VIX" in str(exc.value)


# --------------------------------------------------------------------------- #
# Unavailable streams (spec §4.4 / §11 E5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stream", ["mid", "volume", "open_interest"])
def test_unavailable_streams_raise_rather_than_substituting(stream):
    with pytest.raises(V2UnsupportedField) as exc:
        assert_option_stream_available(stream)
    msg = str(exc.value)
    assert stream in msg
    assert "close" in msg, "the message must name the stream that DOES work"


@pytest.mark.parametrize("stream", ["close", "bs_mid", "iv", "delta"])
def test_available_streams_pass_the_gate(stream):
    assert_option_stream_available(stream)  # must not raise


async def test_settlement_never_leaks_onto_mid():
    """D1: v1's mark is the bid/ask mid, v2's is settlement — they differ by a
    2.64% median. Copying one onto the other would fake agreement."""
    pool = _FakePool([_chain_row(contract_id=1, strike=7455, delta=-0.10)])
    reader = V2OptionsDataReader(pool)
    ((_c, row),) = await reader.query_chain(
        "OPT_SP_500",
        date(2026, 6, 1),
        "P",
        date(2026, 6, 5),
        date(2026, 6, 5),
        expiration_cycle="W1 Friday",
    )
    assert row.close == pytest.approx(6.2)
    assert row.mid is None
    assert row.bid is None and row.ask is None
    assert row.volume is None and row.open_interest is None


# --------------------------------------------------------------------------- #
# The serie_id fan-out (guardrail Sign 7)
# --------------------------------------------------------------------------- #
async def test_greeks_are_joined_through_contract_id_not_serie_id():
    """A contract has a DIFFERENT serie_id per serie.type. Joining the two fact
    tables on a shared serie_id returns zero rows; joining them on an ASSUMED
    shared id would pair the wrong greeks to the wrong price, silently."""
    pool = _FakePool()
    reader = V2OptionsDataReader(pool)
    await reader.query_chain(
        "OPT_SP_500",
        date(2026, 6, 1),
        "P",
        date(2026, 6, 5),
        date(2026, 6, 5),
        expiration_cycle="W1 Friday",
    )
    sql = " ".join(pool.last_sql.split())
    assert "sg.contract_id = sv.contract_id AND sg.type = 'greeks'" in sql
    assert "fg.serie_id = sg.serie_id" in sql
    assert "fv.serie_id = fg.serie_id" not in sql
    assert "fg.serie_id = fv.serie_id" not in sql


async def test_a_contract_day_without_greeks_still_yields_a_row():
    """The greeks side is a LEFT JOIN with its ts bound in the ON clause. If the
    bound moved to the WHERE clause the join would collapse to an inner one and
    greek-less contract-days would vanish, changing the chain size and breaking
    v1's ``missing_delta_no_compute`` classification (spec §11 E9)."""
    pool = _FakePool(
        [
            _chain_row(contract_id=1, strike=7455, delta=None),
            _chain_row(contract_id=2, strike=7460, delta=-0.11),
        ]
    )
    reader = V2OptionsDataReader(pool)
    pairs = await reader.query_chain(
        "OPT_SP_500",
        date(2026, 6, 1),
        "P",
        date(2026, 6, 5),
        date(2026, 6, 5),
        expiration_cycle="W1 Friday",
    )
    assert len(pairs) == 2
    assert pairs[0][1].delta_stored is None
    sql = " ".join(pool.last_sql.split())
    assert "LEFT JOIN tcg_instruments_v2.fact_greeks fg ON" in sql


# --------------------------------------------------------------------------- #
# DTO shape (spec §6.7 / §6.8)
# --------------------------------------------------------------------------- #
async def test_contract_doc_carries_the_v1_shaped_identity():
    pool = _FakePool([_chain_row(contract_id=1, strike=7455, delta=-0.10)])
    reader = V2OptionsDataReader(pool)
    ((contract, row),) = await reader.query_chain(
        "OPT_SP_500",
        date(2026, 6, 1),
        "P",
        date(2026, 6, 5),
        date(2026, 6, 5),
        expiration_cycle="W1 Friday",
    )
    assert contract.collection == "OPT_SP_500"  # never OPT_SP_500_EW1
    assert contract.contract_id == "OPT_FUT_SP_500_EMINI_20260605_7455_P"
    assert contract.root_underlying == "IND_SP_500"
    assert contract.underlying_symbol == "EW1"
    assert contract.expiration_cycle == "W1 Friday"  # never "weekly"
    assert contract.type == "P"
    assert contract.contract_size == 50.0
    assert contract.currency is None  # exact v1 parity — v1 is NULL here
    assert contract.provider == "DATABENTO"
    assert row.date == date(2026, 6, 1)
    # theta/vega are deliberately NOT unit-normalised (spec §5.3).
    assert row.theta_stored == pytest.approx(-300.0)
    assert row.vega_stored == pytest.approx(1.5)
    assert row.underlying_price_stored is None


def test_object_ids_map_to_the_right_underlying_symbol():
    from tcg.data._v2_compat.options_reader import _UNDERLYING_BY_OBJECT

    assert _UNDERLYING_BY_OBJECT == {
        11: "EW1",
        12: "EW2",
        7: "EW3",
        13: "EW4",
        14: "ES",  # the standard quarterly option's true root (display-only)
    }


# --------------------------------------------------------------------------- #
# Capability flags — the engine gates its fast paths on these
# --------------------------------------------------------------------------- #
def test_bulk_capabilities_are_advertised():
    """``_choose_path`` and ``_reader_supports_bulk_multi`` probe BOTH the flag
    and ``callable(getattr(...))``. Failing either drops v2 onto the legacy
    per-date path."""
    reader = V2OptionsDataReader(_FakePool())
    assert reader.supports_bulk_multi is True
    assert reader.supports_held_rows is True
    assert callable(getattr(reader, "query_chain_bulk_multi", None))
    assert callable(getattr(reader, "query_held_rows", None))


def test_the_reader_satisfies_every_protocol_method():
    from tcg.data.options.protocol import OptionsDataReader

    reader = V2OptionsDataReader(_FakePool())
    for name in (n for n in dir(OptionsDataReader) if not n.startswith("_")):
        assert callable(getattr(reader, name, None)), f"missing protocol method {name}"


# --------------------------------------------------------------------------- #
# Delta pushdown (spec §5.2)
# --------------------------------------------------------------------------- #
def _delta_rows() -> list[dict[str, Any]]:
    """A chain whose SQL rank order is already applied (rn ascending)."""
    deltas = [-0.101, -0.098, -0.12, -0.085, None, -0.30]
    return [
        _chain_row(contract_id=100 + i, strike=7400 + 5 * i, delta=d)
        for i, d in enumerate(deltas)
    ]


async def test_pushdown_retains_the_matcher_winner():
    """The pushdown is sound only if ``match_by_delta`` picks the SAME contract
    from the retained top-k as it would from the full chain."""
    pool = _FakePool(_delta_rows())
    reader = V2OptionsDataReader(pool)
    groups = [(date(2026, 6, 5), [date(2026, 6, 1)])]

    full = await reader.query_chain_bulk_multi(
        "OPT_SP_500", "P", groups, expiration_cycle="W1 Friday"
    )
    kept = await reader.query_chain_bulk_multi(
        "OPT_SP_500",
        "P",
        groups,
        expiration_cycle="W1 Friday",
        delta_pushdown=(-0.10, 3),
    )
    d = date(2026, 6, 1)

    def _pick(pairs):
        return match_by_delta(
            pairs,
            [r.delta_stored for _c, r in pairs],
            -0.10,
            1.0,
            False,
            chain_size=len(pairs),
        ).contract.contract_id

    assert len(kept[d]) == 3, "top-k must be truncated to k, not k+1"
    assert _pick(kept[d]) == _pick(full[d])
    # -0.101 is nearest to -0.10.
    assert _pick(kept[d]) == "OPT_FUT_SP_500_EMINI_20260605_7400_P"


async def test_pushdown_ranking_matches_the_shared_python_reference():
    """``symbol_delta_rank`` is the single source of truth the engine's matcher
    is bound to; the retained set must equal what it returns."""
    from tcg.data._sql.options import symbol_delta_rank

    pool = _FakePool(_delta_rows())
    reader = V2OptionsDataReader(pool)
    groups = [(date(2026, 6, 5), [date(2026, 6, 1)])]
    full = await reader.query_chain_bulk_multi(
        "OPT_SP_500", "P", groups, expiration_cycle="W1 Friday"
    )
    kept = await reader.query_chain_bulk_multi(
        "OPT_SP_500",
        "P",
        groups,
        expiration_cycle="W1 Friday",
        delta_pushdown=(-0.10, 3),
    )
    d = date(2026, 6, 1)
    expected = symbol_delta_rank(full[d], -0.10, 3)
    assert {c.contract_id for c, _r in kept[d]} == {c.contract_id for c, _r in expected}


async def test_pushdown_sql_ranks_on_raw_delta_with_nulls_last():
    """COUPLED with ``match_by_delta``: same primary key, same tie-breaks, no
    transform on delta (v2 already uses v1's signed [-1,1] convention)."""
    pool = _FakePool(_delta_rows())
    reader = V2OptionsDataReader(pool)
    await reader.query_chain_bulk_multi(
        "OPT_SP_500",
        "P",
        [(date(2026, 6, 5), [date(2026, 6, 1)])],
        expiration_cycle="W1 Friday",
        delta_pushdown=(-0.10, 3),
    )
    sql = " ".join(pool.last_sql.split())
    assert "PARTITION BY c.expiration, fv.ts" in sql
    assert "ORDER BY abs(fg.delta - %s) ASC NULLS LAST" in sql
    assert "c.strike ASC, c.contract_id ASC" in sql
    # k+1 is fetched so the Python re-rank has a boundary margin.
    assert pool.calls[-1][1][-1] == 4


async def test_all_null_delta_chain_preserves_the_missing_delta_path():
    """An all-NULL chain must still return ROWS, so ``match_by_delta`` reports
    ``missing_delta_no_compute`` exactly as it does on v1."""
    rows = [
        _chain_row(contract_id=200 + i, strike=7400 + 5 * i, delta=None)
        for i in range(3)
    ]
    reader = V2OptionsDataReader(_FakePool(rows))
    kept = await reader.query_chain_bulk_multi(
        "OPT_SP_500",
        "P",
        [(date(2026, 6, 5), [date(2026, 6, 1)])],
        expiration_cycle="W1 Friday",
        delta_pushdown=(-0.10, 2),
    )
    pairs = kept[date(2026, 6, 1)]
    assert pairs
    result = match_by_delta(
        pairs,
        [r.delta_stored for _c, r in pairs],
        -0.10,
        1.0,
        False,
        chain_size=len(pairs),
    )
    assert result.error_code == "missing_delta_no_compute"


# --------------------------------------------------------------------------- #
# Bulk merge determinism (the four-object union)
# --------------------------------------------------------------------------- #
async def test_union_merge_is_ordered_by_contract_id_within_a_date():
    """One logical collection fans out to four EW objects. ``_row_for_contract``
    takes the FIRST matching row, so the merge order must be deterministic and
    identical between the full-chain and pushdown paths."""
    rows = [
        _chain_row(contract_id=900, strike=7400, delta=-0.10, object_id=13),
        _chain_row(contract_id=100, strike=7405, delta=-0.11, object_id=11),
        _chain_row(contract_id=500, strike=7410, delta=-0.12, object_id=12),
    ]
    reader = V2OptionsDataReader(_FakePool(rows))
    groups = [(date(2026, 6, 5), [date(2026, 6, 1)])]
    kept = await reader.query_chain_bulk_multi(
        "OPT_SP_500",
        "P",
        groups,
        expiration_cycle=("W1 Friday", "W2 Friday", "W4 Friday"),
        delta_pushdown=(-0.10, 5),
    )
    got = [c.contract_id for c, _r in kept[date(2026, 6, 1)]]
    # SQL emits ORDER BY ts, contract_id — 100, 500, 900 → strikes 7405/7410/7400.
    assert got == [
        "OPT_FUT_SP_500_EMINI_20260605_7405_P",
        "OPT_FUT_SP_500_EMINI_20260605_7410_P",
        "OPT_FUT_SP_500_EMINI_20260605_7400_P",
    ]


async def test_bulk_preseeds_every_requested_date():
    """A date on which nothing traded must be present with an empty list, not
    absent — callers index it directly."""
    reader = V2OptionsDataReader(_FakePool([]))
    out = await reader.query_chain_bulk(
        "OPT_SP_500",
        [date(2026, 6, 1), date(2026, 6, 2)],
        "P",
        date(2026, 6, 5),
        date(2026, 6, 5),
        expiration_cycle="W1 Friday",
    )
    assert out == {date(2026, 6, 1): [], date(2026, 6, 2): []}


async def test_held_rows_key_on_the_natural_key_not_a_symbol_string():
    """v2 has no ``symbol`` column: the held symbol is decomposed into
    (expiration, strike, option_type), which IS unique on v2 (spec §2.4)."""
    pool = _FakePool([_chain_row(contract_id=1, strike=7455, delta=-0.10)])
    reader = V2OptionsDataReader(pool)
    out = await reader.query_held_rows(
        "OPT_SP_500",
        "P",
        [("OPT_FUT_SP_500_EMINI_20260605_7455_P", date(2026, 6, 1), date(2026, 6, 5))],
        expiration_cycle="W1 Friday",
    )
    assert list(out) == [date(2026, 6, 1)]
    sql = " ".join(pool.last_sql.split())
    assert "h.expiration = c.expiration AND h.strike = c.strike" in sql
    assert "h.option_type = c.option_type" in sql


async def test_held_rows_widens_a_duplicated_symbol_window():
    pool = _FakePool([])
    reader = V2OptionsDataReader(pool)
    sym = "OPT_FUT_SP_500_EMINI_20260605_7455_P"
    await reader.query_held_rows(
        "OPT_SP_500",
        "P",
        [
            (sym, date(2026, 6, 1), date(2026, 6, 2)),
            (sym, date(2026, 6, 4), date(2026, 6, 5)),
        ],
        expiration_cycle="W1 Friday",
    )
    params = pool.calls[-1][1]
    bounds = [p for p in params if isinstance(p, datetime)]
    # One merged window, not two: [2026-06-01, 2026-06-06).
    assert datetime(2026, 6, 1, tzinfo=UTC) in bounds
    assert datetime(2026, 6, 6, tzinfo=UTC) in bounds


async def test_get_contract_raises_when_absent():
    from tcg.types.errors import OptionsContractNotFound

    reader = V2OptionsDataReader(_FakePool([]))
    with pytest.raises(OptionsContractNotFound):
        await reader.get_contract("OPT_SP_500", "OPT_FUT_SP_500_EMINI_20260605_7455_P")


async def test_root_symbol_needs_no_round_trip():
    pool = _FakePool()
    reader = V2OptionsDataReader(pool)
    assert await reader.get_option_root_symbol("OPT_SP_500") == "IND_SP_500"
    assert pool.calls == [], "this exists to AVOID a probe fetch"


# --------------------------------------------------------------------------- #
# Quarterly W3 coverage (v2-quarterly-w3-fix)
#
# The v2 EW3 weekly (object 7) lists a 3rd-Friday contract only in the SERIAL
# months (Jul/Aug/Oct/Nov/Jan). The quarterly-month 3rd Fridays (Mar/Jun/Sep/Dec)
# are carried by the standard quarterly ES option (object 14). "W3 Friday" must
# therefore route to BOTH 7 and 14 so every one of the 12 monthly 3rd Fridays
# resolves exactly one contract. Object 14 is quarterly-only and must never leak
# into W1/W2/W4. (W1 investigation: object 14 = OPT_SP_500_ES, kind='option'.)
# --------------------------------------------------------------------------- #
def _objects_of(call) -> list[int]:
    """Pull the routed object-id list out of a captured (sql, params) call."""
    return [p for p in call[1] if isinstance(p, list) and all(
        isinstance(x, int) for x in p) and p][0]


async def test_w3_route_includes_the_quarterly_object():
    """Serial-month 3rd Fridays come from EW3 (7); quarterly-month 3rd Fridays
    (Mar/Jun/Sep/Dec) come from the standard quarterly option (14). Omitting 14
    is exactly why the quarterly expiries were skipped."""
    pool = _FakePool()
    reader = V2OptionsDataReader(pool)
    await reader.query_chain(
        "OPT_SP_500",
        date(2016, 7, 15),
        "P",
        date(2016, 6, 1),
        date(2017, 3, 31),
        expiration_cycle="W3 Friday",
    )
    objects = _objects_of(pool.calls[-1])
    assert 7 in objects, objects
    assert 14 in objects, objects


@pytest.mark.parametrize("cycle", ["W1 Friday", "W2 Friday", "W4 Friday"])
async def test_quarterly_object_never_leaks_into_other_weekly_cycles(cycle):
    """Object 14 expires ONLY on quarterly 3rd Fridays (the W3 slot). It must
    not appear on any other weekly cycle's route."""
    pool = _FakePool()
    reader = V2OptionsDataReader(pool)
    await reader.query_chain(
        "OPT_SP_500",
        date(2016, 7, 15),
        "P",
        date(2016, 6, 1),
        date(2017, 3, 31),
        expiration_cycle=cycle,
    )
    objects = _objects_of(pool.calls[-1])
    assert 14 not in objects, (cycle, objects)


def test_quarterly_object_is_tagged_as_w3_friday():
    """The quarterly contract must SURFACE under "W3 Friday", so a roll schedule
    picking "W3 Friday" treats it as the 3rd-Friday contract for that month."""
    from tcg.data._v2_compat.options_reader import _CYCLE_BY_OBJECT

    assert _CYCLE_BY_OBJECT[14] == "W3 Friday"


async def test_quarterly_row_emits_the_w3_friday_cycle_tag():
    """A row from object 14 must emit ``expiration_cycle == "W3 Friday"`` so it
    is indistinguishable, to the roll schedule, from an EW3 3rd-Friday pick."""
    pool = _FakePool(
        [
            _chain_row(
                contract_id=1,
                strike=1950,
                delta=-0.1007,
                object_id=14,
                expiration=date(2016, 9, 16),
                ts=datetime(2016, 7, 15, tzinfo=UTC),
            )
        ]
    )
    reader = V2OptionsDataReader(pool)
    ((contract, _row),) = await reader.query_chain(
        "OPT_SP_500",
        date(2016, 7, 15),
        "P",
        date(2016, 9, 16),
        date(2016, 9, 16),
        expiration_cycle="W3 Friday",
    )
    assert contract.expiration_cycle == "W3 Friday"
    assert contract.expiration == date(2016, 9, 16)


async def test_get_contract_searches_the_quarterly_object():
    """A quarterly symbol won't resolve unless object 14 is in the searched set
    (``get_contract`` has no cycle parameter)."""
    pool = _FakePool()
    reader = V2OptionsDataReader(pool)
    try:
        await reader.get_contract(
            "OPT_SP_500", "OPT_FUT_SP_500_EMINI_20160916_1950_P"
        )
    except Exception:  # noqa: BLE001 — empty fake pool => NotFound; we want the SQL
        pass
    objects = _objects_of(pool.calls[-1])
    assert 14 in objects, objects


async def test_inventory_and_coverage_paths_include_the_quarterly_object():
    """Existence/coverage answers must count the quarterly object so inventory
    is honest — but this is NOT cycle routing, so W1/W2/W4 are unaffected."""
    pool = _FakePool([{"exp_first": None, "exp_last": None, "n": 0}])
    reader = V2OptionsDataReader(pool)
    # list_roots issues a contract count then a coverage query; both must span 14.
    try:
        await reader.list_roots()
    except Exception:  # noqa: BLE001 — coverage sub-call on the tiny fake may error
        pass
    seen_14 = any(
        14 in ([p for p in c[1] if isinstance(p, list)] or [[]])[0]
        for c in pool.calls
        if any(isinstance(p, list) for p in c[1])
    )
    assert seen_14, pool.calls
