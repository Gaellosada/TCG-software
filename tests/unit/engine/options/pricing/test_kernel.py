"""Textbook Black-76 kernel tests.

Sanity checks against well-known cases. Tighter golden agreement against
py_vollib lives in ``test_golden.py``.
"""

from __future__ import annotations

import math

import pytest

from tcg.engine.options.pricing.kernel import BS76Kernel


@pytest.fixture
def kernel() -> BS76Kernel:
    return BS76Kernel()


# ---------------------------------------------------------------------------
# ATM 30-day-tenor cases
# ---------------------------------------------------------------------------
# Brief target T=0.25 (3-month, mislabelled "30-day" in the brief). Under
# r=0, σ=0.20, the analytic ATM call price is F * (2*N(σ√T/2) - 1) ≈ 3.988
# (NOT 3.99 to 1e-3 as the brief states; that figure is rounded). We keep
# the brief's tolerance loose (1e-2) for the textbook-shape sanity test
# and rely on test_golden.py for tight (1e-6) agreement.


def test_atm_call_price_textbook(kernel: BS76Kernel) -> None:
    p = kernel.price_call(F=100, K=100, T=0.25, r=0.0, sigma=0.20)
    assert p == pytest.approx(3.988, abs=1e-2)


def test_atm_put_price_textbook(kernel: BS76Kernel) -> None:
    # Put-call parity at K=F, r=0 → P_call == P_put.
    p = kernel.price_put(F=100, K=100, T=0.25, r=0.0, sigma=0.20)
    assert p == pytest.approx(3.988, abs=1e-2)


def test_put_call_parity_atm_r_zero(kernel: BS76Kernel) -> None:
    c = kernel.price_call(F=100, K=100, T=0.25, r=0.0, sigma=0.20)
    p = kernel.price_put(F=100, K=100, T=0.25, r=0.0, sigma=0.20)
    assert c == pytest.approx(p, abs=1e-12)


def test_atm_call_delta_around_one_half(kernel: BS76Kernel) -> None:
    # Under Black-76 (r=0), ATM call delta = N(σ√T/2). At σ=0.20, T=0.25:
    # d1 = 0.05, N(0.05) ≈ 0.5199. The brief says "≈ 0.504" which is wrong
    # (that's the OTM-adjacent rounding); we use the actual analytic value.
    d = kernel.delta(F=100, K=100, T=0.25, r=0.0, sigma=0.20, flag="c")
    assert d == pytest.approx(0.5199, abs=1e-3)
    assert 0.50 < d < 0.55  # well-bounded sanity


def test_atm_put_delta_negative_about_minus_one_half(kernel: BS76Kernel) -> None:
    d = kernel.delta(F=100, K=100, T=0.25, r=0.0, sigma=0.20, flag="p")
    # Put-call delta parity at r=0: delta_call - delta_put = 1.
    assert d == pytest.approx(-0.4801, abs=1e-3)
    assert -0.55 < d < -0.45


def test_call_delta_minus_put_delta_equals_one_at_r_zero(kernel: BS76Kernel) -> None:
    # Black-76 delta parity: e^{-rT}(N(d1) - (N(d1)-1)) = e^{-rT}.
    dc = kernel.delta(F=100, K=100, T=0.25, r=0.0, sigma=0.20, flag="c")
    dp = kernel.delta(F=100, K=100, T=0.25, r=0.0, sigma=0.20, flag="p")
    assert (dc - dp) == pytest.approx(1.0, abs=1e-12)


def test_atm_gamma_positive(kernel: BS76Kernel) -> None:
    g = kernel.gamma(F=100, K=100, T=0.25, r=0.0, sigma=0.20)
    assert g > 0
    # Sanity: ATM gamma ≈ phi(d1) / (F*sigma*sqrt(T)) ≈ 0.0398.
    assert g == pytest.approx(0.0398, abs=1e-3)


def test_atm_vega_positive(kernel: BS76Kernel) -> None:
    v = kernel.vega(F=100, K=100, T=0.25, r=0.0, sigma=0.20)
    assert v > 0
    # vega per 1% point: ≈ 0.199.
    assert v == pytest.approx(0.199, abs=1e-3)


def test_atm_theta_negative(kernel: BS76Kernel) -> None:
    # Long option → negative theta (time decay).
    t_call = kernel.theta(F=100, K=100, T=0.25, r=0.0, sigma=0.20, flag="c")
    t_put = kernel.theta(F=100, K=100, T=0.25, r=0.0, sigma=0.20, flag="p")
    assert t_call < 0
    assert t_put < 0


# ---------------------------------------------------------------------------
# Deep OTM / ITM
# ---------------------------------------------------------------------------


def test_deep_otm_call_small(kernel: BS76Kernel) -> None:
    # F=100, K=130 → very far OTM call.
    p = kernel.price_call(F=100, K=130, T=0.25, r=0.0, sigma=0.20)
    d = kernel.delta(F=100, K=130, T=0.25, r=0.0, sigma=0.20, flag="c")
    assert 0 < p < 0.05
    assert 0 < d < 0.05


def test_deep_itm_put_above_intrinsic(kernel: BS76Kernel) -> None:
    # F=100, K=130, put → deep ITM. Intrinsic = max(K-F, 0) = 30.
    p = kernel.price_put(F=100, K=130, T=0.25, r=0.0, sigma=0.20)
    intrinsic = 30.0
    assert p >= intrinsic - 1e-9  # never below intrinsic with r=0
    assert p < intrinsic + 1.0  # but not absurdly far above


# ---------------------------------------------------------------------------
# Mathematical invariants
# ---------------------------------------------------------------------------


def test_call_minus_put_equals_forward_minus_strike_at_r_zero(kernel: BS76Kernel) -> None:
    # Put-call parity (Black-76, r=0): C - P = F - K.
    F, K, T, sigma = 100.0, 110.0, 0.5, 0.25
    c = kernel.price_call(F=F, K=K, T=T, r=0.0, sigma=sigma)
    p = kernel.price_put(F=F, K=K, T=T, r=0.0, sigma=sigma)
    assert (c - p) == pytest.approx(F - K, abs=1e-12)


def test_gamma_call_equals_gamma_put(kernel: BS76Kernel) -> None:
    # Gamma is sign-insensitive (no flag dependency).
    g_atm = kernel.gamma(F=100, K=100, T=0.25, r=0.0, sigma=0.20)
    assert g_atm > 0
    # Gamma from the kernel signature has no flag, so this is trivially true,
    # but we still cross-check vs numerical second derivative of price.
    h = 1e-3
    p0 = kernel.price_call(100, 100, 0.25, 0, 0.20)
    p_up = kernel.price_call(100 + h, 100, 0.25, 0, 0.20)
    p_dn = kernel.price_call(100 - h, 100, 0.25, 0, 0.20)
    finite_gamma = (p_up - 2 * p0 + p_dn) / (h * h)
    assert g_atm == pytest.approx(finite_gamma, rel=1e-3)


def test_kernel_has_name_attribute() -> None:
    # pricer.py reads the kernel's class name into inputs_used.kernel.
    assert BS76Kernel().name == "BS76Kernel"
    assert type(BS76Kernel()).__name__ == "BS76Kernel"


def test_implied_vol_round_trip(kernel: BS76Kernel) -> None:
    sigma_true = 0.25
    p = kernel.price_call(F=100, K=100, T=0.5, r=0.0, sigma=sigma_true)
    iv = kernel.implied_vol(price=p, F=100, K=100, T=0.5, r=0.0, flag="c")
    assert math.isfinite(iv)
    assert iv == pytest.approx(sigma_true, abs=1e-5)
