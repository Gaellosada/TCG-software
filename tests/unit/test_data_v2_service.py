"""Unit tests for the Database v2 backend (no live dwh).

Covers:
  * fact-table dispatch mapping (serie.type -> fact table + fields);
  * the v2 options-continuous resolver selection logic (strike, moneyness,
    delta rejection, false-zero guard, AtExpiry roll);
  * futures-continuous wiring (service composes the real ContinuousSeriesBuilder
    from synthetic ContractPriceData, no DB).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tcg.data._options_continuous_v2 import (
    _front_close_by_date,
    resolve_options_continuous_v2,
)
from tcg.data._sql.instruments_v2 import FACT_DISPATCH, _bounds, _ts_to_int
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


def test_front_close_by_date_picks_nearest_expiration():
    rows = [
        {"ts_int": 20240618, "expiration_int": 20240621, "close": 5495.5},
        {"ts_int": 20240618, "expiration_int": 20240920, "close": 5564.75},
    ]
    m = _front_close_by_date(rows)
    assert m == {20240618: 5495.5}  # smallest expiration >= date wins


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
