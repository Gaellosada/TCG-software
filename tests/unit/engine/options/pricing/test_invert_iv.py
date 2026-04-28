"""IV inversion tests — round-trip + failure modes."""

from __future__ import annotations

from datetime import date

import pytest

from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.pricing.pricer import DefaultOptionsPricer

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


def test_below_intrinsic_invert_failed(pricer: DefaultOptionsPricer) -> None:
    """Price below intrinsic triggers py_vollib's BelowIntrinsicException."""
    # Deep ITM call F=100, K=80; intrinsic = 20. Price = 1 << 20.
    contract = make_contract(strike=80.0, type_="C", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=1.0)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == "missing_iv_invert_failed"
    assert result.error_detail is not None
    assert len(result.error_detail) > 0
    assert result.missing_inputs == ("iv",)


def test_above_maximum_invert_failed(pricer: DefaultOptionsPricer) -> None:
    """A call price above F triggers py_vollib's AboveMaximumException."""
    # call price > F is impossible under r=0; py_vollib refuses.
    contract = make_contract(strike=100.0, type_="C", expiration=date(2024, 6, 21))
    row = make_row(row_date=date(2024, 3, 22), mid=200.0)
    result = pricer.invert_iv(contract, row, underlying_price=100.0)
    assert result.source == "missing"
    assert result.error_code == "missing_iv_invert_failed"
    assert result.error_detail is not None


# ---------------------------------------------------------------------------
# Blocked roots / gates also propagate through invert_iv
# ---------------------------------------------------------------------------


def test_invert_iv_opt_vix_blocked(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(collection="OPT_VIX")
    row = make_row()
    result = pricer.invert_iv(contract, row, underlying_price=20.0)
    assert result.source == "missing"
    assert result.error_code == "missing_forward_vix_curve"


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
