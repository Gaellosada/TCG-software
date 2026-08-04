"""Property tests — F1 non-normalizing / leveraged portfolio combine.

Invariants (Hypothesis, arbitrary finite weight vectors incl. gross>1 and
negatives, arbitrary return matrices):

* NON-NORMALIZED daily combine per-bar return ≈ Σ wᵢ·rᵢ.
* NORMALIZED daily combine per-bar return ≈ Σ (wᵢ/Σ|w|)·rᵢ (leverage erased).
* "Leverage preserved through combine": scaling every weight by k scales the
  portfolio (excess) return by k in non-normalized mode.

The daily kernel is the exact fixed-weight combine, so these hold to fp tol.
Prices are reconstructed from the return matrix (normal basis:
``close[t]=close[t-1]·(1+r)``) so ``compute_daily_returns`` recovers r exactly.
"""

from __future__ import annotations

import numpy as np
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tcg.engine.metrics import compute_weighted_portfolio


def _closes_from_returns(rets: np.ndarray) -> np.ndarray:
    """Reconstruct a positive close series from a normal-return series (r[0] ignored)."""
    out = np.empty(rets.shape[0], dtype=np.float64)
    out[0] = 100.0
    for t in range(1, rets.shape[0]):
        out[t] = out[t - 1] * (1.0 + rets[t])
    return out


@st.composite
def _weights_and_returns(draw):
    n_legs = draw(st.integers(min_value=1, max_value=4))
    n_bars = draw(st.integers(min_value=2, max_value=8))
    labels = [f"L{i}" for i in range(n_legs)]

    fin_w = st.floats(
        min_value=-5.0, max_value=5.0, allow_nan=False, allow_infinity=False
    )
    weights = {lbl: draw(fin_w) for lbl in labels}
    # Gross must be non-trivially non-zero (validation guard + division safety).
    assume(sum(abs(w) for w in weights.values()) > 1e-3)

    # Returns kept in (-0.4, 0.4) so reconstructed prices stay positive/finite.
    fin_r = st.floats(
        min_value=-0.4, max_value=0.4, allow_nan=False, allow_infinity=False
    )
    rets = {lbl: np.array([0.0] + [draw(fin_r) for _ in range(n_bars - 1)]) for lbl in labels}
    return labels, n_bars, weights, rets


@settings(max_examples=200, deadline=None)
@given(_weights_and_returns())
def test_daily_non_normalized_is_signed_weighted_sum(data) -> None:
    labels, n_bars, weights, rets = data
    closes = {lbl: _closes_from_returns(rets[lbl]) for lbl in labels}
    dates = np.arange(20200101, 20200101 + n_bars, dtype=np.int64)

    r = compute_weighted_portfolio(
        closes, weights, "daily", "normal", dates, normalize_weights=False
    )
    expected = np.full(n_bars, np.nan)
    for t in range(1, n_bars):
        expected[t] = sum(weights[lbl] * rets[lbl][t] for lbl in labels)
    np.testing.assert_allclose(
        r.portfolio_returns[1:], expected[1:], rtol=1e-9, atol=1e-12
    )


@settings(max_examples=200, deadline=None)
@given(_weights_and_returns())
def test_daily_normalized_is_gross_scaled_sum(data) -> None:
    labels, n_bars, weights, rets = data
    closes = {lbl: _closes_from_returns(rets[lbl]) for lbl in labels}
    dates = np.arange(20200101, 20200101 + n_bars, dtype=np.int64)
    gross = sum(abs(w) for w in weights.values())

    r = compute_weighted_portfolio(
        closes, weights, "daily", "normal", dates, normalize_weights=True
    )
    expected = np.full(n_bars, np.nan)
    for t in range(1, n_bars):
        expected[t] = sum((weights[lbl] / gross) * rets[lbl][t] for lbl in labels)
    np.testing.assert_allclose(
        r.portfolio_returns[1:], expected[1:], rtol=1e-9, atol=1e-12
    )


@settings(max_examples=200, deadline=None)
@given(
    _weights_and_returns(),
    st.floats(min_value=0.1, max_value=5.0, allow_nan=False, allow_infinity=False),
)
def test_leverage_preserved_scaling_by_k(data, k: float) -> None:
    """Scaling all weights by k scales the non-normalized portfolio return by k."""
    labels, n_bars, weights, rets = data
    closes = {lbl: _closes_from_returns(rets[lbl]) for lbl in labels}
    dates = np.arange(20200101, 20200101 + n_bars, dtype=np.int64)

    base = compute_weighted_portfolio(
        closes, weights, "daily", "normal", dates, normalize_weights=False
    )
    scaled = compute_weighted_portfolio(
        closes,
        {lbl: k * w for lbl, w in weights.items()},
        "daily",
        "normal",
        dates,
        normalize_weights=False,
    )
    np.testing.assert_allclose(
        scaled.portfolio_returns[1:],
        k * base.portfolio_returns[1:],
        rtol=1e-9,
        atol=1e-12,
    )


@settings(max_examples=200, deadline=None)
@given(
    _weights_and_returns(),
    st.floats(min_value=0.1, max_value=5.0, allow_nan=False, allow_infinity=False),
)
def test_normalized_is_scale_invariant(data, k: float) -> None:
    """NORMALIZED mode is invariant to scaling all weights (leverage erased)."""
    labels, n_bars, weights, rets = data
    closes = {lbl: _closes_from_returns(rets[lbl]) for lbl in labels}
    dates = np.arange(20200101, 20200101 + n_bars, dtype=np.int64)

    base = compute_weighted_portfolio(
        closes, weights, "daily", "normal", dates, normalize_weights=True
    )
    scaled = compute_weighted_portfolio(
        closes,
        {lbl: k * w for lbl, w in weights.items()},
        "daily",
        "normal",
        dates,
        normalize_weights=True,
    )
    np.testing.assert_allclose(
        scaled.portfolio_returns[1:], base.portfolio_returns[1:], rtol=1e-9, atol=1e-12
    )
