"""Black-76 pricing kernel — port of `BasicBlackScholes.java` (~lines 26-119).

Ported from
``trajectoirecap.platform.parent/simulator/src/main/java/com/simulator/statmath/
BasicBlackScholes.java``.

Java conventions preserved verbatim (LEGACY_FINDINGS §2.3):
- ``theta`` per **calendar day** (Java line 70 divides by 365.0).
- ``vega`` per **1 percentage-point** of vol (Java line 80 divides by 100.0).
- ``gamma`` standard (no scaling).
- ``delta`` returned in [-1, 1] (call) and [-1, 0] (put).

In Black-76 the dividend yield is folded into the forward, so the Java port's
``d`` (continuous dividend) is **not** a free parameter here — Black-76 = the
Java ``calculateBlack`` family, which uses ``d=0`` implicitly. With Phase 1's
``r=0`` mandate the discount factor ``exp(-rT)`` is identically 1, so call and
put prices match standard Black-76 with no discounting (cf. Java :112-119).

`implied_vol` delegates to ``py_vollib.black.implied_volatility``
(Peter Jäckel "Let's Be Rational"; LEGACY_FINDINGS §2.3 recommends this over a
hand port).  The fast ``py_vollib.black`` C-extension impl is used in place of
the pure-Python ``py_vollib.ref_python.black`` reference impl — the two are
drop-in compatible and the kernel matches both to 1e-12 (verified by
``test_golden.py``).
"""

from __future__ import annotations

import math
from typing import Literal

from py_vollib.black.implied_volatility import (
    implied_volatility as _vollib_implied_volatility,
)

from tcg.engine.options.pricing.protocol import PricingKernel


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF. Mirrors `NormalDistribution.norm_cdf` in the Java port."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard-normal PDF. Mirrors `NormalDistribution.norm_pdf` in the Java port.

    Equivalent to `BasicBlackScholes.normalDensity` (Java :36-41).
    """
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(F: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """Compute (d1, d2) under Black-76 (no dividend term: ``d=0``).

    port: BasicBlackScholes.java:113-114 (``calculateBlack(...)`` Black-76 form).
    Note: the Java :26-28 ``calcD1`` carries an extra ``-d`` term; we drop it
    because Black-76 has no separate dividend yield (it is folded into ``F``).
    """
    sqrtT = math.sqrt(T)
    d1 = (math.log(F / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return d1, d2


class BS76Kernel(PricingKernel):
    """Default Black-76 pricing kernel.

    Pure NumPy/Python; no I/O, no DTO awareness. All inputs are scalar floats.
    """

    name: str = "BS76Kernel"

    # ---- prices -----------------------------------------------------------

    def price_call(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        # port: BasicBlackScholes.java:84-92 (``calculateBlack`` from N(d1)/N(d2))
        # and :112-119 (Black-76 wrapper that builds d1/d2).
        d1, d2 = _d1_d2(F, K, T, r, sigma)
        return math.exp(-r * T) * (F * _norm_cdf(d1) - K * _norm_cdf(d2))

    def price_put(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        # port: BasicBlackScholes.java:84-92 (PUT branch: K*(1-N(d2)) - F*(1-N(d1))).
        d1, d2 = _d1_d2(F, K, T, r, sigma)
        return math.exp(-r * T) * (K * (1.0 - _norm_cdf(d2)) - F * (1.0 - _norm_cdf(d1)))

    # ---- greeks -----------------------------------------------------------

    def delta(
        self, F: float, K: float, T: float, r: float, sigma: float, flag: Literal["c", "p"]
    ) -> float:
        # port: BasicBlackScholes.java:44-49. With d=0 the dividend factor
        # exp(t*d*-1) collapses to 1, leaving N(d1) for calls and N(d1)-1 for puts.
        # Black-76 also discounts delta by exp(-rT); with Phase 1's r=0 that is 1.
        d1, _ = _d1_d2(F, K, T, r, sigma)
        nd1 = _norm_cdf(d1)
        disc = math.exp(-r * T)
        if flag == "c":
            return disc * nd1
        return disc * (nd1 - 1.0)

    def gamma(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        # port: BasicBlackScholes.java:52-54. With d=0 the dividend factor → 1.
        # Black-76 multiplies by exp(-rT); with r=0 → 1.
        d1, _ = _d1_d2(F, K, T, r, sigma)
        return math.exp(-r * T) * _norm_pdf(d1) / (F * sigma * math.sqrt(T))

    def theta(
        self, F: float, K: float, T: float, r: float, sigma: float, flag: Literal["c", "p"]
    ) -> float:
        # port: BasicBlackScholes.java:60-74. Per-day (Java line 70 divides by 365.0).
        # The Java port is generalized Black-Scholes-Merton with separate
        # dividend yield ``d``; Black-76 substitutes ``d = r`` so that the
        # forward F earns the risk-free rate (the no-arbitrage forward
        # embeds carry). With d=r the Java formula simplifies to py_vollib's
        # Black-76 theta:
        #     theta_call = -(F*exp(-rT)*pd1*sigma/(2*sqrtT)
        #                    - r*F*exp(-rT)*N(d1)
        #                    + r*K*exp(-rT)*N(d2)) / 365
        #     theta_put  = (-F*exp(-rT)*pd1*sigma/(2*sqrtT)
        #                   - r*F*exp(-rT)*N(-d1)
        #                   + r*K*exp(-rT)*N(-d2)) / 365
        # With Phase 1's r=0 this collapses to -F*pd1*sigma/(2*sqrtT)/365 for both
        # call and put. Verified against py_vollib at multiple (r, sigma, T) in
        # test_golden.py to tol 1e-5.
        d1, d2 = _d1_d2(F, K, T, r, sigma)
        pd1 = _norm_pdf(d1)
        disc_r = math.exp(-r * T)
        two_sqrtT = 2.0 * math.sqrt(T)

        first_term = F * disc_r * pd1 * sigma / two_sqrtT
        if flag == "c":
            second_term = -r * F * disc_r * _norm_cdf(d1)
            third_term = r * K * disc_r * _norm_cdf(d2)
            return -(first_term + second_term + third_term) / 365.0
        # put branch
        second_term = -r * F * disc_r * _norm_cdf(-d1)
        third_term = r * K * disc_r * _norm_cdf(-d2)
        return (-first_term + second_term + third_term) / 365.0

    def vega(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        # port: BasicBlackScholes.java:79-81. Per 1 percentage-point of sigma
        # (line 80 divides by 100.0). Black-76 multiplies by exp(-rT); with r=0 → 1.
        d1, _ = _d1_d2(F, K, T, r, sigma)
        return F * math.exp(-r * T) * _norm_pdf(d1) * math.sqrt(T) / 100.0

    # ---- IV inversion -----------------------------------------------------

    def implied_vol(
        self,
        price: float,
        F: float,
        K: float,
        T: float,
        r: float,
        flag: Literal["c", "p"],
    ) -> float:
        """Delegate to py_vollib's "Let's Be Rational" implementation.

        Signature note: ``py_vollib.black.implied_volatility`` expects
        ``(price, F, K, r, t, flag)`` (note the ``r``-then-``t`` ordering).
        Raises ``BelowIntrinsicException`` /
        ``AboveMaximumException`` when no real solution exists; caller wraps
        these into ``error_code="missing_iv_invert_failed"``.
        """
        return float(_vollib_implied_volatility(price, F, K, r, T, flag))
