"""Regression tests for the FUT_VIX weekly '-2.147480' no-quote sentinel.

Real dwh data carries a ``-2.147480`` (INT32_MIN/1e9) *no-quote* sentinel on the
first listed day of ~108 weekly FUT_VIX contracts. Under
``front_month`` + all cycles + ``ratio`` + ``roll_offset=90`` this negative close
flowed through rolling/back-adjustment (the guards only skipped EXACT 0 /
non-finite), producing NEGATIVE per-roll ratios and a portfolio equity that blew
up to ~1e63. A non-positive futures close is not a price — it must be treated as
"untraded", identically to the exact-0 rows already stripped.
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
from tcg.data._rolling.calendar import _first_tradeable_int, trim_overlaps
from tcg.data._rolling.adjustment import adjust_ratio, adjust_difference
from tcg.data._rolling.stitcher import ContinuousSeriesBuilder

SENTINEL = -2.147480


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


def test_first_tradeable_skips_negative_sentinel():
    """First tradeable day is the first close > 0, not the sentinel bar."""
    c = _contract("W", 20150826, [20150729, 20150730, 20150731], [SENTINEL, 15.0, 16.0])
    assert _first_tradeable_int(c) == 20150730


def test_trim_overlaps_strips_non_positive_close():
    """trim_overlaps drops non-positive (sentinel/negative) rows like exact-0."""
    c = _contract("W", 20150826, [20150729, 20150730, 20150731], [SENTINEL, 15.0, 16.0])
    trimmed = trim_overlaps([c], [])
    assert len(trimmed) == 1
    kept = trimmed[0].prices
    assert list(kept.close) == [15.0, 16.0]
    assert list(kept.dates) == [20150730, 20150731]
    assert np.all(kept.close > 0)


def test_adjust_ratio_skips_negative_reference_close():
    """A negative reference close must not create a (sign-flipping) ratio."""
    # Two contracts sharing the seam day 20150729; the NEW contract's close on
    # the seam is the sentinel. The roll must be skipped (prior prices unchanged),
    # never multiplied by new/old < 0.
    old = _contract("A", 20150729, [20150728, 20150729], [14.0, 15.0])
    new = _contract("B", 20150826, [20150729, 20150730], [SENTINEL, 16.0])
    prices = PriceSeries(
        dates=np.array([20150728, 20150729, 20150730], dtype=np.int64),
        open=np.array([14.0, 15.0, 16.0]),
        high=np.array([14.0, 15.0, 16.0]),
        low=np.array([14.0, 15.0, 16.0]),
        close=np.array([14.0, 15.0, 16.0]),
        volume=np.full(3, 1000.0),
    )
    out = adjust_ratio(prices, [20150729], [old, new])
    # Roll skipped → prices before the roll are unchanged (no negative factor).
    assert out.close[0] == 14.0
    assert np.all(out.close > 0)


def test_adjust_difference_skips_negative_reference_close():
    old = _contract("A", 20150729, [20150728, 20150729], [14.0, 15.0])
    new = _contract("B", 20150826, [20150729, 20150730], [SENTINEL, 16.0])
    prices = PriceSeries(
        dates=np.array([20150728, 20150729, 20150730], dtype=np.int64),
        open=np.array([14.0, 15.0, 16.0]),
        high=np.array([14.0, 15.0, 16.0]),
        low=np.array([14.0, 15.0, 16.0]),
        close=np.array([14.0, 15.0, 16.0]),
        volume=np.full(3, 1000.0),
    )
    out = adjust_difference(prices, [20150729], [old, new])
    assert out.close[0] == 14.0


def test_builder_ratio_never_propagates_sentinel_or_explodes():
    """End-to-end: a sentinel-first-bar contract must not leak into the series
    nor make the ratio-adjusted output non-positive/exploding."""
    # Three front-month contracts; the middle one lists a sentinel close on the
    # shared SEAM day (20150720) it owns after dedup — so the sentinel both
    # (a) would leak into the stitched series and (b) is the ratio reference.
    c1 = _contract("C1", 20150720, [20150716, 20150717, 20150720], [15.0, 15.5, 16.0])
    c2 = _contract(
        "C2", 20150817, [20150720, 20150721, 20150817], [SENTINEL, 16.5, 17.0]
    )
    c3 = _contract("C3", 20150921, [20150817, 20150818, 20150921], [17.5, 18.0, 18.5])
    cfg = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.RATIO
    )
    series = ContinuousSeriesBuilder().build([c1, c2, c3], cfg, "FUT_TEST")
    close = series.prices.close
    assert np.all(np.isfinite(close))
    assert np.all(close > 0), f"non-positive close leaked: {close}"
    # Bounded: no sign flip / explosion (all within a sane multiple of inputs).
    assert close.max() < 1e3
    assert SENTINEL not in list(close)
