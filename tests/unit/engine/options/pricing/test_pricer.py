"""Pricer gating + envelope tests."""

from __future__ import annotations

from datetime import date

import pytest

from tcg.engine.options.pricing.pricer import DefaultOptionsPricer
from tcg.types.options import ComputedGreeks, GreekKind

from ._fixtures import make_contract, make_row


@pytest.fixture
def pricer() -> DefaultOptionsPricer:
    return DefaultOptionsPricer()


def _all_5(g: ComputedGreeks) -> tuple:
    return (g.iv, g.delta, g.gamma, g.theta, g.vega)


# ---------------------------------------------------------------------------
# Blocked roots
# ---------------------------------------------------------------------------


def test_opt_vix_blocked_all_five_missing(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(collection="OPT_VIX", root_underlying="IND_VIX")
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=20.0)
    for r in _all_5(g):
        assert r.value is None
        assert r.source == "missing"
        assert r.error_code == "missing_forward_vix_curve"
        assert r.missing_inputs == ("forward_vix_curve",)


def test_opt_eth_blocked_all_five_missing(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(collection="OPT_ETH", root_underlying="ETHUSD")
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=2000.0)
    for r in _all_5(g):
        assert r.value is None
        assert r.source == "missing"
        assert r.error_code == "missing_deribit_feed"
        assert r.missing_inputs == ("underlying_price",)


def test_blocked_root_does_not_call_kernel() -> None:
    """If a blocked root were to leak through, the (deliberately broken) kernel
    would raise. This proves Module 2 short-circuits before any pricing."""

    class ExplodingKernel:
        def price_call(self, *a, **kw):  # noqa: D401, ANN001, ANN003
            raise AssertionError("kernel should not be invoked for blocked roots")

        price_put = delta = gamma = theta = vega = implied_vol = price_call  # type: ignore[assignment]

    pricer = DefaultOptionsPricer(kernel=ExplodingKernel())  # type: ignore[arg-type]
    contract = make_contract(collection="OPT_VIX")
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=20.0)
    assert g.iv.error_code == "missing_forward_vix_curve"


# ---------------------------------------------------------------------------
# Strike-factor gate
# ---------------------------------------------------------------------------


def test_t_note_unverified_strike_factor(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(
        collection="OPT_T_NOTE_10_Y",
        root_underlying="FUT_T_NOTE_10_Y",
        strike_factor_verified=False,
    )
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=110.0)
    for r in _all_5(g):
        assert r.error_code == "strike_factor_unverified"
        assert r.missing_inputs == ("strike_factor",)


def test_t_note_verified_strike_factor_runs(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(
        collection="OPT_T_NOTE_10_Y",
        root_underlying="FUT_T_NOTE_10_Y",
        strike_factor_verified=True,
    )
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=110.0)
    # IV inversion will compute (mid is set in fixture).
    assert g.iv.source in ("computed", "missing")  # IV may fail to invert at K=100/F=110
    # Whatever happens, no strike-factor block.
    for r in _all_5(g):
        assert r.error_code != "strike_factor_unverified"


def test_sp_500_does_not_need_strike_factor_verification(
    pricer: DefaultOptionsPricer,
) -> None:
    # SP_500 is NOT in the gated set; even if a malformed contract had
    # strike_factor_verified=False, the gate must not fire.
    contract = make_contract(strike_factor_verified=False)
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=100.0)
    for r in _all_5(g):
        assert r.error_code != "strike_factor_unverified"


# ---------------------------------------------------------------------------
# Underlying / TTM gates
# ---------------------------------------------------------------------------


def test_missing_underlying_price(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract()
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=None)
    for r in _all_5(g):
        assert r.error_code == "missing_underlying_price"
        assert r.missing_inputs == ("underlying_price",)


