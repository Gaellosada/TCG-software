"""F1 — non-normalizing / leveraged portfolio combine (``compute_weighted_portfolio``).

Two contracts are pinned here:

1. **Golden regression** — the DEFAULT path (``normalize_weights=True``) must be
   BYTE-IDENTICAL to the pre-F1 behaviour. The oracle constants below were
   captured from the code *before* the F1 branch was added (a fixed 3-leg /
   6-bar fixture, all four rebalance frequencies). Any drift here is a
   regression in the normalized combine.

2. **Leverage semantics** — with ``normalize_weights=False`` the Σ|w| divide is
   skipped: weights are raw signed notional multiples of NAV, gross may exceed
   100%, and the per-bar return of the daily kernel equals the un-normalized
   ``Σ wᵢ·rᵢ`` (vs the normalized ``Σ wᵢ·rᵢ / Σ|w|``).
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.metrics import compute_weighted_portfolio


# ── Golden fixture (identical to the capture harness) ──────────────────────
_CLOSES = {
    "x": np.array([100.0, 102.0, 101.0, 103.0, 107.0, 106.0]),
    "y": np.array([50.0, 50.5, 51.0, 50.2, 49.8, 50.9]),
    "z": np.array([200.0, 198.0, 201.0, 205.0, 203.0, 207.0]),
}
_DATES = np.array(
    [20200101, 20200102, 20200103, 20200106, 20200107, 20200108], dtype=np.int64
)
_WEIGHTS = {"x": 0.5, "y": -0.3, "z": 0.2}

# Oracle: captured from the pre-F1 implementation. NaN at index 0 (no prior bar).
_ORACLE = {
    "daily": {
        "ret": [
            np.nan,
            0.005,
            -0.004841954783713664,
            0.018586971954438623,
            0.019856694462972203,
            -0.007358516520850685,
        ],
        "eq": [
            100.0,
            100.49999999999999,
            100.01338354423677,
            101.87232949924201,
            103.89517722033968,
            103.13066284232708,
        ],
    },
    "none": {
        "ret": [
            np.nan,
            0.005,
            -0.004975124378109453,
            0.018799999999999956,
            0.020023557126030687,
            -0.007313317936874431,
        ],
        "eq": [100.0, 100.5, 100.0, 101.88, 103.92, 103.16000000000001],
    },
    "weekly": {
        "ret": [
            np.nan,
            0.005,
            -0.004916013989458603,
            0.018586971954438567,
            0.019870532729761598,
            -0.007464647211114487,
        ],
        "eq": [
            100.0,
            100.5,
            100.00594059405941,
            101.86474820715844,
            103.88885502041771,
            103.11336136852367,
        ],
    },
    "monthly": {
        "ret": [
            np.nan,
            0.005,
            -0.004916013989458603,
            0.01861159092470919,
            0.020006297748185967,
            -0.007362232478542118,
        ],
        "eq": [
            100.0,
            100.5,
            100.00594059405941,
            101.86721025043681,
            103.90519598948411,
            103.14022178088105,
        ],
    },
}


@pytest.mark.parametrize("freq", ["daily", "none", "weekly", "monthly"])
def test_default_path_byte_identical_to_oracle(freq: str) -> None:
    """DEFAULT (normalize_weights=True) reproduces the pre-F1 numbers EXACTLY."""
    r = compute_weighted_portfolio(_CLOSES, _WEIGHTS, freq, "normal", _DATES)
    # assert_array_equal treats same-position NaNs as equal and is EXACT.
    np.testing.assert_array_equal(r.portfolio_returns, np.array(_ORACLE[freq]["ret"]))
    np.testing.assert_array_equal(r.portfolio_equity, np.array(_ORACLE[freq]["eq"]))


@pytest.mark.parametrize("freq", ["daily", "none", "weekly", "monthly"])
def test_default_matches_explicit_true(freq: str) -> None:
    """Passing normalize_weights=True explicitly == omitting it."""
    a = compute_weighted_portfolio(_CLOSES, _WEIGHTS, freq, "normal", _DATES)
    b = compute_weighted_portfolio(
        _CLOSES, _WEIGHTS, freq, "normal", _DATES, normalize_weights=True
    )
    np.testing.assert_array_equal(a.portfolio_returns, b.portfolio_returns)
    np.testing.assert_array_equal(a.portfolio_equity, b.portfolio_equity)


# ── Leverage unit: weights [2.0, -1.0] on a hand-computable series ──────────
# r_a = [., +0.10, +0.10], r_b = [., +0.05, 0.00]
_LEV_CLOSES = {
    "a": np.array([100.0, 110.0, 121.0]),
    "b": np.array([100.0, 105.0, 105.0]),
}
_LEV_DATES = np.array([20200101, 20200102, 20200103], dtype=np.int64)
_LEV_W = {"a": 2.0, "b": -1.0}


def test_daily_non_normalized_equals_signed_weighted_sum() -> None:
    """Daily kernel per-bar return == Σ wᵢ·rᵢ (signed, un-normalized)."""
    r = compute_weighted_portfolio(
        _LEV_CLOSES, _LEV_W, "daily", "normal", _LEV_DATES, normalize_weights=False
    )
    # bar1: 2*0.10 + (-1)*0.05 = 0.15 ; bar2: 2*0.10 + (-1)*0.00 = 0.20
    assert r.portfolio_returns[1] == pytest.approx(2 * 0.10 - 1 * 0.05)  # 0.15
    assert r.portfolio_returns[2] == pytest.approx(2 * 0.10 - 1 * 0.00)  # 0.20


def test_non_normalized_preserves_gross_over_100pct() -> None:
    """Non-normalized bar-1 return is Σ|w|× the normalized one (leverage kept)."""
    lev = compute_weighted_portfolio(
        _LEV_CLOSES, _LEV_W, "daily", "normal", _LEV_DATES, normalize_weights=False
    )
    nrm = compute_weighted_portfolio(
        _LEV_CLOSES, _LEV_W, "daily", "normal", _LEV_DATES, normalize_weights=True
    )
    gross = abs(2.0) + abs(1.0)  # 3.0
    # Normalized divides by gross → leveraged return is exactly gross× larger.
    assert nrm.portfolio_returns[1] == pytest.approx(0.15 / gross)  # 0.05
    assert lev.portfolio_returns[1] == pytest.approx(gross * nrm.portfolio_returns[1])


@pytest.mark.parametrize("freq", ["none", "monthly"])
def test_buyhold_periodic_first_bar_is_leveraged(freq: str) -> None:
    """Buy-and-hold / periodic first-bar return is the leveraged Σ wᵢ·rᵢ too.

    (Bar 1 has no boundary/drift yet, so it is the clean invariant point;
    default-mode would have re-normalized it to Σ wᵢ·rᵢ / Σ|w|.)
    """
    lev = compute_weighted_portfolio(
        _LEV_CLOSES, _LEV_W, freq, "normal", _LEV_DATES, normalize_weights=False
    )
    nrm = compute_weighted_portfolio(
        _LEV_CLOSES, _LEV_W, freq, "normal", _LEV_DATES, normalize_weights=True
    )
    assert lev.portfolio_returns[1] == pytest.approx(0.15)
    assert nrm.portfolio_returns[1] == pytest.approx(0.05)


def test_periodic_leverage_does_not_blow_up() -> None:
    """A raw-weight periodic combine must NOT inflate the book by Σ|w| per rebalance.

    (Guard against the naive 'pass raw weights straight in' bug where the
    boundary redistribution ``leg = |w|·total`` multiplies equity by Σ|w|=3 at
    every rebalance.) Monthly boundaries over a multi-month grid must keep the
    equity finite and on the order of the leveraged P&L, not exponential.
    """
    # ~4 months of flat-ish data with a monthly boundary each month start.
    dates = np.array(
        [20200102, 20200103, 20200203, 20200204, 20200303, 20200304, 20200402],
        dtype=np.int64,
    )
    closes = {
        "a": np.array([100.0, 100.5, 101.0, 101.2, 101.5, 101.6, 102.0]),
        "b": np.array([100.0, 100.1, 100.0, 99.9, 100.2, 100.1, 100.3]),
    }
    w = {"a": 2.0, "b": -1.0}
    r = compute_weighted_portfolio(closes, w, "monthly", "normal", dates, normalize_weights=False)
    assert np.all(np.isfinite(r.portfolio_equity))
    # 2× long a (+2%) minus 1× short b (+0.3%) over the window → a few % gain,
    # certainly bounded well under a 3^3 = 27× blow-up.
    assert 90.0 < r.portfolio_equity[-1] < 130.0


def test_all_zero_weights_guard_still_raises() -> None:
    """The all-zero guard is preserved in non-normalizing mode."""
    with pytest.raises(ValueError, match="All weights are zero"):
        compute_weighted_portfolio(
            _LEV_CLOSES,
            {"a": 0.0, "b": 0.0},
            "daily",
            "normal",
            _LEV_DATES,
            normalize_weights=False,
        )
