"""Public Protocols for Module 2 (pricing).

`PricingKernel` is the swap point for the analytic kernel (Black-76 default).
`OptionsPricer` is the higher-level orchestrator that wraps a kernel and
emits `ComputeResult` envelopes per spec §3.2 / §4.4.

Independence contract: this module must NOT import from `tcg.data.*`.
The caller supplies `OptionContractDoc` + `OptionDailyRow` + `underlying_price`
(see brief and ORDERS Wave B1.2).
"""

from __future__ import annotations

from typing import Literal, Protocol, Sequence

from tcg.types.options import (
    ComputedGreeks,
    ComputeResult,
    GreekKind,
    OptionContractDoc,
    OptionDailyRow,
)


class PricingKernel(Protocol):
    """Pure-math analytic kernel. No DTOs, no I/O.

    Convention notes (mirroring `BasicBlackScholes.java`):
    - `T` is in years.
    - `r` is the continuously-compounded risk-free rate (Phase 1: 0.0).
    - `sigma` is the annualized volatility.
    - `theta` is per **calendar day** (Java `calcTheta` divides by 365).
    - `vega` is per **1 percentage-point** of vol (Java `calcVega` divides by 100).
    """

    def price_call(self, F: float, K: float, T: float, r: float, sigma: float) -> float: ...
    def price_put(self, F: float, K: float, T: float, r: float, sigma: float) -> float: ...
    def delta(
        self, F: float, K: float, T: float, r: float, sigma: float, flag: Literal["c", "p"]
    ) -> float: ...
    def gamma(self, F: float, K: float, T: float, r: float, sigma: float) -> float: ...
    def theta(
        self, F: float, K: float, T: float, r: float, sigma: float, flag: Literal["c", "p"]
    ) -> float: ...
    def vega(self, F: float, K: float, T: float, r: float, sigma: float) -> float: ...
    def implied_vol(
        self,
        price: float,
        F: float,
        K: float,
        T: float,
        r: float,
        flag: Literal["c", "p"],
    ) -> float: ...


class OptionsPricer(Protocol):
    """High-level pricer that wraps a `PricingKernel` and emits provenance."""

    def compute(
        self,
        contract: OptionContractDoc,
        row: OptionDailyRow,
        underlying_price: float | None,
        which: Sequence[GreekKind] = (
            GreekKind.IV,
            GreekKind.DELTA,
            GreekKind.GAMMA,
            GreekKind.THETA,
            GreekKind.VEGA,
        ),
    ) -> ComputedGreeks: ...

    def invert_iv(
        self,
        contract: OptionContractDoc,
        row: OptionDailyRow,
        underlying_price: float | None,
    ) -> ComputeResult: ...
