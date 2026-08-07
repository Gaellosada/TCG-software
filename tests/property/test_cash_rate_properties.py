"""Property tests for the cash-rate accrual leg (invariants of §5.7)."""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.engine.cash_rate import accrue_cash_equity

# Rates as fractions: 0 .. 25 %/yr (well inside the > -100 % guard, realistic
# for a US short rate: ZIRP ~0, 2007/2023 peaks ~5 %).
_rates = st.floats(min_value=0.0, max_value=0.25, allow_nan=False, allow_infinity=False)


@given(
    rates=st.lists(_rates, min_size=1, max_size=400),
    compound=st.booleans(),
)
@settings(max_examples=200, deadline=None)
def test_monotonic_non_decreasing_when_rate_nonneg(
    rates: list[float], compound: bool
) -> None:
    equity = accrue_cash_equity(
        np.array(rates, dtype=np.float64), compound=compound
    )
    diffs = np.diff(equity)
    assert np.all(diffs >= -1e-12), "equity must be non-decreasing when rate >= 0"
    assert equity[0] == 100.0


@given(rate=_rates, n=st.integers(min_value=30, max_value=600))
@settings(max_examples=150, deadline=None)
def test_constant_rate_near_zero_daily_vol(rate: float, n: int) -> None:
    """Constant rate -> daily log-returns are (near) identical -> ~zero vol."""
    equity = accrue_cash_equity(rate, n)
    daily_ret = equity[1:] / equity[:-1] - 1.0
    # Every non-funding bar carries the SAME growth factor -> std ~ 0.
    assert float(np.std(daily_ret)) < 1e-12


@given(rate=_rates, n=st.integers(min_value=30, max_value=600))
@settings(max_examples=100, deadline=None)
def test_leverage_sign_independent_shape(rate: float, n: int) -> None:
    """The leg's RETURN series is a property of the rate, independent of any
    external weight/leverage sign the combine later applies. Scaling the base
    (as a leverage magnitude would in %-return space) leaves day-over-day
    ratios invariant.
    """
    e1 = accrue_cash_equity(rate, n, base=100.0)
    e2 = accrue_cash_equity(rate, n, base=250.0)
    r1 = e1[1:] / e1[:-1]
    r2 = e2[1:] / e2[:-1]
    assert np.allclose(r1, r2, atol=1e-12)
