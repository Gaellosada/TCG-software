"""P1 — modular hedge resolver dispatch (spot / any-future) + VX1 back-compat.

DB-free.  Drives :func:`tcg.core.api._series_fetch.resolve_hedge_raw` with a fake
``svc`` that RECORDS its calls, proving:

  * a legacy VX1 ``DeltaHedgeSpec`` (via the back-compat shim / migrator) resolves
    the hedge price through ``get_continuous`` with a ``ContinuousRollConfig`` EQUAL
    to the legacy hard-wire (front-month, difference) — byte-identical fetch;
  * a NON-VIX continuous future hedges via ``get_continuous`` on ITS collection;
  * a SPOT hedge resolves via ``get_prices`` (never ``get_continuous``);
  * ``gate_collection=None`` ⇒ no gate fetch, gate pair is ``None`` (always-on).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tcg.core.api._series_fetch import resolve_delta_hedge_raw, resolve_hedge_raw
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    PriceSeries,
    RollStrategy,
)
from tcg.types.options import ByDelta, NearestToTarget
from tcg.types.signal import (
    DeltaHedgeSpec,
    HedgeSpec,
    InstrumentContinuous,
    InstrumentOptionStream,
    delta_hedge_to_hedge_spec,
)


def _ps(dates, vals):
    a = np.asarray(vals, dtype=np.float64)
    d = np.asarray(dates, dtype=np.int64)
    return PriceSeries(dates=d, open=a, high=a, low=a, close=a, volume=np.zeros(len(a)))


@dataclass
class _Cont:
    """Duck-typed continuous series — the resolver reads only ``.prices``."""

    prices: PriceSeries


class _RecSvc:
    """Fake MarketDataService recording get_continuous / get_prices calls."""

    def __init__(self):
        self.continuous_calls: list[tuple] = []
        self.prices_calls: list[tuple] = []

    async def get_continuous(self, collection, roll_config, *, start=None, end=None):
        self.continuous_calls.append((collection, roll_config))
        return _Cont(prices=_ps([20240102, 20240103], [20.0, 21.0]))

    async def get_prices(self, collection, instrument_id, *, start=None, end=None):
        self.prices_calls.append((collection, instrument_id))
        return _ps([20240102, 20240103], [100.0, 101.0])


async def _fetch_fn(instrument, stream):
    # the option-delta second-resolve
    return np.array([20240102, 20240103], dtype=np.int64), np.array([0.4, 0.5])


def _opt() -> InstrumentOptionStream:
    return InstrumentOptionStream(
        collection="OPT_VIX",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=0.30, tolerance=0.20),
        stream="mid",
        hold_between_rolls=True,
    )


async def test_vx1_legacy_maps_to_frontmonth_difference_continuous() -> None:
    svc = _RecSvc()
    d_pair, hp_pair, gate_pair = await resolve_delta_hedge_raw(
        label="vx1",
        hedge=DeltaHedgeSpec(),  # FUT_VIX / front_month / VVIX>150
        instrument=_opt(),
        fetch_fn=_fetch_fn,
        svc=svc,
        start_date=None,
        end_date=None,
    )
    assert len(svc.continuous_calls) == 1
    coll, cfg = svc.continuous_calls[0]
    assert coll == "FUT_VIX"
    assert cfg == ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.DIFFERENCE
    )
    # gate fetched (VVIX), pair present
    assert svc.prices_calls == [("INDEX", "IND_VVIX")]
    assert gate_pair is not None
    np.testing.assert_array_equal(hp_pair[1], np.array([20.0, 21.0]))


async def test_non_vix_future_hedge_dispatches_continuous() -> None:
    svc = _RecSvc()
    spec = HedgeSpec(
        hedge_instrument=InstrumentContinuous(
            collection="FUT_ES", adjustment="difference", strategy="front_month"
        ),
        gate_collection="INDEX",
        gate_symbol="IND_VVIX",
    )
    _d, hp_pair, gate_pair = await resolve_hedge_raw(
        label="es",
        hedge=spec,
        instrument=_opt(),
        fetch_fn=_fetch_fn,
        svc=svc,
        start_date=None,
        end_date=None,
    )
    assert [c[0] for c in svc.continuous_calls] == ["FUT_ES"]
    assert gate_pair is not None


async def test_spot_hedge_dispatches_get_prices_not_continuous() -> None:
    from tcg.types.signal import InstrumentSpot

    svc = _RecSvc()
    spec = HedgeSpec(
        hedge_instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
        gate_collection=None,  # always-on activation
        gate_symbol=None,
    )
    _d, hp_pair, gate_pair = await resolve_hedge_raw(
        label="spot",
        hedge=spec,
        instrument=_opt(),
        fetch_fn=_fetch_fn,
        svc=svc,
        start_date=None,
        end_date=None,
    )
    assert svc.continuous_calls == []  # NOT a continuous fetch
    assert ("INDEX", "SPX") in svc.prices_calls
    assert gate_pair is None  # no gate ⇒ always-on
    np.testing.assert_array_equal(hp_pair[1], np.array([100.0, 101.0]))


def test_migrator_maps_fields() -> None:
    dh = DeltaHedgeSpec(
        factor=0.25,
        hedge_collection="FUT_VIX",
        hedge_roll_strategy="front_month",
        gate_collection="INDEX",
        gate_symbol="IND_VVIX",
        gate_threshold=150.0,
        gate_op="gt",
    )
    hs = delta_hedge_to_hedge_spec(dh)
    assert isinstance(hs.hedge_instrument, InstrumentContinuous)
    assert hs.hedge_instrument.collection == "FUT_VIX"
    assert hs.hedge_instrument.adjustment == "difference"
    assert hs.hedge_instrument.strategy == "front_month"
    assert hs.factor == 0.25
    assert (hs.gate_collection, hs.gate_symbol, hs.gate_threshold, hs.gate_op) == (
        "INDEX",
        "IND_VVIX",
        150.0,
        "gt",
    )
    # byte-identical knobs
    assert hs.rebalance_interval_days == 1
    assert hs.qty_cap_mult == 10.0
    assert hs.pause_on_roll is True
