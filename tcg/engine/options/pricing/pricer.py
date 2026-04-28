"""DefaultOptionsPricer — wraps a ``PricingKernel`` and emits ``ComputeResult``.

Phase 1 scope (per spec §3.2 + LEGACY_FINDINGS §2.3): all unblocked roots
(SP_500, NASDAQ_100, T_NOTE/T_BOND when verified, GOLD, EURUSD/JPYUSD when
verified, BTC) use Black-76. With ``r=0``, Black-76(F=S) ≡ Black-Scholes(S).
The caller passes a joined ``underlying_price``; Module 2 treats it as the
forward price ``F`` regardless of whether the underlying is a future or a spot
index. Module 6 (`tcg.engine.options.chain`) is responsible for the join.

Invariants (per brief / guardrails):
- ``r=0.0`` hardcoded, surfaced via ``inputs_used.r=0.0`` on every successful
  compute (guardrail #5).
- OPT_VIX → ``error_code="missing_forward_vix_curve"`` for all 5 fields
  (guardrail #6). No Black-76 fallback.
- OPT_ETH → ``error_code="missing_deribit_feed"`` for all 5 fields.
- ``underlying_price is None`` → ``error_code="missing_underlying_price"``.
- TTM ≤ 0 → ``error_code="expired_contract"``.
- Root in the strike-factor-gated set with ``strike_factor_verified=False`` →
  ``error_code="strike_factor_unverified"``.
- ``ComputeResult.source ∈ {"computed", "missing"}`` only — never ``"stored"``.
  The widening to ``"stored"`` is Module 6's responsibility (spec §3.6 / §4.4).
- ``which`` filter: a Greek not in ``which`` is returned as
  ``ComputeResult(value=None, source="missing", error_code="not_requested",
  missing_inputs=())`` so it is distinguishable from a real failure.

Module 2 receives all data; **does NOT import from ``tcg.data.*``** (guardrail
#2 + import-linter ``engine-data-isolation`` independence contract).
"""

from __future__ import annotations

from typing import Sequence

from tcg.engine.options.pricing._gating import (
    is_blocked_root,
    needs_strike_factor_verification,
    sign_for_type,
    time_to_expiry_years,
)
from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.pricing.protocol import OptionsPricer, PricingKernel
from tcg.types.options import (
    ComputedGreeks,
    ComputeResult,
    GreekKind,
    OptionContractDoc,
    OptionDailyRow,
)

# Phase 1 hardcoded risk-free rate. Surfaced via inputs_used.r per guardrail #5.
_RISK_FREE_RATE: float = 0.0

_ALL_GREEKS: tuple[GreekKind, ...] = (
    GreekKind.IV,
    GreekKind.DELTA,
    GreekKind.GAMMA,
    GreekKind.THETA,
    GreekKind.VEGA,
)


def _missing(
    error_code: str,
    missing_inputs: tuple[str, ...],
    error_detail: str | None = None,
) -> ComputeResult:
    """Build a ``source="missing"`` ComputeResult."""
    return ComputeResult(
        value=None,
        source="missing",
        model=None,
        inputs_used=None,
        missing_inputs=missing_inputs,
        error_code=error_code,
        error_detail=error_detail,
    )


def _not_requested() -> ComputeResult:
    """Sentinel for Greeks the caller did not ask for via ``which``."""
    return _missing("not_requested", ())


def _all_missing(error_code: str, missing_inputs: tuple[str, ...]) -> ComputedGreeks:
    """Build a ``ComputedGreeks`` whose 5 fields are all the same missing reason."""
    r = _missing(error_code, missing_inputs)
    return ComputedGreeks(iv=r, delta=r, gamma=r, theta=r, vega=r)


