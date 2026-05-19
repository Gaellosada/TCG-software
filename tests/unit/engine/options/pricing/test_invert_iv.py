"""IV inversion tests — round-trip + failure modes."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.pricing.pricer import (
    IV_ERROR_ABOVE_MAXIMUM,
    IV_ERROR_BELOW_INTRINSIC_DATA_QUALITY,
    IV_ERROR_DEEP_ITM_DEGENERATE,
    IV_ERROR_INVERT_FAILED,
    DefaultOptionsPricer,
)

from ._fixtures import make_contract, make_row


@pytest.fixture
def pricer() -> DefaultOptionsPricer:
    return DefaultOptionsPricer()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_atm_call(pricer: DefaultOptionsPricer) -> None:
    """Synthesize a price from sigma_true; invert; assert recovered sigma.

    Pricer's TTM is computed as ``(expiration - row.date).days / 365.0``, so
    we choose ``days`` that yield exactly the integer TTM the synthesis used —
    here ``days/365`` is the canonical T (no fractional-day mismatch).
    """
    sigma_true = 0.20
    F, K = 100.0, 100.0
    days = 91  # T = 91/365 ≈ 0.2493
    T = days / 365.0
    kernel = BS76Kernel()
    synthesized_price = kernel.price_call(F, K, T, 0.0, sigma_true)

    row_date = date(2024, 3, 22)
    expiry = date.fromordinal(row_date.toordinal() + days)
    assert (expiry - row_date).days == days  # convention guard

    contract = make_contract(strike=K, expiration=expiry, type_="C")
    row = make_row(row_date=row_date, mid=synthesized_price)
    result = pricer.invert_iv(contract, row, underlying_price=F)

    assert result.source == "computed"
    assert result.value is not None
    # Brief tolerance: 1e-5 (sigma_true == 0.20 ± 1e-5).
    assert result.value == pytest.approx(sigma_true, abs=1e-5)


def test_round_trip_otm_put(pricer: DefaultOptionsPricer) -> None:
    sigma_true = 0.30
    F, K = 100.0, 90.0
    days = 182
    T = days / 365.0
    kernel = BS76Kernel()
    price = kernel.price_put(F, K, T, 0.0, sigma_true)

    row_date = date(2024, 1, 1)
    expiry = date.fromordinal(row_date.toordinal() + days)
    contract = make_contract(strike=K, expiration=expiry, type_="P")
    row = make_row(row_date=row_date, mid=price)
    result = pricer.invert_iv(contract, row, underlying_price=F)
    assert result.source == "computed"
    assert result.value == pytest.approx(sigma_true, abs=1e-5)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_mid_none_no_quote_to_invert(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract()
    row = make_row(mid=None, bid=None, ask=None)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == "missing_iv_no_quote_to_invert"
    assert result.missing_inputs == ("iv", "bid", "ask")


def test_mid_zero_no_quote_to_invert(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract()
    row = make_row(mid=0.0)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == "missing_iv_no_quote_to_invert"


def test_below_intrinsic_deep_itm_call_uses_degenerate_code(
    pricer: DefaultOptionsPricer,
) -> None:
    """Deep ITM call (|F-K|/F > 0.3) where mid < intrinsic gets the
    deep-ITM-degenerate code.

    Common in production VIX chains: vendor EOD settle for deep ITM options
    sits a few cents below the discounted no-arb floor, py_vollib raises
    BelowIntrinsicException. We surface a friendlier code + message that
    explains the degenerate-IV situation rather than dumping the raw
    exception text.
    """
    # F=100, K=50 → deep ITM call, intrinsic ≈ 50, moneyness = 0.5 > 0.3.
    # Mid=1 << intrinsic so BelowIntrinsicException fires.
    contract = make_contract(strike=50.0, type_="C", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=1.0)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == IV_ERROR_DEEP_ITM_DEGENERATE
    assert result.missing_inputs == ("iv",)
    # Message stays within 2 sentences and includes the analytic limits the
    # user wanted in the tooltip.
    assert result.error_detail is not None
    assert "Deep ITM" in result.error_detail
    assert "delta" in result.error_detail.lower()
    # No raw exception class name leaks into the tooltip.
    assert "BelowIntrinsicException" not in result.error_detail
    assert result.error_detail.count(".") <= 2


def test_below_intrinsic_deep_itm_put_uses_degenerate_code(
    pricer: DefaultOptionsPricer,
) -> None:
    """Same routing for ITM puts (K > F) when moneyness > 0.3.

    This is the dominant failure mode in the production VIX chain because
    OPT_VIX has strikes up to 200 with the front-month future near 15, so
    most ITM contracts are puts.
    """
    # F=15, K=100 → deep ITM put, intrinsic ≈ 85. Moneyness = 85/15 ≈ 5.67 ≫ 0.3.
    contract = make_contract(strike=100.0, type_="P", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=80.0)
    result = pricer.invert_iv(contract, row, underlying_price=15.0)
    assert result.source == "missing"
    assert result.error_code == IV_ERROR_DEEP_ITM_DEGENERATE
    assert "Deep ITM" in (result.error_detail or "")


def test_below_intrinsic_slightly_itm_data_quality(
    pricer: DefaultOptionsPricer,
) -> None:
    """Shallow-ITM put (moneyness ≤ 0.3) with mid<intrinsic → data-quality code.

    F=100, K=105 puts have intrinsic ≈ 5 and moneyness = 0.05. A mid that
    sits below the discounted intrinsic is almost certainly a stale or bad
    bid/ask — NOT a degenerate-IV regime.
    """
    contract = make_contract(strike=105.0, type_="P", expiration=date(2024, 6, 21))
    # mid = 0.01 < intrinsic ≈ 5; triggers BelowIntrinsicException.
    row = make_row(row_date=date(2024, 3, 22), mid=0.01)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == IV_ERROR_BELOW_INTRINSIC_DATA_QUALITY
    assert result.error_detail is not None
    assert "below intrinsic" in result.error_detail.lower()
    # Not the deep-ITM message.
    assert "Deep ITM" not in result.error_detail


def test_above_maximum_call(pricer: DefaultOptionsPricer) -> None:
    """Mocked AboveMaximumException → ``missing_iv_above_maximum`` code."""
    # We need py_vollib to raise AboveMaximumException; rather than relying
    # on its precise upper bound, we mock the kernel to raise it explicitly.
    from py_lets_be_rational.exceptions import AboveMaximumException

    contract = make_contract(strike=50.0, type_="C", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=60.0)

    with patch.object(
        pricer.kernel,
        "implied_vol",
        side_effect=AboveMaximumException(),
    ):
        result = pricer.invert_iv(contract, row, underlying_price=100.0)

    assert result.source == "missing"
    assert result.error_code == IV_ERROR_ABOVE_MAXIMUM
    assert result.missing_inputs == ("iv",)
    assert result.error_detail is not None
    assert "no-arbitrage" in result.error_detail.lower()


def test_atm_below_intrinsic_data_quality(pricer: DefaultOptionsPricer) -> None:
    """ATM (F=K) with BelowIntrinsicException → data-quality code, NOT deep-ITM.

    Intrinsic is 0 for ATM, so any negative deviation is bad data.
    We mock the kernel to raise BelowIntrinsicException since py_vollib
    won't otherwise accept a negative mid.
    """
    from py_lets_be_rational.exceptions import BelowIntrinsicException

    contract = make_contract(strike=100.0, type_="C", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=0.01)

    with patch.object(
        pricer.kernel,
        "implied_vol",
        side_effect=BelowIntrinsicException(),
    ):
        result = pricer.invert_iv(contract, row, underlying_price=100.0)

    assert result.source == "missing"
    # F == K is NOT ITM → falls through to the data-quality bucket.
    assert result.error_code == IV_ERROR_BELOW_INTRINSIC_DATA_QUALITY
    assert "Deep ITM" not in (result.error_detail or "")


def test_unknown_exception_falls_through(pricer: DefaultOptionsPricer) -> None:
    """A non-py_vollib exception (e.g., NaN input) → invert_failed code."""
    contract = make_contract(strike=100.0, type_="C", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=4.0)

    with patch.object(
        pricer.kernel,
        "implied_vol",
        side_effect=RuntimeError("nan input encountered"),
    ):
        result = pricer.invert_iv(contract, row, underlying_price=100.0)

    assert result.source == "missing"
    assert result.error_code == IV_ERROR_INVERT_FAILED
    assert result.error_detail is not None
    assert "RuntimeError" in result.error_detail


def test_above_maximum_invert_failed(pricer: DefaultOptionsPricer) -> None:
    """A call price above F really does trigger py_vollib's
    AboveMaximumException; verify it routes to the new dedicated code.
    """
    # call price > F is impossible under r=0; py_vollib refuses.
    contract = make_contract(strike=100.0, type_="C", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=200.0)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == IV_ERROR_ABOVE_MAXIMUM
    assert result.error_detail is not None


# ---------------------------------------------------------------------------
# Blocked roots / gates also propagate through invert_iv
# ---------------------------------------------------------------------------


def test_invert_iv_opt_vix_without_forward_blocked(
    pricer: DefaultOptionsPricer,
) -> None:
    """Phase 2: OPT_VIX with no resolved forward (weekly — resolver
    returned None) propagates ``missing_forward_vix_curve`` through
    ``invert_iv`` via the OPT_VIX missing-underlying override.
    """
    contract = make_contract(collection="OPT_VIX")
    row = make_row()
    result = pricer.invert_iv(contract, row, underlying_price=None)
    assert result.source == "missing"
    assert result.error_code == "missing_forward_vix_curve"


def test_invert_iv_opt_vix_with_forward_computes(
    pricer: DefaultOptionsPricer,
) -> None:
    """Phase 2: OPT_VIX with a resolved FUT_VIX forward inverts IV like
    any other root.
    """
    contract = make_contract(collection="OPT_VIX", strike=15.0)
    row = make_row()
    result = pricer.invert_iv(contract, row, underlying_price=18.0)
    assert result.source == "computed"
    assert result.value is not None
    assert result.error_code is None


def test_invert_iv_missing_underlying(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract()
    row = make_row()
    result = pricer.invert_iv(contract, row, underlying_price=None)
    assert result.source == "missing"
    assert result.error_code == "missing_underlying_price"


def test_invert_iv_expired(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(expiration=date(2024, 1, 1))
    row = make_row(row_date=date(2024, 3, 22), mid=1.0)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == "expired_contract"