def test_expired_contract_ttm_zero(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(expiration=date(2024, 3, 22))
    row = make_row(row_date=date(2024, 3, 22))  # same day → TTM=0
    g = pricer.compute(contract, row, underlying_price=100.0)
    for r in _all_5(g):
        assert r.error_code == "expired_contract"
        assert r.missing_inputs == ("time_to_expiry",)


def test_expired_contract_negative_ttm(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(expiration=date(2024, 3, 1))
    row = make_row(row_date=date(2024, 3, 22))
    g = pricer.compute(contract, row, underlying_price=100.0)
    for r in _all_5(g):
        assert r.error_code == "expired_contract"


# ---------------------------------------------------------------------------
# Successful compute on OPT_SP_500
# ---------------------------------------------------------------------------


def test_successful_compute_sp_500_inputs_used(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract(
        collection="OPT_SP_500",
        strike=100.0,
        expiration=date(2024, 6, 21),
    )
    row = make_row(row_date=date(2024, 3, 22), mid=3.99)
    g = pricer.compute(contract, row, underlying_price=100.0)

    for r in _all_5(g):
        assert r.source == "computed", f"got {r}"
        assert r.value is not None
        assert r.model == "Black-76"
        assert r.error_code is None
        # inputs_used must contain all six canonical keys.
        assert r.inputs_used is not None
        assert set(r.inputs_used.keys()) == {
            "underlying_price",
            "iv",
            "ttm",
            "r",
            "sign",
            "kernel",
        }
        assert r.inputs_used["r"] == 0.0  # guardrail #5
        assert r.inputs_used["sign"] == "c"
        assert r.inputs_used["kernel"] == "BS76Kernel"
        assert r.inputs_used["underlying_price"] == 100.0


def test_inputs_used_key_order_stable(pricer: DefaultOptionsPricer) -> None:
    """Stable insertion order — Python 3.7+ guarantees dict insertion order."""
    contract = make_contract()
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=100.0)
    assert g.delta.inputs_used is not None
    keys = list(g.delta.inputs_used.keys())
    assert keys == ["underlying_price", "iv", "ttm", "r", "sign", "kernel"]


def test_signs_call_vs_put(pricer: DefaultOptionsPricer) -> None:
    contract_c = make_contract(type_="C")
    contract_p = make_contract(type_="P")
    row = make_row()
    g_c = pricer.compute(contract_c, row, underlying_price=100.0)
    g_p = pricer.compute(contract_p, row, underlying_price=100.0)
    assert g_c.delta.inputs_used["sign"] == "c"  # type: ignore[index]
    assert g_p.delta.inputs_used["sign"] == "p"  # type: ignore[index]


# ---------------------------------------------------------------------------
# `which` filter
# ---------------------------------------------------------------------------


def test_which_filter_only_delta(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract()
    row = make_row()
    g = pricer.compute(
        contract, row, underlying_price=100.0, which=(GreekKind.DELTA,)
    )
    assert g.delta.source == "computed"
    assert g.delta.value is not None

    for unwanted in (g.iv, g.gamma, g.theta, g.vega):
        assert unwanted.source == "missing"
        assert unwanted.error_code == "not_requested"
        assert unwanted.missing_inputs == ()
        assert unwanted.value is None


def test_which_filter_only_iv(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract()
    row = make_row()
    g = pricer.compute(contract, row, underlying_price=100.0, which=(GreekKind.IV,))
    assert g.iv.source == "computed"
    for unwanted in (g.delta, g.gamma, g.theta, g.vega):
        assert unwanted.error_code == "not_requested"


# ---------------------------------------------------------------------------
# IV propagation to Greeks when IV inversion fails
# ---------------------------------------------------------------------------


def test_iv_failure_propagates_to_greeks(pricer: DefaultOptionsPricer) -> None:
    contract = make_contract()
    row = make_row(mid=None, bid=None, ask=None)  # cannot invert IV
    g = pricer.compute(contract, row, underlying_price=100.0)
    assert g.iv.error_code == "missing_iv_no_quote_to_invert"
    # The other 4 must surface the same missing reason (not silently zero).
    for r in (g.delta, g.gamma, g.theta, g.vega):
        assert r.value is None
        assert r.source == "missing"
        assert r.error_code == "missing_iv_no_quote_to_invert"


# ---------------------------------------------------------------------------
# source ∈ {"computed","missing"} only (never "stored") — guardrail
# ---------------------------------------------------------------------------


def test_module_2_never_emits_source_stored(pricer: DefaultOptionsPricer) -> None:
    # Probe each gating path; none must produce source="stored".
    cases = [
        (make_contract(collection="OPT_VIX"), make_row(), 20.0),
        (make_contract(collection="OPT_ETH"), make_row(), 2000.0),
        (
            make_contract(collection="OPT_T_NOTE_10_Y", strike_factor_verified=False),
            make_row(),
            110.0,
        ),
        (make_contract(), make_row(), None),
        (make_contract(expiration=date(2020, 1, 1)), make_row(row_date=date(2024, 3, 22)), 100.0),
        (make_contract(), make_row(), 100.0),  # full success
    ]
    for contract, row, up in cases:
        g = pricer.compute(contract, row, underlying_price=up)
        for r in _all_5(g):
            assert r.source != "stored", (
                f"Module 2 must not emit source='stored' (got {r}); "
                f"that widening is Module 6's job."
            )