class DefaultOptionsPricer(OptionsPricer):
    """Default implementation. Wraps a ``PricingKernel`` (default: ``BS76Kernel``).

    No I/O, no Mongo, no fetching. Caller provides every input.
    """

    def __init__(self, kernel: PricingKernel | None = None) -> None:
        self.kernel: PricingKernel = kernel if kernel is not None else BS76Kernel()
        self._kernel_name: str = type(self.kernel).__name__
        self._model_name: str = "Black-76"

    # ---- compute ----------------------------------------------------------

    def compute(
        self,
        contract: OptionContractDoc,
        row: OptionDailyRow,
        underlying_price: float | None,
        which: Sequence[GreekKind] = _ALL_GREEKS,
    ) -> ComputedGreeks:
        # 1) Blocked roots (OPT_VIX / OPT_ETH) — short-circuit, all 5 missing.
        blocked, blocked_code, blocked_missing = is_blocked_root(contract.collection)
        if blocked:
            assert blocked_code is not None  # pragma: no cover (guarded by helper)
            return _all_missing(blocked_code, blocked_missing)

        # 2) Strike-factor unverified gate.
        if (
            needs_strike_factor_verification(contract.collection)
            and not contract.strike_factor_verified
        ):
            return _all_missing("strike_factor_unverified", ("strike_factor",))

        # 3) Underlying price.
        if underlying_price is None:
            return _all_missing("missing_underlying_price", ("underlying_price",))

        # 4) Time-to-expiry.
        T = time_to_expiry_years(contract.expiration, row.date)
        if T <= 0.0:
            return _all_missing("expired_contract", ("time_to_expiry",))

        # 5) IV — needed for any Greek other than IV itself.
        wanted = set(which)
        iv_result = self._compute_iv(contract, row, underlying_price, T)

        if iv_result.source != "computed":
            # No usable IV → propagate the same missing reason to all requested Greeks.
            assert iv_result.error_code is not None
            iv_for_greeks_missing = _missing(
                iv_result.error_code,
                iv_result.missing_inputs or (),
                iv_result.error_detail,
            )
            return ComputedGreeks(
                iv=iv_result if GreekKind.IV in wanted else _not_requested(),
                delta=iv_for_greeks_missing if GreekKind.DELTA in wanted else _not_requested(),
                gamma=iv_for_greeks_missing if GreekKind.GAMMA in wanted else _not_requested(),
                theta=iv_for_greeks_missing if GreekKind.THETA in wanted else _not_requested(),
                vega=iv_for_greeks_missing if GreekKind.VEGA in wanted else _not_requested(),
            )

        # 6) Compute the Greeks.
        assert iv_result.value is not None  # narrowed by source=="computed" check
        iv_value = float(iv_result.value)
        flag = sign_for_type(contract.type)
        F = float(underlying_price)
        K = float(contract.strike)

        def _ok(value: float) -> ComputeResult:
            return ComputeResult(
                value=float(value),
                source="computed",
                model=self._model_name,
                inputs_used={
                    "underlying_price": F,
                    "iv": iv_value,
                    "ttm": T,
                    "r": _RISK_FREE_RATE,
                    "sign": flag,
                    "kernel": self._kernel_name,
                },
                missing_inputs=None,
                error_code=None,
                error_detail=None,
            )

        delta_r = (
            _ok(self.kernel.delta(F, K, T, _RISK_FREE_RATE, iv_value, flag))
            if GreekKind.DELTA in wanted
            else _not_requested()
        )
        gamma_r = (
            _ok(self.kernel.gamma(F, K, T, _RISK_FREE_RATE, iv_value))
            if GreekKind.GAMMA in wanted
            else _not_requested()
        )
        theta_r = (
            _ok(self.kernel.theta(F, K, T, _RISK_FREE_RATE, iv_value, flag))
            if GreekKind.THETA in wanted
            else _not_requested()
        )
        vega_r = (
            _ok(self.kernel.vega(F, K, T, _RISK_FREE_RATE, iv_value))
            if GreekKind.VEGA in wanted
            else _not_requested()
        )
        iv_out = iv_result if GreekKind.IV in wanted else _not_requested()

        return ComputedGreeks(iv=iv_out, delta=delta_r, gamma=gamma_r, theta=theta_r, vega=vega_r)

    # ---- invert_iv (public) -----------------------------------------------

    def invert_iv(
        self,
        contract: OptionContractDoc,
        row: OptionDailyRow,
        underlying_price: float | None,
    ) -> ComputeResult:
        # 1) Blocked roots first.
        blocked, blocked_code, blocked_missing = is_blocked_root(contract.collection)
        if blocked:
            assert blocked_code is not None
            return _missing(blocked_code, blocked_missing)

        # 2) Strike-factor gate.
        if (
            needs_strike_factor_verification(contract.collection)
            and not contract.strike_factor_verified
        ):
            return _missing("strike_factor_unverified", ("strike_factor",))

        # 3) Underlying price.
        if underlying_price is None:
            return _missing("missing_underlying_price", ("underlying_price",))

        # 4) TTM.
        T = time_to_expiry_years(contract.expiration, row.date)
        if T <= 0.0:
            return _missing("expired_contract", ("time_to_expiry",))

        return self._compute_iv(contract, row, float(underlying_price), T)

    # ---- internal: IV inversion ------------------------------------------

    def _compute_iv(
        self,
        contract: OptionContractDoc,
        row: OptionDailyRow,
        underlying_price: float,
        T: float,
    ) -> ComputeResult:
        """Invert IV from ``row.mid``. Caller has already gated blocked-root etc."""
        mid = row.mid
        if mid is None or mid <= 0:
            return _missing("missing_iv_no_quote_to_invert", ("iv", "bid", "ask"))

        flag = sign_for_type(contract.type)
        F = float(underlying_price)
        K = float(contract.strike)
        try:
            iv_value = self.kernel.implied_vol(
                price=float(mid),
                F=F,
                K=K,
                T=T,
                r=_RISK_FREE_RATE,
                flag=flag,
            )
        except Exception as exc:  # noqa: BLE001 — py_vollib raises a hierarchy
            detail = f"{type(exc).__name__}: {exc}"
            return _missing("missing_iv_invert_failed", ("iv",), error_detail=detail)

        # Some py_vollib paths return NaN or a sentinel non-finite value rather
        # than raising; treat as failure.
        if not (iv_value == iv_value) or iv_value <= 0:  # NaN-safe check
            return _missing(
                "missing_iv_invert_failed",
                ("iv",),
                error_detail=f"non-finite or non-positive IV ({iv_value!r})",
            )

        return ComputeResult(
            value=float(iv_value),
            source="computed",
            model=self._model_name,
            inputs_used={
                "underlying_price": F,
                "iv": float(iv_value),
                "ttm": T,
                "r": _RISK_FREE_RATE,
                "sign": flag,
                "kernel": self._kernel_name,
            },
            missing_inputs=None,
            error_code=None,
            error_detail=None,
        )
