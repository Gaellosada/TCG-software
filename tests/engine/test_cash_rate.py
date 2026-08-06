"""Unit oracle tests for the cash-rate accrual leg math (DB-free)."""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.cash_rate import (
    accrue_cash_equity,
    daily_accrual_factor,
    reindex_rate_series,
)


def test_constant_rate_matches_compounded_curve() -> None:
    """1 %/yr compounded over N bars == base * (1+r)^(1/252) ** i, to 1e-9."""
    rate = 0.01
    n = 500
    dc = 252
    equity = accrue_cash_equity(rate, n, day_count=dc, base=100.0, compound=True)
    daily = (1.0 + rate) ** (1.0 / dc)
    expected = 100.0 * daily ** np.arange(n)
    # equity[0] == base (no accrual on the funding bar)
    assert equity[0] == pytest.approx(100.0, abs=1e-12)
    assert np.allclose(equity, expected, atol=1e-9, rtol=0.0)
    # After ~252 bars, equity ~ 100*(1+r)^(251/252) -> just under 101.
    assert equity[dc] == pytest.approx(100.0 * daily**dc, abs=1e-9)


def test_constant_rate_simple_interest() -> None:
    """Simple mode: daily_return = r/252, equity = base*(1+r/252)^i."""
    rate = 0.05
    n = 100
    dc = 252
    equity = accrue_cash_equity(rate, n, day_count=dc, compound=False)
    expected = 100.0 * (1.0 + rate / dc) ** np.arange(n)
    assert np.allclose(equity, expected, atol=1e-9, rtol=0.0)


def test_zero_rate_is_flat() -> None:
    equity = accrue_cash_equity(0.0, 300)
    assert np.all(equity == 100.0)


def test_stepped_rate_matches_hand_calc() -> None:
    """A 4-bar stepped rate curve matches an explicit hand computation.

    Bar 0 is the funding bar (factor forced to 1). Bars 1..3 accrue rate[i].
    """
    dc = 252
    rates = np.array([0.02, 0.02, 0.05, 0.00])  # fractions
    equity = accrue_cash_equity(rates, day_count=dc, base=100.0, compound=True)
    f1 = (1.0 + 0.02) ** (1.0 / dc)
    f2 = (1.0 + 0.05) ** (1.0 / dc)
    f3 = (1.0 + 0.00) ** (1.0 / dc)  # == 1.0
    hand = np.array([
        100.0,
        100.0 * f1,
        100.0 * f1 * f2,
        100.0 * f1 * f2 * f3,
    ])
    assert np.allclose(equity, hand, atol=1e-12, rtol=0.0)


def test_scalar_requires_n() -> None:
    with pytest.raises(ValueError, match="n is required"):
        accrue_cash_equity(0.01)


def test_array_length_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="does not match"):
        accrue_cash_equity(np.array([0.01, 0.02]), n=5)


def test_rate_below_minus_one_rejected() -> None:
    with pytest.raises(ValueError, match="> -1"):
        accrue_cash_equity(np.array([0.0, -1.5]), compound=True)


def test_daily_factor_zero_rate_exactly_one() -> None:
    f = daily_accrual_factor(np.array([0.0, 0.0]))
    assert np.all(f == 1.0)


def test_empty_length() -> None:
    out = accrue_cash_equity(np.array([], dtype=np.float64))
    assert out.shape == (0,)


# ── reindex_rate_series ─────────────────────────────────────────────────────


def test_reindex_step_fills_and_holds() -> None:
    src_dates = np.array([20070101, 20070601, 20080101], dtype=np.int64)
    src_rates = np.array([0.05, 0.02, 0.001], dtype=np.float64)
    target = np.array(
        [20061231, 20070101, 20070301, 20070601, 20071231, 20080101, 20090101],
        dtype=np.int64,
    )
    out = reindex_rate_series(src_dates, src_rates, target, fallback=0.0)
    # before first src date -> fallback; on/after -> last observed value held
    expected = np.array([0.0, 0.05, 0.05, 0.02, 0.02, 0.001, 0.001])
    assert np.allclose(out, expected)


def test_reindex_empty_source_returns_fallback() -> None:
    target = np.array([20200101, 20200102], dtype=np.int64)
    out = reindex_rate_series(
        np.array([], dtype=np.int64),
        np.array([], dtype=np.float64),
        target,
        fallback=0.01,
    )
    assert np.all(out == 0.01)


def test_zero_dim_ndarray_treated_as_scalar() -> None:
    """A6b: a 0-d ndarray (``np.array(0.01)``) must route through the scalar
    branch — ``np.isscalar`` returns False for it, wrongly sending it to the
    1-D array branch and raising "must be 1-D". It should equal the scalar path.
    """
    zerod = accrue_cash_equity(np.array(0.01), n=5, base=100.0, compound=True)
    scalar = accrue_cash_equity(0.01, n=5, base=100.0, compound=True)
    assert zerod.shape == (5,)
    np.testing.assert_array_equal(zerod, scalar)
