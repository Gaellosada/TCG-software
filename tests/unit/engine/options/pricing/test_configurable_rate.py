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

KERNEL d1 BUG — FIXED in PR #56 (Gael-approved):
  ``BS76Kernel._d1_d2`` previously used ``d1 = (ln(F/K) + (r + 0.5σ²)T)/(σ√T)``
  — the Black-SCHOLES (spot) convention, which carries an extra ``r`` term.
  TRUE Black-76 (py_vollib, Hull) uses ``d1 = (ln(F/K) + 0.5σ²T)/(σ√T)`` — no
  ``r`` in the numerator, because F is the forward (carry is already in F).
  At r=0 the two coincide (the rT term vanishes), so every r=0 golden passed
  and the bug was invisible; at r=0.04 the kernel's delta was 0.5540 vs the
  correct 0.5148. The fix dropped the ``r +`` term (rate still enters via the
  ``exp(-rT)`` discount). The r=0.04 correctness goldens below now PASS;
  ``test_kernel_greeks_at_r004_match_py_vollib_golden`` and
  ``test_kernel_delta_agrees_with_py_vollib_at_nonzero_r`` are the regression
  guards. Every r=0 value (incl. delta 0.5199388058383725) is byte-identical.
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


# --- Correctness goldens at r=0.04 (kernel d1 bug FIXED in PR #56) ----------
# These pin the TRUE Black-76 values (py_vollib). Two layers:
#   * kernel-level (exact inputs, no IV inversion) → must match py_vollib to 1e-9.
#     This is the real regression guard against any return of the spot-d1 bug.
#   * pricer end-to-end (compute() inverts IV from a mid first) → looser tol,
#     because the inverted IV (~0.20027) carries the mid's float-rounding
#     residual; this checks the rate flows correctly through the full path.
# The previous revision marked these xfail(strict) pending the kernel fix.


def test_kernel_greeks_at_r004_match_py_vollib_golden() -> None:
    """The corrected kernel must match py_vollib's Black-76 greeks at r=0.04 to
    1e-9 at the exact golden inputs (F=K=100, T=0.25, sigma=0.20, call).

    This is the definitive regression guard for the d1 fix: it isolates the
    kernel from IV inversion, so any reintroduction of the spurious ``+rT``
    term in d1 (which moved delta to ~0.5540) fails loudly here.
    """
    from py_vollib.black.greeks.analytical import delta, gamma, theta, vega

    from tcg.engine.options.pricing.kernel import BS76Kernel

    k = BS76Kernel()
    F, K, T, r, sigma, flag = 100.0, 100.0, 0.25, 0.04, 0.20, "c"

    assert k.delta(F, K, T, r, sigma, flag) == pytest.approx(
        _EXPECTED_DELTA_R04, abs=1e-9
    )
    assert k.gamma(F, K, T, r, sigma) == pytest.approx(_EXPECTED_GAMMA_R04, abs=1e-9)
    assert k.theta(F, K, T, r, sigma, flag) == pytest.approx(
        _EXPECTED_THETA_R04, abs=1e-9
    )
    assert k.vega(F, K, T, r, sigma) == pytest.approx(_EXPECTED_VEGA_R04, abs=1e-9)

    # Cross-check the goldens themselves against py_vollib (independent impl).
    assert _EXPECTED_DELTA_R04 == pytest.approx(
        delta(flag, F, K, T, r, sigma), abs=1e-9
    )
    assert _EXPECTED_GAMMA_R04 == pytest.approx(
        gamma(flag, F, K, T, r, sigma), abs=1e-9
    )
    assert _EXPECTED_THETA_R04 == pytest.approx(
        theta(flag, F, K, T, r, sigma), abs=1e-9
    )
    assert _EXPECTED_VEGA_R04 == pytest.approx(vega(flag, F, K, T, r, sigma), abs=1e-9)


def test_delta_at_r004_matches_golden() -> None:
    """End-to-end delta = exp(-rT)*N(d1) at r=0.04 via compute() (IV inverted)."""
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    # Tol 1e-3: the inverted IV (~0.20027) differs from the golden's exact 0.20
    # by the mid's float-rounding residual; kernel correctness is pinned to
    # 1e-9 above. Asserts delta is ~0.5148 (Black-76), NOT the buggy ~0.5540.
    assert g.delta.value == pytest.approx(_EXPECTED_DELTA_R04, abs=1e-3)
    assert g.delta.value < 0.52  # would be ~0.554 under the old spot-d1 bug


def test_gamma_at_r004_matches_golden() -> None:
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.gamma.value == pytest.approx(_EXPECTED_GAMMA_R04, abs=1e-3)


def test_theta_at_r004_matches_golden() -> None:
    """theta differs from r=0 by the carry term — golden via py_vollib."""
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.theta.value == pytest.approx(_EXPECTED_THETA_R04, abs=1e-3)


def test_vega_at_r004_matches_golden() -> None:
    pricer = DefaultOptionsPricer(risk_free_rate=0.04)
    g = pricer.compute(_atm_3m_call_contract(), _atm_3m_call_row(), underlying_price=_F)
    assert g.vega.value == pytest.approx(_EXPECTED_VEGA_R04, abs=1e-3)


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
# Regression guard for the d1 fix (PR #56): the kernel must AGREE with the true
# Black-76 (py_vollib) delta at r != 0, not just at r=0. Before the fix the
# kernel used the Black-Scholes spot d1 (extra +rT) and delta diverged to
# ~0.5540 at r=0.04. This test locks the fix in permanently.
# ---------------------------------------------------------------------------


def test_kernel_delta_agrees_with_py_vollib_at_nonzero_r() -> None:
    """The corrected kernel delta must equal py_vollib's Black-76 delta at both
    r=0 and r!=0, across calls and puts. Guards against any reintroduction of
    the spurious ``+rT`` term in d1.
    """
    from py_vollib.black.greeks.analytical import delta as vollib_delta

    from tcg.engine.options.pricing.kernel import BS76Kernel

    k = BS76Kernel()
    cases = [
        (100.0, 100.0, 0.25, 0.0, 0.20, "c"),
        (100.0, 100.0, 0.25, 0.04, 0.20, "c"),
        (100.0, 100.0, 0.50, 0.05, 0.20, "c"),
        (100.0, 110.0, 0.50, 0.05, 0.20, "p"),
        (100.0, 90.0, 1.0, 0.03, 0.30, "p"),
    ]
    for F, K, T, r, sigma, flag in cases:
        kernel_d = k.delta(F, K, T, r, sigma, flag)  # type: ignore[arg-type]
        vollib_d = vollib_delta(flag, F, K, T, r, sigma)
        assert kernel_d == pytest.approx(vollib_d, abs=1e-9), (
            f"kernel delta diverges from Black-76 at r={r} "
            f"(F={F},K={K},T={T},sigma={sigma},{flag}): "
            f"kernel={kernel_d}, vollib={vollib_d} — d1 bug regressed?"
        )
