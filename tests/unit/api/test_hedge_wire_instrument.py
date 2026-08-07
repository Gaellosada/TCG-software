"""P1 — modular hedge instrument EXPOSED end-to-end via the wire model.

DB-free.  Proves the API-level ``DeltaHedgeConfig`` can now specify the HEDGE
INSTRUMENT as an arbitrary continuous future (collection + roll strategy /
adjustment / cycle) OR a spot instrument, flowing through ``to_spec()`` to the
generalized :class:`tcg.types.signal.HedgeSpec` and through the core resolver
callers (``_build_delta_hedge_arrays``) to the aligned engine arrays.

Back-compat: an untouched ``DeltaHedgeConfig`` (no ``hedge_instrument``) still
emits the legacy :class:`DeltaHedgeSpec` (byte-identical VX1 path).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from tcg.core.api._models import DeltaHedgeConfig
from tcg.core.api.portfolio import _build_delta_hedge_arrays
from tcg.types.options import ByDelta, NearestToTarget
from tcg.types.signal import (
    DeltaHedgeSpec,
    HedgeSpec,
    InstrumentContinuous,
    InstrumentOptionStream,
    InstrumentSpot,
)
from tcg.types.market import PriceSeries


# ── to_spec() dispatch ─────────────────────────────────────────────────────
def test_to_spec_default_is_legacy_deltahedgespec() -> None:
    """No ``hedge_instrument`` ⇒ the legacy VX1 spec (byte-identical path)."""
    spec = DeltaHedgeConfig().to_spec()
    assert isinstance(spec, DeltaHedgeSpec)
    assert spec.hedge_collection == "FUT_VIX"
    assert spec.hedge_roll_strategy == "front_month"


def test_to_spec_continuous_future_instrument() -> None:
    cfg = DeltaHedgeConfig(
        hedge_instrument={
            "type": "continuous",
            "collection": "FUT_ES",
            "adjustment": "difference",
            "strategy": "front_month",
            "cycle": "HMUZ",
        },
    )
    spec = cfg.to_spec()
    assert isinstance(spec, HedgeSpec)
    assert isinstance(spec.hedge_instrument, InstrumentContinuous)
    assert spec.hedge_instrument.collection == "FUT_ES"
    assert spec.hedge_instrument.adjustment == "difference"
    assert spec.hedge_instrument.strategy == "front_month"
    assert spec.hedge_instrument.cycle == "HMUZ"
    # gate + knobs carry across as VX1-style defaults
    assert spec.gate_collection == "INDEX"
    assert spec.rebalance_interval_days == 1


def test_to_spec_spot_instrument() -> None:
    cfg = DeltaHedgeConfig(
        hedge_instrument={
            "type": "spot",
            "collection": "INDEX",
            "instrument_id": "SPX",
        },
    )
    spec = cfg.to_spec()
    assert isinstance(spec, HedgeSpec)
    assert isinstance(spec.hedge_instrument, InstrumentSpot)
    assert spec.hedge_instrument.collection == "INDEX"
    assert spec.hedge_instrument.instrument_id == "SPX"


def test_bad_hedge_instrument_type_rejected() -> None:
    """An unknown instrument-type discriminator is a 400, not a 500."""
    from pydantic import ValidationError as PydValidationError

    with pytest.raises(PydValidationError):
        DeltaHedgeConfig(
            hedge_instrument={"type": "option", "collection": "OPT_SP_500"},
        )


def test_spot_hedge_instrument_missing_id_rejected() -> None:
    from pydantic import ValidationError as PydValidationError

    with pytest.raises(PydValidationError):
        DeltaHedgeConfig(hedge_instrument={"type": "spot", "collection": "INDEX"})


# ── config → core resolver arrays (end-to-end, DB-free) ────────────────────
def _ps(dates, vals):
    a = np.asarray(vals, dtype=np.float64)
    d = np.asarray(dates, dtype=np.int64)
    return PriceSeries(dates=d, open=a, high=a, low=a, close=a, volume=np.zeros(len(a)))


@dataclass
class _Cont:
    prices: PriceSeries


class _RecSvc:
    def __init__(self):
        self.continuous_calls: list = []
        self.prices_calls: list = []

    async def get_continuous(self, collection, roll_config, *, start=None, end=None):
        self.continuous_calls.append((collection, roll_config))
        return _Cont(prices=_ps([20240102, 20240103, 20240104], [50.0, 51.0, 52.0]))

    async def get_prices(self, collection, instrument_id, *, start=None, end=None):
        self.prices_calls.append((collection, instrument_id))
        # gate (VVIX) high enough to be active, or a spot price series
        return _ps([20240102, 20240103, 20240104], [200.0, 200.0, 200.0])


async def _fetch_fn(instrument, stream):
    return (
        np.array([20240102, 20240103, 20240104], dtype=np.int64),
        np.array([0.4, 0.5, 0.6], dtype=np.float64),
    )


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


async def test_build_arrays_accepts_hedgespec_continuous() -> None:
    """A HedgeSpec (non-VIX future) flows through ``_build_delta_hedge_arrays``."""
    svc = _RecSvc()
    spec = DeltaHedgeConfig(
        hedge_instrument={
            "type": "continuous",
            "collection": "FUT_ES",
            "adjustment": "difference",
        },
    ).to_spec()
    dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
    hedge_delta, hedge_price, hedge_active = await _build_delta_hedge_arrays(
        label="es",
        hedge=spec,
        instrument=_opt(),
        fetcher=_fetch_fn,
        svc=svc,
        dates_arr=dates,
        is_roll_mask=np.zeros(3, dtype=np.bool_),
        start_date=None,
        end_date=None,
    )
    assert [c[0] for c in svc.continuous_calls] == ["FUT_ES"]
    np.testing.assert_array_equal(hedge_price, np.array([50.0, 51.0, 52.0]))
    np.testing.assert_array_equal(hedge_delta, np.array([0.4, 0.5, 0.6]))
    assert hedge_active.all()  # VVIX=200 > 150, no roll bars


async def test_build_arrays_accepts_hedgespec_spot() -> None:
    svc = _RecSvc()
    spec = DeltaHedgeConfig(
        hedge_instrument={"type": "spot", "collection": "INDEX", "instrument_id": "SPX"},
    ).to_spec()
    dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
    _hd, hedge_price, _ha = await _build_delta_hedge_arrays(
        label="spx",
        hedge=spec,
        instrument=_opt(),
        fetcher=_fetch_fn,
        svc=svc,
        dates_arr=dates,
        is_roll_mask=np.zeros(3, dtype=np.bool_),
        start_date=None,
        end_date=None,
    )
    assert svc.continuous_calls == []  # spot dispatch, NOT continuous
    assert ("INDEX", "SPX") in svc.prices_calls
    np.testing.assert_array_equal(hedge_price, np.array([200.0, 200.0, 200.0]))
