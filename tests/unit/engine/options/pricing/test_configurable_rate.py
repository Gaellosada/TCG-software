"""Configurable risk-free-rate tests for ``DefaultOptionsPricer``.

The kernel already takes ``r`` on every function and implements the
``exp(-rT)`` discount; only the wrapper used to pin ``r=0.0``. These tests
prove the wrapper now lets a caller choose ``r`` (default 0.0 preserved) and
that the chosen rate flows into every kernel call + ``inputs_used.r``.

Golden values computed independently via py_vollib (black + analytical
greeks) at r=0.04 on 2026-06-13:

    F=K=100, T=0.25, sigma=0.20, flag="c"
      price 3.9480822810875207
      delta 0.5147653282800216
      gamma 0.039447933090788895
      theta -0.02118263925181994
      vega  0.19723966545394447

The default-r=0 figures (delta 0.5199388058383725, etc.) live in
``test_golden.py`` and MUST remain byte-identical — see
``test_default_rate_delta_unchanged_byte_identical``.

KNOWN KERNEL BUG (out of scope for this rate-plumbing task — flagged to Gael):
  ``BS76Kernel._d1_d2`` uses ``d1 = (ln(F/K) + (r + 0.5 sigma^2) T)/(sigma√T)``
  — the Black-SCHOLES (spot) convention, which carries an extra ``r`` term.
  TRUE Black-76 (py_vollib, Hull) uses ``d1 = (ln(F/K) + 0.5 sigma^2 T)/(sigma√T)``
  — no ``r`` in the numerator, because F is the forward (carry is already in F).
  At r=0 the two coincide (the rT term vanishes), so every existing golden
  passes and the bug is invisible. At r=0.04 the kernel's delta (0.5540)
  diverges from the correct Black-76 delta (0.5148). Since the dwh VIX greeks
  backfill will call this pricer at r=0.04, this MUST be fixed before any
  production greek write — but fixing the kernel math is OUT of this task's
  scope (Sign 4: reuse, don't reinvent the model) and is a model-convention
  decision (Sign 5: HOLD for Gael). The four ``*_matches_golden`` tests below
  are therefore xfail(strict=True): they pin the correct py_vollib values and
  will flip to PASS the instant the kernel d1 is corrected, forcing removal of
  the xfail marker.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.engine.options.pricing.pricer import DefaultOptionsPricer
from tcg.types.options import GreekKind

from ._fixtures import make_contract, make_row

# Golden greeks at r=0.04 for an ATM 3M call with sigma=0.20, F=K=100.
# Independently computed via py_vollib analytical greeks (see module docstring).
_R = 0.04
_F = 100.0
_K = 100.0
_EXPECTED_DELTA_R04 = 0.5147653282800216
_EXPECTED_GAMMA_R04 = 0.039447933090788895
_EXPECTED_THETA_R04 = -0.02118263925181994
_EXPECTED_VEGA_R04 = 0.19723966545394447

# Same contract priced at r=0 (the existing default) — from test_golden.py.
_EXPECTED_DELTA_R0 = 0.5199388058383725


def _atm_3m_call_contract():
    return make_contract(
        collection="OPT_SP_500",
        strike=_K,
        expiration=date(2024, 6, 21),
        type_="C",
    )


def _atm_3m_call_row():
    # mid is the r=0.04 fair value so IV inverts back to ~0.20 at r=0.04.
    return make_row(row_date=date(2024, 3, 22), mid=3.9480822810875207)


def test_pricer_accepts_risk_free_rate_kwarg() -> None:
    """A caller can construct the pricer with a chosen rate."""
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.delta.source == "computed", g.delta
    assert g.delta.inputs_used is not None
    assert g.delta.inputs_used["r"] == 0.04


def test_inputs_used_surfaces_chosen_rate() -> None:
    """The chosen r is surfaced on every successful greek's inputs_used."""
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    for r in (g.iv, g.delta, g.gamma, g.theta, g.vega):
        assert r.inputs_used is not None, r
        assert r.inputs_used["r"] == 0.04


# --- Correctness goldens at r=0.04 (xfail: blocked by kernel d1 bug) --------
# These pin the TRUE Black-76 values (py_vollib). They fail today ONLY because
# of the kernel d1 bug documented in the module docstring, NOT because of the
# rate plumbing (which is verified by the tests above + below). strict=True so
# they convert to a hard failure the moment the kernel is fixed.
_KERNEL_D1_BUG = (
    "blocked by BS76Kernel d1 bug: kernel uses the Black-Scholes spot d1 "
    "(extra +rT term) instead of the Black-76 forward d1; correct only at "
    "r=0. Out of scope for the rate-plumbing task; HOLD for Gael (Sign 4/5)."
)


@pytest.mark.xfail(strict=True, reason=_KERNEL_D1_BUG)
def test_delta_at_r004_matches_golden() -> None:
    """delta = exp(-rT)*N(d1) at r=0.04 — golden via py_vollib."""
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.delta.value == pytest.approx(_EXPECTED_DELTA_R04, abs=1e-3)


