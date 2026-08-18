"""Unit tests for the Database v2 backend (no live dwh).

Covers:
  * fact-table dispatch mapping (serie.type -> fact table + fields);
  * the v2 options-continuous resolver selection logic (strike, moneyness,
    delta rejection, false-zero guard, AtExpiry roll);
  * futures-continuous wiring (service composes the real ContinuousSeriesBuilder
    from synthetic ContractPriceData, no DB).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pytest

from tcg.data._options_continuous_v2 import (
    _front_close_by_date,
    resolve_options_continuous_v2,
)
from tcg.data._sql.instruments_v2 import (
    FACT_DISPATCH,
    _bounds,
    _ts_to_int,
    _ts_to_iso,
    grain_for_freq,
)
from tcg.data.service_v2 import DefaultMarketDataServiceV2
from tcg.types.errors import ValidationError
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContractPriceData,
    PriceSeries,
    RollStrategy,
)


# --------------------------------------------------------------------------- #
# Fact-table dispatch
# --------------------------------------------------------------------------- #
def test_fact_dispatch_maps_each_type_to_one_table():
    assert FACT_DISPATCH["bar"][0] == "fact_bar"
    assert FACT_DISPATCH["value"][0] == "fact_value"
    assert FACT_DISPATCH["greeks"][0] == "fact_greeks"
    assert FACT_DISPATCH["bbba"][0] == "fact_bbba"
    # bar carries OHLCV + open_interest; value carries just value.
    assert FACT_DISPATCH["bar"][1] == (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    )
    assert FACT_DISPATCH["value"][1] == ("value",)
    assert FACT_DISPATCH["greeks"][1] == (
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "implied_vol",
    )
    assert FACT_DISPATCH["bbba"][1] == (
        "best_bid_value",
        "best_bid_volume",
        "best_ask_value",
        "best_ask_volume",
    )
    # Exactly the four schema types, no extras.
    assert set(FACT_DISPATCH) == {"bar", "value", "greeks", "bbba"}


def test_ts_to_int_and_bounds():
    from datetime import datetime, timezone

    assert _ts_to_int(datetime(2024, 6, 18, 0, 0, tzinfo=timezone.utc)) == 20240618
    lower, upper = _bounds(date(2024, 1, 1), date(2024, 6, 18))
    assert lower == date(2024, 1, 1)
    # upper is exclusive = end + 1 day so an inclusive end date is captured.
    assert upper == date(2024, 6, 19)


def test_grain_for_freq_daily_is_date_grain():
    assert grain_for_freq("daily") == "daily"


def test_grain_for_freq_minute_is_intraday():
    assert grain_for_freq("1m") == "intraday"


def test_grain_for_freq_unknown_defaults_to_intraday():
    # Deliberate: emitting a full timestamp is lossless, collapsing one to a
    # date destroys information. A future '5m'/'1h' must not silently collapse.
    assert grain_for_freq("5m") == "intraday"
    assert grain_for_freq(None) == "intraday"


def test_ts_to_iso_normalises_to_utc_z():
    ts = datetime(2026, 3, 12, 14, 31, tzinfo=timezone.utc)
    assert _ts_to_iso(ts) == "2026-03-12T14:31:00Z"


def test_ts_to_iso_treats_naive_as_utc():
    assert _ts_to_iso(datetime(2026, 3, 12, 14, 31)) == "2026-03-12T14:31:00Z"


# --------------------------------------------------------------------------- #
# Options-continuous resolver
# --------------------------------------------------------------------------- #
class _FakeReaderOptions:
    """Fake v2 reader for the resolver: canned settlements + future closes."""

    def __init__(self, settlements, future_rows):
        self._settlements = settlements
        self._future_rows = future_rows

    async def fetch_option_settlements(self, object_id, option_type, *, start, end):
        return [s for s in self._settlements if s["option_type"] == option_type]

    async def fetch_option_expirations(self, object_id, option_type):
        # Mirrors the real reader: distinct contract expirations for this
        # option_type from the contract dimension — GLOBAL (across all dates),
        # independent of any single date's settlement availability.
        exps = {
            s["expiration_int"]
            for s in self._settlements
            if s["option_type"] == option_type
        }
        return sorted(exps)

    async def fetch_future_front_closes(self, object_id, *, start, end):
        return list(self._future_rows)


def _settlement(ts, exp, strike, value, ot="put", cid=None, code=None):
    return {
        "ts_int": ts,
        "contract_id": cid if cid is not None else int(f"{strike:.0f}"),
        "contract_code": code or f"C{strike:.0f}.{exp}",
        "expiration_int": exp,
        "strike": strike,
        "value": value,
        "option_type": ot,
    }


_OBJ = {"object_id": 7, "kind": "option", "underlying_object_id": 6}


async def test_resolver_strike_picks_nearest_strike():
    settlements = [
        _settlement(20240618, 20240621, 5000.0, 0.25),
        _settlement(20240618, 20240621, 5495.0, 2.70),
        _settlement(20240618, 20240621, 5500.0, 3.10),
    ]
    reader = _FakeReaderOptions(settlements, [])
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=None,
        end=None,
    )
    assert res.dates == (20240618,)
    assert res.values == (0.25,)
    assert res.contracts == ("C5000.20240621",)


async def test_resolver_moneyness_uses_front_future_spot():
    # spot = 5495.5 (front future close), moneyness 1.0 -> target strike 5495.5
    settlements = [
        _settlement(20240618, 20240621, 5000.0, 0.25),
        _settlement(20240618, 20240621, 5495.0, 2.70),
        _settlement(20240618, 20240621, 5500.0, 3.10),
    ]
    future_rows = [
        {"ts_int": 20240618, "expiration_int": 20240621, "close": 5495.5},
        {"ts_int": 20240618, "expiration_int": 20240920, "close": 5564.75},
    ]
    reader = _FakeReaderOptions(settlements, future_rows)
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="moneyness",
        target=1.0,
        option_type="put",
        start=None,
        end=None,
    )
    assert res.values == (2.70,)  # strike 5495 nearest to 5495.5
    assert res.contracts == ("C5495.20240621",)


async def test_resolver_rejects_delta():
    reader = _FakeReaderOptions([], [])
    with pytest.raises(ValidationError, match="greeks"):
        await resolve_options_continuous_v2(
            reader,
            _OBJ,
            criterion="delta",
            target=0.1,
            option_type="put",
            start=None,
            end=None,
        )


async def test_resolver_false_zero_settlement_dropped():
    # The nearest-strike contract has a false-zero settlement; it must be
    # dropped (not plotted as zero) and the next usable strike chosen.
    settlements = [
        _settlement(20240618, 20240621, 5000.0, 0.0),  # false zero
        _settlement(20240618, 20240621, 5100.0, 1.5),
    ]
    reader = _FakeReaderOptions(settlements, [])
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=None,
        end=None,
    )
    # 5000 dropped -> nearest usable is 5100.
    assert res.values == (1.5,)
    assert res.contracts == ("C5100.20240621",)


async def test_resolver_atexpiry_roll_records_boundary():
    # Two dates: first holds the near expiry, second (after it expired) holds
    # the next expiry -> one roll recorded on the second date.
    settlements = [
        _settlement(20240620, 20240621, 5000.0, 2.0, code="NEAR"),
        _settlement(20240620, 20240719, 5000.0, 9.0, code="FAR"),
        # 2024-06-25 is AFTER the near expiry (2024-06-21) -> near is dead.
        _settlement(20240625, 20240719, 5000.0, 8.0, code="FAR"),
    ]
    reader = _FakeReaderOptions(settlements, [])
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=None,
        end=None,
    )
    assert res.dates == (20240620, 20240625)
    assert res.values == (2.0, 8.0)  # near on d1, far on d2
    assert res.roll_dates == (20240625,)
    assert res.contracts == ("NEAR", "FAR")
    # per-date codes are 1:1 with dates (here == distinct list, no drift)
    assert res.contract_codes == ("NEAR", "FAR")


def _roll_marker_labels(res):
    """Replicate the frontend roll-marker labeling from the resolver output.

    Mirrors ``ContinuousOptionsChartV2.jsx``: for each roll date, locate its
    index ``i`` in ``dates`` and read the per-date contract code on the sell bar
    (``i-1``) and the buy bar (``i``). Returns ``[(sell_code, buy_code), ...]``.
    """
    labels = []
    for rd in res.roll_dates:
        i = res.dates.index(rd)
        labels.append((res.contract_codes[i - 1], res.contract_codes[i]))
    return labels


async def test_resolver_multi_roll_contract_codes_align_to_each_roll():
    # Three expiry segments (strike criterion) => two real rolls. Each roll
    # marker must be labeled with the contract actually held on its sell bar
    # (i-1) and buy bar (i). This is the case thin live data (1 settlement date,
    # 0 rolls) cannot exercise.
    settlements = [
        _settlement(20240110, 20240119, 5000.0, 2.0, code="A5000"),
        _settlement(20240115, 20240119, 5000.0, 1.5, code="A5000"),
        # 2024-01-22: expiry A (0119) is dead -> front is B (0216); C also listed
        _settlement(20240122, 20240216, 5000.0, 5.0, code="B5000"),
        _settlement(20240122, 20240315, 5000.0, 9.0, code="C5000"),
        _settlement(20240205, 20240216, 5000.0, 4.0, code="B5000"),
        # 2024-02-20: expiry B (0216) is dead -> front is C (0315)
        _settlement(20240220, 20240315, 5000.0, 6.0, code="C5000"),
        _settlement(20240301, 20240315, 5000.0, 5.5, code="C5000"),
    ]
    reader = _FakeReaderOptions(settlements, [])
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=None,
        end=None,
    )
    assert res.dates == (
        20240110,
        20240115,
        20240122,
        20240205,
        20240220,
        20240301,
    )
    assert res.roll_dates == (20240122, 20240220)
    assert res.contract_codes == (
        "A5000",
        "A5000",
        "B5000",
        "B5000",
        "C5000",
        "C5000",
    )
    # Each roll marker labeled with the exact contract active before/after it.
    assert _roll_marker_labels(res) == [
        ("A5000", "B5000"),  # roll 2024-01-22
        ("B5000", "C5000"),  # roll 2024-02-20
    ]


async def test_resolver_moneyness_intra_segment_drift_roll_label_is_correct():
    # Moneyness re-selects the strike per date, so within ONE expiry segment the
    # chosen contract drifts as spot moves. This proves the per-date label is
    # correct where a per-segment / first-seen list would be WRONG.
    #   Segment A (exp 0119): spot 5000 -> 5100 -> 5200, ATM strike follows.
    #   Roll to segment B (exp 0315) on 0122 at spot 5200.
    future_rows = [
        {"ts_int": 20240110, "expiration_int": 20241220, "close": 5000.0},
        {"ts_int": 20240112, "expiration_int": 20241220, "close": 5100.0},
        {"ts_int": 20240115, "expiration_int": 20241220, "close": 5200.0},
        {"ts_int": 20240122, "expiration_int": 20241220, "close": 5200.0},
    ]
    settlements = [
        # 0110 spot 5000 -> ATM 5000
        _settlement(20240110, 20240119, 4950.0, 1.0, code="A4950"),
        _settlement(20240110, 20240119, 5000.0, 1.2, code="A5000"),
        _settlement(20240110, 20240119, 5050.0, 1.4, code="A5050"),
        # 0112 spot 5100 -> ATM 5100
        _settlement(20240112, 20240119, 5050.0, 1.1, code="A5050"),
        _settlement(20240112, 20240119, 5100.0, 1.3, code="A5100"),
        _settlement(20240112, 20240119, 5150.0, 1.5, code="A5150"),
        # 0115 spot 5200 -> ATM 5200  (still segment A)
        _settlement(20240115, 20240119, 5150.0, 1.2, code="A5150"),
        _settlement(20240115, 20240119, 5200.0, 1.4, code="A5200"),
        _settlement(20240115, 20240119, 5250.0, 1.6, code="A5250"),
        # 0122 spot 5200 -> ATM 5200, segment A dead -> segment B
        _settlement(20240122, 20240315, 5150.0, 7.0, code="B5150"),
        _settlement(20240122, 20240315, 5200.0, 8.0, code="B5200"),
    ]
    reader = _FakeReaderOptions(settlements, future_rows)
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="moneyness",
        target=1.0,
        option_type="put",
        start=None,
        end=None,
    )
    assert res.dates == (20240110, 20240112, 20240115, 20240122)
    assert res.contract_codes == ("A5000", "A5100", "A5200", "B5200")
    assert res.roll_dates == (20240122,)
    # Correct label: sold the drifted A5200 (last bar of segment A), bought B5200.
    assert _roll_marker_labels(res) == [("A5200", "B5200")]
    # Guard against regressing to the old de-duped/per-segment indexing, which
    # would have mislabeled this roll as sell=A5000, buy=A5100 (both wrong).
    distinct = res.contracts  # first-seen order
    assert (distinct[0], distinct[1]) == ("A5000", "A5100")
    assert _roll_marker_labels(res) != [(distinct[0], distinct[1])]


async def test_resolver_front_hole_follows_contract_chain_not_settlements():
    # The TRUE front expiration (A, exp 0119) has a settlement data HOLE on
    # 2024-01-12 (no usable row that day) while the NEXT expiration (B, exp
    # 0216) already has data. The active/front expiration must be taken from the
    # contract chain (A is still the calendar front on 0112), so the hole date is
    # simply dropped — NOT rolled to B and then rolled back to A on 0115.
    #
    # Under the OLD settlement-derived logic, 0112 would select B (min expiration
    # among that day's usable settlements) → a spurious roll A->B on 0112 and a
    # non-monotonic roll B->A on 0115, giving roll_dates (0112, 0115, 0122).
    # The chain-derived logic yields a single monotonic roll on 0122.
    settlements = [
        _settlement(20240110, 20240119, 5000.0, 2.0, code="A5000"),
        # 2024-01-12: A (front) has a hole; only B is present that day.
        _settlement(20240112, 20240216, 5000.0, 5.0, code="B5000"),
        # 2024-01-15: A is back.
        _settlement(20240115, 20240119, 5000.0, 1.5, code="A5000"),
        # 2024-01-22: A (0119) has expired -> real roll to B (0216).
        _settlement(20240122, 20240216, 5000.0, 4.0, code="B5000"),
    ]
    reader = _FakeReaderOptions(settlements, [])
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=None,
        end=None,
    )
    # 0112 dropped (front A had a hole); no spurious/backward roll.
    assert res.dates == (20240110, 20240115, 20240122)
    assert res.contract_codes == ("A5000", "A5000", "B5000")
    assert res.roll_dates == (20240122,)
    # Roll boundaries are monotonically increasing (never roll backward).
    assert list(res.roll_dates) == sorted(res.roll_dates)
    assert len(res.roll_dates) == 1


def test_front_close_by_date_picks_nearest_expiration():
    rows = [
        {"ts_int": 20240618, "expiration_int": 20240621, "close": 5495.5},
        {"ts_int": 20240618, "expiration_int": 20240920, "close": 5564.75},
    ]
    m = _front_close_by_date(rows)
    assert m == {20240618: 5495.5}  # smallest expiration >= date wins


# --------------------------------------------------------------------------- #
# Service response shaping + error paths (fake reader, no DB)
# --------------------------------------------------------------------------- #
from tcg.types.errors import DataNotFoundError  # noqa: E402


class _FakeReaderService:
    """Fake v2 reader for service-level shaping/error tests."""

    def __init__(
        self,
        *,
        obj=None,
        serie=None,
        facts=None,
        series=None,
        facets=None,
        filtered=None,
    ):
        self._obj = obj
        self._serie = serie
        self._facts = facts or ("daily", [], {})
        self._series = series or []
        self._facets_data = facets
        self._filtered = filtered if filtered is not None else ([], 0)
        self.last_freq = None
        self.last_facets_object_id = None
        self.filter_calls = []
        # Whether the unbounded whole-object series read was actually issued.
        # ``get_object_detail`` must not issue it — see
        # ``test_get_object_detail_ships_metadata_only``.
        #
        # There is deliberately no ``contracts_calls`` counterpart: the real
        # reader has no ``list_contracts`` any more, so this fake must not
        # either. A fake carrying methods the real class lacks lets a test pass
        # against code that would ``AttributeError`` in production.
        self.series_calls = []

    async def get_object(self, object_id):
        return self._obj

    async def get_serie(self, serie_id):
        return self._serie

    async def list_series(self, object_id):
        self.series_calls.append(object_id)
        return list(self._series)

    async def read_serie_facts(
        self, serie_id, serie_type, *, freq=None, start=None, end=None
    ):
        self.last_freq = freq
        return self._facts

    async def fetch_object_facets(self, object_id):
        self.last_facets_object_id = object_id
        return self._facets_data

    async def list_series_filtered(self, object_id, **kwargs):
        self.filter_calls.append((object_id, kwargs))
        return self._filtered


def _make_service(reader):
    svc = DefaultMarketDataServiceV2.__new__(DefaultMarketDataServiceV2)
    svc._reader = reader
    from tcg.data._rolling import ContinuousSeriesBuilder

    svc._roller = ContinuousSeriesBuilder()
    return svc


_EW2_OBJECT = {
    "object_id": 12,
    "kind": "option",
    "symbol": "OPT_SP_500_EW2",
    "name": "S&P 500 E-mini EW2 Weekly Options (CME)",
    "cycle": "weekly",
    "underlying_object_id": 6,
}


async def test_get_object_detail_ships_metadata_only():
    """The 38 MB payload is gone: bulk lists moved to the paginated endpoint.

    The fake is deliberately loaded with a serie so this cannot pass vacuously —
    under the old implementation the ``series`` key is present and populated.

    ``reader.series_calls == []`` is the load-bearing line, and the only one that
    discriminates the regression that matters. The brief prescribed ``"contracts"
    not in out`` / ``"series" not in out``; those hold for an empty dict, a
    malformed response or an error payload, so the exact-equality form replaced
    them. But note that even the exact-equality form is blind to the real trap: a
    version that fetches both lists and then discards them returns precisely
    ``{"object": obj}`` while still paying the whole 36 s. Only the call counter
    sees that.

    There is no ``contracts_calls`` counterpart because ``list_contracts`` has
    been deleted from the reader. That protection is now structural rather than
    tested: the equivalent regression cannot be written at all — it raises
    ``AttributeError`` instead of silently costing 36 s — which is strictly
    stronger than an assertion. An assertion here would read as protection while
    protecting nothing, so it is gone rather than kept for symmetry.
    """
    reader = _FakeReaderService(
        obj=_EW2_OBJECT,
        series=[{"serie_id": 9, "contract_id": 1, "type": "bbba"}],
    )
    out = await _make_service(reader).get_object_detail(12)
    assert out == {"object": _EW2_OBJECT}
    assert out["object"]["object_id"] == 12
    assert reader.series_calls == []


async def test_get_object_detail_missing_object_raises_404():
    svc = _make_service(_FakeReaderService(obj=None))
    with pytest.raises(DataNotFoundError):
        await svc.get_object_detail(999)


# --------------------------------------------------------------------------- #
# Object facets (the filter form's aggregate)
#
# The reader is faked out here, so these only pin what the SERVICE adds:
# existence-checking the object, stamping ``object_id``/``kind``, and splicing
# the reader's aggregate through untouched. The aggregate's own shaping
# (isoformat, Decimal -> float, sorted option types, the series total) is pinned
# against the REAL reader in
# ``tests/unit/data/sql/test_sql_instruments_v2_facets.py``.
# --------------------------------------------------------------------------- #
_EW2_FACETS = {
    "expirations": [{"expiration": "2026-09-11", "contracts": 146}],
    "strike_min": 15.0,
    "strike_max": 10600.0,
    "option_types": ["call", "put"],
    "serie_types": [
        {"type": "bar", "freq": "1m", "series": 96106},
        {"type": "bbba", "freq": "1m", "series": 96106},
    ],
    "totals": {"contracts": 96106, "series": 200672},
}


async def test_get_object_facets_returns_object_kind_and_facets():
    reader = _FakeReaderService(obj=_EW2_OBJECT, facets=_EW2_FACETS)
    out = await _make_service(reader).get_object_facets(12)
    assert out["object_id"] == 12
    assert out["kind"] == "option"
    assert out["totals"]["series"] == 200672
    assert out["option_types"] == ["call", "put"]
    # Every facet key the frontend filter form reads must survive the splice —
    # a service that cherry-picked a subset would still pass the asserts above.
    assert set(out) == {"object_id", "kind", *_EW2_FACETS}


async def test_get_object_facets_queries_the_requested_object():
    """The aggregate must be read for the id the caller asked for.

    Guards a plausible confusion between the route's ``object_id`` and something
    derived from the object row; here the two differ, so passing the wrong one is
    visible.
    """
    reader = _FakeReaderService(obj={**_EW2_OBJECT, "object_id": 999}, facets={})
    out = await _make_service(reader).get_object_facets(12)
    assert reader.last_facets_object_id == 12
    assert out["object_id"] == 12


async def test_get_object_facets_unknown_object_raises_not_found():
    reader = _FakeReaderService(obj=None, facets=_EW2_FACETS)
    with pytest.raises(DataNotFoundError):
        await _make_service(reader).get_object_facets(999)


# --------------------------------------------------------------------------- #
# Filtered, paginated series page (the facets form's other half)
#
# Reader faked out, so these pin only what the SERVICE adds: existence-checking
# the object, forwarding every filter unaltered, and wrapping the reader's
# ``(rows, total)`` into the PaginatedResult shape. The SQL's own semantics
# (WHERE / ORDER BY / LIMIT-OFFSET) are pinned in
# ``tests/unit/data/sql/test_sql_instruments_v2_series_page.py`` (text + shaping)
# and executed for real in
# ``tests/integration/data/test_instruments_v2_integration.py``.
# --------------------------------------------------------------------------- #
_EW2_PAGE_ROWS = [
    {
        "serie_id": 1433194,
        "contract_id": 77,
        "type": "bbba",
        "freq": "1m",
        "source": "DATABENTO:GLBX.MDP3:bbo-1m",
        "contract_code": "EW2H6 P6260.20260313",
        "expiration": "2026-03-13",
        "strike": 6260.0,
        "option_type": "put",
    }
]


async def test_list_object_series_returns_paginated_shape():
    reader = _FakeReaderService(obj=_EW2_OBJECT, filtered=(_EW2_PAGE_ROWS, 195))
    svc = _make_service(reader)
    out = await svc.list_object_series(12, serie_type="bbba", skip=0, limit=50)
    assert out["total"] == 195
    assert out["skip"] == 0
    assert out["limit"] == 50
    assert len(out["items"]) == 1
    assert out["items"][0]["contract_code"] == "EW2H6 P6260.20260313"
    # No key silently added or dropped — Task 5's client destructures all four.
    assert set(out) == {"items", "total", "skip", "limit"}


async def test_list_object_series_forwards_every_filter():
    reader = _FakeReaderService(obj=_EW2_OBJECT, filtered=(_EW2_PAGE_ROWS, 195))
    svc = _make_service(reader)
    out = await svc.list_object_series(
        12,
        expiration_min=date(2026, 3, 1),
        expiration_max=date(2026, 3, 31),
        strike_min=6000.0,
        strike_max=7000.0,
        option_type="put",
        serie_type="bbba",
        freq="1m",
        skip=50,
        limit=100,
    )
    object_id, kwargs = reader.filter_calls[0]
    assert object_id == 12
    # Every filter, not a subset: a forwarder that dropped or transposed any one
    # of these would narrow (or widen) the page silently. The min/max values
    # differ pairwise so a transposition is visible.
    assert kwargs == {
        "expiration_min": date(2026, 3, 1),
        "expiration_max": date(2026, 3, 31),
        "strike_min": 6000.0,
        "strike_max": 7000.0,
        "option_type": "put",
        "serie_type": "bbba",
        "freq": "1m",
        "skip": 50,
        "limit": 100,
    }
    # skip/limit are ECHOED from the request, not hardcoded to the defaults.
    # test_list_object_series_returns_paginated_shape passes skip=0/limit=50 —
    # exactly the defaults — so only a non-default pair discriminates here.
    assert out["skip"] == 50
    assert out["limit"] == 100


async def test_list_object_series_unknown_object_raises_not_found():
    reader = _FakeReaderService(obj=None, filtered=(_EW2_PAGE_ROWS, 195))
    with pytest.raises(DataNotFoundError):
        await _make_service(reader).list_object_series(999)


async def test_list_object_series_empty_result_is_not_an_error():
    """A narrow filter is a result, not an error."""
    reader = _FakeReaderService(obj=_EW2_OBJECT, filtered=([], 0))
    out = await _make_service(reader).list_object_series(12, strike_min=999_999.0)
    assert out["items"] == []
    assert out["total"] == 0


async def test_get_series_bar_type_dispatches_bar_fields():
    facts = (
        "daily",
        [20240102, 20240103],
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.5, 2.5],
            "volume": [10.0, 20.0],
            "open_interest": [5.0, 6.0],
        },
    )
    reader = _FakeReaderService(serie={"serie_id": 5, "type": "bar"}, facts=facts)
    svc = _make_service(reader)
    out = await svc.get_series(5)
    assert out["type"] == "bar"
    assert out["fields"] == list(FACT_DISPATCH["bar"][1])
    assert out["points"]["ts"] == [20240102, 20240103]
    assert out["points"]["close"] == [1.5, 2.5]
    assert out["points"]["open_interest"] == [5.0, 6.0]


async def test_get_series_value_type_dispatches_value_field():
    reader = _FakeReaderService(
        serie={"serie_id": 8, "type": "value"},
        facts=("daily", [20240102], {"value": [42.0]}),
    )
    svc = _make_service(reader)
    out = await svc.get_series(8)
    assert out["type"] == "value"
    assert out["fields"] == ["value"]
    assert out["points"] == {"ts": [20240102], "value": [42.0]}


async def test_get_series_empty_stream_ok():
    reader = _FakeReaderService(
        serie={"serie_id": 8, "type": "value"}, facts=("daily", [], {"value": []})
    )
    svc = _make_service(reader)
    out = await svc.get_series(8)
    assert out["points"] == {"ts": [], "value": []}


async def test_get_series_missing_serie_raises_404():
    svc = _make_service(_FakeReaderService(serie=None))
    with pytest.raises(DataNotFoundError):
        await svc.get_series(404)


@pytest.mark.parametrize(
    ("freq", "grain", "ts"),
    [
        ("daily", "daily", [20240102]),
        ("1m", "intraday", ["2026-06-01T14:31:00Z"]),
    ],
)
async def test_get_series_forwards_freq_and_echoes_grain(freq, grain, ts):
    """The service's two jobs at this seam: pass ``serie.freq`` DOWN to the reader
    (that is what selects the grain) and surface the returned ``grain`` UP in the
    response, whatever it is.

    Both grains are exercised because a service that hardcoded or mis-derived one
    of them would still satisfy the other, and Task 8 dispatches the chart axis on
    exactly this string. Echoing is the whole contract here: hardcoding
    ``"intraday"`` fails the daily case and vice versa.

    It deliberately asserts nothing about how a ts is rendered — with the reader
    faked out, any such assertion would only re-read this test's own literals.
    Grain resolution itself is covered against the REAL reader in
    ``tests/unit/data/sql/test_sql_instruments_v2_grain.py``.
    """
    reader = _FakeReaderService(
        serie={
            "serie_id": 1,
            "object_id": 16,
            "contract_id": 42,
            "type": "value",
            "freq": freq,
            "source": "TEST",
        },
        facts=(grain, ts, {"value": [610.5]}),
    )
    out = await _make_service(reader).get_series(1)
    assert reader.last_freq == freq
    assert out["grain"] == grain


async def test_get_continuous_options_rejects_non_option():
    reader = _FakeReaderService(
        obj={"object_id": 6, "kind": "future", "symbol": "FUT_SP_500"}
    )
    svc = _make_service(reader)
    with pytest.raises(ValidationError, match="not an option"):
        await svc.get_continuous_options(
            6, criterion="strike", target=5000.0, option_type="put"
        )


async def test_get_continuous_options_missing_object_raises_404():
    svc = _make_service(_FakeReaderService(obj=None))
    with pytest.raises(DataNotFoundError):
        await svc.get_continuous_options(
            999, criterion="strike", target=5000.0, option_type="put"
        )


async def test_resolver_empty_settlements_yields_empty_stream():
    reader = _FakeReaderOptions([], [])
    res = await resolve_options_continuous_v2(
        reader,
        _OBJ,
        criterion="strike",
        target=5000.0,
        option_type="put",
        start=None,
        end=None,
    )
    assert res.dates == ()
    assert res.values == ()
    assert res.roll_dates == ()
    assert res.contracts == ()
    assert res.contract_codes == ()


async def test_resolver_rejects_non_positive_strike():
    reader = _FakeReaderOptions([], [])
    with pytest.raises(ValidationError, match="Strike target must be > 0"):
        await resolve_options_continuous_v2(
            reader,
            _OBJ,
            criterion="strike",
            target=0.0,
            option_type="put",
            start=None,
            end=None,
        )


# --------------------------------------------------------------------------- #
# Futures-continuous wiring (real roller, fake reader)
# --------------------------------------------------------------------------- #
def _contract(code, exp, closes, start_int):
    n = len(closes)
    dates = np.array([start_int + i for i in range(n)], dtype=np.int64)
    arr = np.array(closes, dtype=np.float64)
    return ContractPriceData(
        contract_id=code,
        expiration=exp,
        expiration_cycle="quarterly",
        prices=PriceSeries(
            dates=dates,
            open=arr,
            high=arr,
            low=arr,
            close=arr,
            volume=np.zeros(n, dtype=np.float64),
        ),
    )


class _FakeReaderFutures:
    def __init__(self, obj, contracts):
        self._obj = obj
        self._contracts = contracts

    async def get_object(self, object_id):
        return self._obj

    async def fetch_future_contract_bars(self, object_id, object_cycle):
        return list(self._contracts)


async def test_service_futures_continuous_wires_roller(monkeypatch):
    obj = {"object_id": 6, "kind": "future", "symbol": "FUT_TEST", "cycle": "quarterly"}
    contracts = [
        _contract("C1", 20240115, [10.0, 11.0, 12.0], 20240110),
        _contract("C2", 20240415, [12.5, 13.0, 14.0], 20240113),
    ]
    svc = DefaultMarketDataServiceV2.__new__(DefaultMarketDataServiceV2)
    from tcg.data._rolling import ContinuousSeriesBuilder

    svc._reader = _FakeReaderFutures(obj, contracts)
    svc._roller = ContinuousSeriesBuilder()

    cfg = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.NONE
    )
    res = await svc.get_continuous_future(6, cfg)
    assert res is not None
    assert res.collection == "FUT_TEST"
    assert len(res.prices) > 0
    # Continuous series must span into the second contract's data.
    assert "C2" in res.contracts


async def test_service_futures_continuous_rejects_non_future():
    obj = {"object_id": 5, "kind": "index", "symbol": "IND", "cycle": None}
    svc = DefaultMarketDataServiceV2.__new__(DefaultMarketDataServiceV2)
    svc._reader = _FakeReaderFutures(obj, [])
    from tcg.data._rolling import ContinuousSeriesBuilder

    svc._roller = ContinuousSeriesBuilder()
    cfg = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.NONE
    )
    with pytest.raises(ValidationError, match="not a future"):
        await svc.get_continuous_future(5, cfg)
