"""Tests for the NTH_NEAREST continuous-roll strategy.

Holds the rank-th nearest contract by expiration (within the cycle-filtered set):
the rank-th nearest changes each time the front (nearest) contract expires, so a
roll fires at each front-contract expiry and ownership shifts up by one. rank=1 is
therefore identical to FRONT_MONTH. rank=3 on a monthly cycle ≈ a ~3-month VIX.
"""

from __future__ import annotations

import numpy as np

from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContractPriceData,
    PriceSeries,
    RollStrategy,
)
from tcg.data._rolling.stitcher import ContinuousSeriesBuilder


def _contract(cid: str, expiration: int, dates: list[int], closes: list[float]):
    n = len(dates)
    c = np.array(closes, dtype=np.float64)
    return ContractPriceData(
        contract_id=cid,
        expiration=expiration,
        prices=PriceSeries(
            dates=np.array(dates, dtype=np.int64),
            open=c.copy(),
            high=c.copy(),
            low=c.copy(),
            close=c,
            volume=np.full(n, 1000.0),
        ),
    )


def _monthly_set():
    """Five monthly contracts, each trading from ~2 months before its expiry."""
    return [
        _contract("M1", 20200117, [20191115, 20191216, 20200117], [10.0, 10.5, 11.0]),
        _contract("M2", 20200221, [20191216, 20200117, 20200221], [12.0, 12.5, 13.0]),
        _contract("M3", 20200320, [20200117, 20200221, 20200320], [14.0, 14.5, 15.0]),
        _contract("M4", 20200417, [20200221, 20200320, 20200417], [16.0, 16.5, 17.0]),
        _contract("M5", 20200515, [20200320, 20200417, 20200515], [18.0, 18.5, 19.0]),
    ]


def test_enum_and_default_rank():
    assert RollStrategy.NTH_NEAREST == "nth_nearest"
    cfg = ContinuousRollConfig(strategy=RollStrategy.NTH_NEAREST)
    assert cfg.rank == 1


def test_rank1_equals_front_month():
    contracts = _monthly_set()
    fm = ContinuousSeriesBuilder().build(
        contracts,
        ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH),
        "FUT_TEST",
    )
    nn = ContinuousSeriesBuilder().build(
        contracts,
        ContinuousRollConfig(strategy=RollStrategy.NTH_NEAREST, rank=1),
        "FUT_TEST",
    )
    assert np.array_equal(fm.prices.dates, nn.prices.dates)
    assert np.array_equal(fm.prices.close, nn.prices.close)
    assert list(fm.contracts) == list(nn.contracts)


def test_rank1_equals_front_month_with_offset():
    """rank=1 composes roll_offset identically to FRONT_MONTH."""
    contracts = _monthly_set()
    fm = ContinuousSeriesBuilder().build(
        contracts,
        ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH, roll_offset_days=10),
        "FUT_TEST",
    )
    nn = ContinuousSeriesBuilder().build(
        contracts,
        ContinuousRollConfig(
            strategy=RollStrategy.NTH_NEAREST, rank=1, roll_offset_days=10
        ),
        "FUT_TEST",
    )
    assert np.array_equal(fm.prices.dates, nn.prices.dates)
    assert np.array_equal(fm.prices.close, nn.prices.close)
    assert list(fm.roll_dates) == list(nn.roll_dates)


def test_rank3_holds_third_nearest_first():
    """Before any front expiry, the held contract is the 3rd-nearest (M3)."""
    contracts = _monthly_set()
    nn = ContinuousSeriesBuilder().build(
        contracts,
        ContinuousRollConfig(strategy=RollStrategy.NTH_NEAREST, rank=3),
        "FUT_TEST",
    )
    # Held sequence is M3, M4, M5 (contracts[rank-1:]); two rolls (M1, M2 expiries).
    assert list(nn.contracts) == ["M3", "M4", "M5"]
    assert len(nn.roll_dates) == 2
    # First owned bar comes from M3 (close 14.x), never from M1/M2.
    assert nn.prices.close[0] >= 14.0


def test_rank_too_large_yields_empty():
    contracts = _monthly_set()  # 5 contracts
    nn = ContinuousSeriesBuilder().build(
        contracts,
        ContinuousRollConfig(strategy=RollStrategy.NTH_NEAREST, rank=6),
        "FUT_TEST",
    )
    assert len(nn.prices) == 0


def test_rank_equals_count_single_contract():
    contracts = _monthly_set()  # 5 contracts
    nn = ContinuousSeriesBuilder().build(
        contracts,
        ContinuousRollConfig(strategy=RollStrategy.NTH_NEAREST, rank=5),
        "FUT_TEST",
    )
    assert list(nn.contracts) == ["M5"]
    assert len(nn.roll_dates) == 0


def test_parse_legs_threads_nth_nearest_rank():
    """A portfolio continuous leg carries rank into ContinuousRollConfig."""
    from tcg.core.api.portfolio import LegSpec, _parse_legs
    from tcg.types.market import AssetClass, ContinuousLegSpec

    legs = {
        "vix": LegSpec(
            type="continuous",
            collection="FUT_VIX",
            strategy="nth_nearest",
            adjustment="ratio",
            cycle="M",
            rank=3,
        )
    }
    parsed = _parse_legs(legs, lambda _c: AssetClass.FUTURE)
    spec = parsed["vix"]
    assert isinstance(spec, ContinuousLegSpec)
    assert spec.roll_config.strategy == RollStrategy.NTH_NEAREST
    assert spec.roll_config.rank == 3


def test_continuous_cache_key_distinguishes_rank():
    """Two nth_nearest configs differing only in rank must not share a cache
    entry (otherwise every rank returns the first-computed series)."""
    from tcg.data.service import DefaultMarketDataService

    def cfg(rank):
        return ContinuousRollConfig(strategy=RollStrategy.NTH_NEAREST, rank=rank)

    k1 = DefaultMarketDataService._make_continuous_key("FUT_VIX", cfg(1), None, None)
    k3 = DefaultMarketDataService._make_continuous_key("FUT_VIX", cfg(3), None, None)
    assert k1 != k3
