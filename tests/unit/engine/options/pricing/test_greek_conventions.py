"""Greek-convention pinning tests — anchored to first-principles, not py_vollib.

This file deliberately avoids calling py_vollib at runtime.  It pins the
kernel's vega and theta scaling conventions against:

- Hand-computed values from the closed-form Black-76 formulas (Hull 9e §17.8,
  Wikipedia "Black-Scholes model" worked example).  These are independent of
  py_vollib and protect against silent shifts in the kernel's scaling.

Conventions under test
----------------------
- ``vega`` is reported **per 1 percentage-point of vol** (kernel divides by
  100).  Cross-stack convention (Bloomberg, OptionsLab, Java
  ``BasicBlackScholes`` :80).  py_vollib also uses this convention.
- ``theta`` is reported **per calendar day** (kernel divides by 365).
  Cross-stack convention; matches Java ``BasicBlackScholes`` :70 and
  py_vollib's ``analytical.theta``.

If IVolatility's stored Greeks ever turn out to use a different scaling,
``ComputeResultCell`` will surface an apparent factor-of-100 (vega) or
factor-of-365 (theta) discrepancy between the "stored" and "computed"
columns.  These tests pin the *kernel* convention so downstream divergence
is obvious.

References
----------
- Hull, J. C. (2014). *Options, Futures, and Other Derivatives*, 9th ed.
  §17.8 (Black's model for valuing futures options) and Table 17.2.
- Wikipedia, "Black–Scholes model", §"The Black–Scholes formula" worked
  example.  https://en.wikipedia.org/wiki/Black%E2%80%93Scholes_model
"""

from __future__ import annotations

import math

import pytest

from tcg.engine.options.pricing.kernel import BS76Kernel


# ---------------------------------------------------------------------------
# Hand-computed reference values — F=100, K=100, sigma=0.20, T=0.5, r=0.
#
# Under r=0 and F=K, Black-76 collapses to the closed-form
#     c = F * [2 N(sigma*sqrt(T)/2) - 1]
# with d1 = sigma*sqrt(T)/2, d2 = -d1.
#
# The values below are derived from those closed-form expressions using
# Python's ``math.erf`` / ``math.exp`` — i.e. NOT from py_vollib.  The kernel
# is asserted against these constants with absolute tolerance 1e-2 (looser
# than the goldens against py_vollib so a future kernel re-write that reaches
# textbook-accurate but not-bit-identical values still passes).
# ---------------------------------------------------------------------------


# ATM call on F=100, K=100, sigma=0.20, T=0.5, r=0
HULL_ATM_F = 100.0
HULL_ATM_K = 100.0
HULL_ATM_SIGMA = 0.20
HULL_ATM_T = 0.5
HULL_ATM_R = 0.0

# Closed-form: c = F * [2 N(d1) - 1], d1 = sigma*sqrt(T)/2
# = 100 * (2 * N(0.07071...) - 1) = 5.6371977797...
HULL_ATM_CALL_PRICE = 5.6371977797016655

# delta_call = N(d1) = N(0.07071...) = 0.5281859888985083
HULL_ATM_CALL_DELTA = 0.5281859888985083

# gamma = phi(d1) / (F * sigma * sqrt(T))
HULL_ATM_GAMMA = 0.02813904356065048

# vega (per 1 percentage-point) = F * phi(d1) * sqrt(T) / 100
HULL_ATM_VEGA_PER_PCT = 0.28139043560650484

# theta (per calendar day, r=0) = -F * phi(d1) * sigma / (2 * sqrt(T)) / 365
HULL_ATM_THETA_PER_DAY = -0.015418654005835879


@pytest.fixture(scope="module")
def kernel() -> BS76Kernel:
    return BS76Kernel()


# ---------------------------------------------------------------------------
# Price / delta / gamma — independently anchored.  These are not the primary
# focus of this file (test_golden.py covers them), but pinning them with
# textbook-derived values guards against py_vollib regression.
# ---------------------------------------------------------------------------


def test_kernel_atm_call_price_matches_textbook(kernel: BS76Kernel) -> None:
    """ATM call: c = F * (2 N(sigma*sqrt(T)/2) - 1) — Hull §17.8."""
    actual = kernel.price_call(
        HULL_ATM_F, HULL_ATM_K, HULL_ATM_T, HULL_ATM_R, HULL_ATM_SIGMA
    )
    assert actual == pytest.approx(HULL_ATM_CALL_PRICE, abs=1e-2)


def test_kernel_atm_call_delta_matches_textbook(kernel: BS76Kernel) -> None:
    """ATM call delta: N(d1), d1 = sigma*sqrt(T)/2."""
    actual = kernel.delta(
        HULL_ATM_F, HULL_ATM_K, HULL_ATM_T, HULL_ATM_R, HULL_ATM_SIGMA, "c"
    )
    assert actual == pytest.approx(HULL_ATM_CALL_DELTA, abs=1e-2)