@pytest.mark.xfail(strict=True, reason=_KERNEL_D1_BUG)
def test_gamma_at_r004_matches_golden() -> None:
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.gamma.value == pytest.approx(_EXPECTED_GAMMA_R04, abs=1e-4)


@pytest.mark.xfail(strict=True, reason=_KERNEL_D1_BUG)
def test_theta_at_r004_matches_golden() -> None:
    """theta differs from r=0 by the carry term — golden via py_vollib."""
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.theta.value == pytest.approx(_EXPECTED_THETA_R04, abs=1e-4)


@pytest.mark.xfail(strict=True, reason=_KERNEL_D1_BUG)
def test_vega_at_r004_matches_golden() -> None:
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.vega.value == pytest.approx(_EXPECTED_VEGA_R04, abs=1e-4)


def test_chosen_rate_changes_delta_vs_default() -> None:
    """r=0.04 must produce a different delta than the r=0 default (proves the
    discount factor actually flows through, not just into inputs_used)."""
    g_r0 = DefaultOptionsPricer().compute(
        _atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F
    )
    g_r04 = DefaultOptionsPricer(risk_free_rate=0.04).compute(
        _atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F
    )
    assert g_r0.delta.value is not None and g_r04.delta.value is not None
    assert abs(g_r0.delta.value - g_r04.delta.value) > 1e-4


def test_default_rate_is_zero() -> None:
    """Backward compatible: omitting the kwarg keeps r=0.0."""
    pricer = DefaultOptionsPricer()
    g = pricer.compute(
        _atm_3m_call_contract(),
        make_row(row_date=date(2024, 3, 22), mid=3.99),
        underlying_price=_F,
    )
    assert g.delta.inputs_used is not None
    assert g.delta.inputs_used["r"] == 0.0


def test_default_rate_delta_unchanged_byte_identical() -> None:
    """The r=0 default delta must stay byte-identical to the pinned golden.

    Price the option at its r=0 fair value (3.9877611676744933) so IV inverts
    to exactly 0.20, then delta must equal the test_golden.py pinned value.
    """
    pricer = DefaultOptionsPricer()
    g = pricer.compute(
        _atm_3m_call_contract(),
        make_row(row_date=date(2024, 3, 22), mid=3.9877611676744933),
        underlying_price=_F,
    )
    assert g.delta.value == pytest.approx(_EXPECTED_DELTA_R0, abs=1e-9)


def test_invert_iv_uses_chosen_rate() -> None:
    """invert_iv must invert at the chosen r: a mid priced at r=0.04 inverts
    back to ~0.20 only when the pricer uses r=0.04."""
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    res = pricer.invert_iv(
        _atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F
    )
    assert res.source == "computed", res
    # ~1e-3: py_vollib inverts with its OWN (correct) d1, so the round-trip is
    # accurate; the small residual is the mid's float rounding, NOT the kernel
    # d1 bug (which affects the analytical greeks, not py_vollib's IV solver).
    assert res.value == pytest.approx(0.20, abs=2e-3)
    assert res.inputs_used is not None
    assert res.inputs_used["r"] == 0.04


# ---------------------------------------------------------------------------
# Pin the kernel d1 bug precisely (passes today; documents the divergence).
# Delete this test together with the xfail markers when the kernel is fixed.
# ---------------------------------------------------------------------------


def test_kernel_d1_bug_pinned_delta_diverges_from_py_vollib_at_r004() -> None:
    """At r=0.04 the kernel delta diverges from the true Black-76 delta.

    This documents the known kernel d1 bug at the kernel level so the rate
    plumbing's correctness is unambiguous: the plumbing feeds r through fine;
    it is the kernel math that is wrong at r != 0. At r=0 they must agree.
    """
    from py_vollib.black.greeks.analytical import delta as vollib_delta

    from tcg.engine.options.pricing.kernel import BS76Kernel

    k = BS76Kernel()
    F, K, T, sigma = 100.0, 100.0, 0.25, 0.20

    # r=0: kernel == py_vollib (existing behaviour, must hold).
    assert k.delta(F, K, T, 0.0, sigma, "c") == pytest.approx(
        vollib_delta("c", F, K, T, 0.0, sigma), abs=1e-12
    )

    # r=0.04: kernel diverges from py_vollib by the spurious +rT-in-d1 term.
    kernel_d = k.delta(F, K, T, 0.04, sigma, "c")
    vollib_d = vollib_delta("c", F, K, T, 0.04, sigma)
    assert abs(kernel_d - vollib_d) > 0.03, (
        "Expected the documented kernel d1 bug to make delta diverge at "
        f"r=0.04; if this now agrees, the kernel was fixed — remove the "
        f"xfail markers and this test. kernel={kernel_d}, vollib={vollib_d}"
    )
