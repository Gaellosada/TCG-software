"""Hedge modularization — P0 (generalized sizing law + shared accrual helper).

DB-free.  Pins the P0 additions to :mod:`tcg.engine.hold_pnl`:

  * ``delta_hedge_qty`` now DIVIDES by the hedge instrument's own per-unit delta
    (``hedge_unit_delta``, default ``1.0``) with a degenerate-delta guard
    (``|δ_hedge| < 1e-6`` / non-finite ⇒ 0) and a symmetric quantity cap
    (``|qty| ≤ cap_mult·|option_qty|``, default ``cap_mult = 10``);
  * ``hedge_step_contrib`` is the SINGLE accrual primitive both notional bases use;
  * a ``hedge_unit_delta`` array of ALL ONES (or left ``None``) reproduces the
    pre-modularization output bit-for-bit (spot/future δ ≡ 1).
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.engine.hold_pnl import (
    _compound_with_hold,
    _HoldPnLSpec,
    delta_hedge_qty,
    hedge_step_contrib,
)


# ── delta_hedge_qty: the divide ─────────────────────────────────────────────
def test_qty_divides_by_hedge_unit_delta() -> None:
    # δ_hedge = 0.5 ⇒ twice as many hedge units as the δ≡1 case.
    q1 = delta_hedge_qty(1.0 / 3.0, option_qty=0.6, option_delta=0.4)
    q_half = delta_hedge_qty(1.0 / 3.0, option_qty=0.6, option_delta=0.4, hedge_unit_delta=0.5)
    assert q_half == pytest.approx(q1 / 0.5)
    assert q_half == pytest.approx(-(1.0 / 3.0) * 0.6 * 0.4 / 0.5)


def test_qty_default_hedge_unit_delta_is_one_exact() -> None:
    # Divide by 1.0 is exact ⇒ 3-arg call is bit-identical to explicit δ=1.
    for f, q, d in [(1.0 / 3.0, 0.5, 0.4), (0.7, -1.3, 0.9), (2.0, 3.0, -0.2)]:
        assert delta_hedge_qty(f, q, d) == delta_hedge_qty(f, q, d, 1.0)


# ── delta_hedge_qty: the degenerate-delta guard ─────────────────────────────
@pytest.mark.parametrize("bad", [0.0, 1e-7, -1e-9, np.nan, np.inf, -np.inf])
def test_qty_degenerate_hedge_unit_delta_books_zero(bad) -> None:
    assert delta_hedge_qty(1.0 / 3.0, option_qty=5.0, option_delta=0.9, hedge_unit_delta=bad) == 0.0


def test_qty_just_above_threshold_does_not_book_zero() -> None:
    # 1e-6 is the boundary; a value at/above it must NOT be treated as degenerate.
    q = delta_hedge_qty(1.0, option_qty=1e-3, option_delta=1e-3, hedge_unit_delta=1e-6, cap_mult=1e12)
    assert q != 0.0


# ── delta_hedge_qty: the quantity cap ───────────────────────────────────────
def test_qty_cap_binds_symmetric() -> None:
    # tiny δ_hedge blows the raw qty up; the cap clips to cap_mult·|option_qty|.
    q = delta_hedge_qty(1.0, option_qty=2.0, option_delta=1.0, hedge_unit_delta=1e-4, cap_mult=10.0)
    assert q == pytest.approx(-10.0 * 2.0)  # negative net delta side, clipped
    # opposite sign (short option delta) clips to the positive bound.
    q2 = delta_hedge_qty(1.0, option_qty=2.0, option_delta=-1.0, hedge_unit_delta=1e-4, cap_mult=10.0)
    assert q2 == pytest.approx(+10.0 * 2.0)


def test_qty_cap_inert_for_vx1_regime() -> None:
    # |δ_opt|≤1, factor=1/3, δ_hedge=1 ⇒ |q| ≤ 0.33·|option_qty| ⇒ cap never binds.
    for q in (-5.0, -1.0, 0.3, 4.0):
        for d in (-1.0, -0.4, 0.7, 1.0):
            expect = -(1.0 / 3.0) * q * d
            assert delta_hedge_qty(1.0 / 3.0, q, d) == pytest.approx(expect, abs=1e-15)


# ── hedge_step_contrib primitive ────────────────────────────────────────────
def test_step_contrib_matches_qty_times_dprice() -> None:
    c = hedge_step_contrib(
        factor=1.0 / 3.0,
        option_qty_cur=0.5,
        delta_opt_s=0.4,
        hedge_unit_delta_s=1.0,
        d_hedge_price=2.0,
    )
    assert c == pytest.approx(delta_hedge_qty(1.0 / 3.0, 0.5, 0.4, 1.0) * 2.0)


@pytest.mark.parametrize("bad_delta,bad_dprice", [(np.nan, 1.0), (1.0, np.nan), (np.inf, 1.0)])
def test_step_contrib_nonfinite_books_zero(bad_delta, bad_dprice) -> None:
    assert hedge_step_contrib(
        factor=1.0 / 3.0,
        option_qty_cur=0.5,
        delta_opt_s=bad_delta,
        hedge_unit_delta_s=1.0,
        d_hedge_price=bad_dprice,
    ) == 0.0


# ── hedge_unit_delta ≡ 1 reproduces the δ-implicit (None) path bit-for-bit ───
def _hedged_spec(hedge_unit_delta, T=6):
    P0 = 10.0
    premium = np.full(T, P0, dtype=np.float64)
    is_roll = np.zeros(T, dtype=np.bool_)
    is_roll[0] = True
    roll_premium = np.full(T, np.nan, dtype=np.float64)
    roll_premium[0] = P0
    return _HoldPnLSpec(
        ref_id="_leg",
        sign=1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(T, dtype=np.bool_),
        hedge_factor=1.0 / 3.0,
        hedge_delta=np.array([0.5, 0.6, 0.4, 0.7, 0.55, 0.5]),
        hedge_price=np.array([20.0, 21.0, 20.5, 22.0, 21.0, 20.0]),
        hedge_active=np.ones(T, dtype=np.bool_),
        hedge_unit_delta=hedge_unit_delta,
    )


def test_all_ones_hedge_unit_delta_byte_identical_to_none() -> None:
    T = 6
    r_none, s_none, c_none = _compound_with_hold(np.zeros(T - 1), [_hedged_spec(None, T)])
    r_ones, s_ones, c_ones = _compound_with_hold(
        np.zeros(T - 1), [_hedged_spec(np.ones(T, dtype=np.float64), T)]
    )
    np.testing.assert_array_equal(r_none, r_ones)
    np.testing.assert_array_equal(s_none, s_ones)
    np.testing.assert_array_equal(c_none["_leg"], c_ones["_leg"])


@given(
    delta=st.lists(st.floats(-1.0, 1.0), min_size=2, max_size=10),
    vx_step=st.lists(st.floats(-4.0, 4.0), min_size=2, max_size=10),
)
@settings(max_examples=40, deadline=None)
def test_prop_all_ones_equals_none(delta, vx_step) -> None:
    T = min(len(delta), len(vx_step))
    d = np.asarray(delta[:T], dtype=np.float64)
    vx = np.cumsum(np.asarray([20.0] + vx_step[: T - 1], dtype=np.float64))
    P0 = 10.0
    premium = np.full(T, P0, dtype=np.float64)
    is_roll = np.zeros(T, dtype=np.bool_)
    is_roll[0] = True
    roll_premium = np.full(T, np.nan, dtype=np.float64)
    roll_premium[0] = P0

    def mk(hud):
        return _HoldPnLSpec(
            ref_id="_leg",
            sign=1.0,
            nav_times=1.0,
            premium=premium,
            is_roll=is_roll,
            roll_premium=roll_premium,
            pos_active=np.ones(T, dtype=np.bool_),
            hedge_factor=1.0 / 3.0,
            hedge_delta=d,
            hedge_price=vx,
            hedge_active=np.ones(T, dtype=np.bool_),
            hedge_unit_delta=hud,
        )

    r0, s0, c0 = _compound_with_hold(np.zeros(max(T - 1, 0)), [mk(None)])
    r1, s1, c1 = _compound_with_hold(np.zeros(max(T - 1, 0)), [mk(np.ones(T))])
    np.testing.assert_array_equal(r0, r1)
    np.testing.assert_array_equal(c0["_leg"], c1["_leg"])