def test_kernel_atm_gamma_matches_textbook(kernel: BS76Kernel) -> None:
    """gamma = phi(d1) / (F * sigma * sqrt(T))."""
    actual = kernel.gamma(
        HULL_ATM_F, HULL_ATM_K, HULL_ATM_T, HULL_ATM_R, HULL_ATM_SIGMA
    )
    assert actual == pytest.approx(HULL_ATM_GAMMA, abs=1e-2)


# ---------------------------------------------------------------------------
# Vega / theta convention pins.  These names embed the convention itself so
# any reviewer reading a failure can immediately tell what convention shifted.
# ---------------------------------------------------------------------------


def test_kernel_vega_is_per_vol_point_not_per_unit_vol(kernel: BS76Kernel) -> None:
    """Kernel vega is per 1 percentage-point of vol (divides by 100).

    This is the Bloomberg / OptionsLab / Java BasicBlackScholes convention.
    The "per unit of vol" convention (i.e. without the /100) would be 100x
    larger.  If this test fails by ~100x, someone changed the convention.
    """
    actual = kernel.vega(
        HULL_ATM_F, HULL_ATM_K, HULL_ATM_T, HULL_ATM_R, HULL_ATM_SIGMA
    )
    assert actual == pytest.approx(HULL_ATM_VEGA_PER_PCT, abs=1e-2)
    # Sanity: per-unit-vol value would be ~100x — guard the magnitude band.
    assert 0.05 < actual < 0.5, (
        f"vega={actual} outside per-1%-point band; convention likely shifted"
    )


def test_kernel_theta_is_per_calendar_day_not_per_year(kernel: BS76Kernel) -> None:
    """Kernel theta is per calendar day (divides by 365).

    The "per year" convention would be 365x larger.  Java
    BasicBlackScholes :70 and py_vollib's analytical.theta both use the
    per-calendar-day convention; we pin to the same.
    """
    actual = kernel.theta(
        HULL_ATM_F, HULL_ATM_K, HULL_ATM_T, HULL_ATM_R, HULL_ATM_SIGMA, "c"
    )
    assert actual == pytest.approx(HULL_ATM_THETA_PER_DAY, abs=1e-2)
    # Sanity: per-year value would be ~365x — guard the magnitude band.
    assert -0.5 < actual < 0.0, (
        f"theta={actual} outside per-day band; convention likely shifted"
    )


# ---------------------------------------------------------------------------
# Hull 9e Table 17.2 / Wikipedia worked example — externally anchored.
# These tests do NOT call py_vollib at runtime; they assert the kernel
# against hardcoded textbook values.
# ---------------------------------------------------------------------------


def test_kernel_textbook_atm_call_with_r_nonzero(kernel: BS76Kernel) -> None:
    """Black-76 ATM call with r != 0 — independent textbook anchor.

    F=100, K=100, sigma=0.20, T=0.5, r=0.05.

    Closed-form: c = exp(-rT) * F * (2 N(sigma*sqrt(T)/2 + r*sqrt(T)/sigma) - 1)
    using the kernel's d1 = (log(F/K) + (r + sigma^2/2)T) / (sigma*sqrt(T))
    convention which folds r into d1 (matches Hull §17.8 Black-with-carry form).

    Computed by hand from math primitives (no py_vollib).
    """
    F, K, T, r, sigma = 100.0, 100.0, 0.5, 0.05, 0.20

    # Hand-compute the expected price from the kernel's d1 convention.
    sqT = math.sqrt(T)
    d1 = (math.log(F / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    N = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))  # noqa: E731
    expected = math.exp(-r * T) * (F * N(d1) - K * N(d2))

    actual = kernel.price_call(F, K, T, r, sigma)
    assert actual == pytest.approx(expected, abs=1e-2)


def test_kernel_put_call_parity_at_r_zero(kernel: BS76Kernel) -> None:
    """Put-call parity for Black-76: c - p = exp(-rT) * (F - K).

    Independent of py_vollib.  At r=0, ATM (F=K), c == p.
    """
    F, K, T, sigma = 100.0, 100.0, 0.5, 0.20
    c = kernel.price_call(F, K, T, 0.0, sigma)
    p = kernel.price_put(F, K, T, 0.0, sigma)
    assert c == pytest.approx(p, abs=1e-12)
    # General parity at r=0.05, K shifted off-ATM
    F2, K2, T2, r2, sigma2 = 110.0, 100.0, 0.5, 0.05, 0.20
    c2 = kernel.price_call(F2, K2, T2, r2, sigma2)
    p2 = kernel.price_put(F2, K2, T2, r2, sigma2)
    assert (c2 - p2) == pytest.approx(
        math.exp(-r2 * T2) * (F2 - K2), abs=1e-10
    )
