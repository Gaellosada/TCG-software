"""Delta-based futures sizing — the ⅓-delta VX1 hedge (feature F2, SPEC §5.5/§5.6).

DB-free.  Exercises the delta-hedge overlay of ``_compound_with_hold`` /
``_HoldPnLSpec`` and the pure sizing law ``delta_hedge_qty`` against a
hand-computed oracle, plus Hypothesis invariants:

  * ``qty_hedge = -factor·option_qty·option_delta`` exactly (sign opposite the
    net option delta; |qty| = factor·|option_qty·delta|);
  * daily re-sizing tracks a changing delta;
  * hedge $ P&L = ``qty_hedge·ΔVX1`` to 1e-12;
  * gate off ⇒ ZERO hedge;
  * an option leg WITHOUT a hedge is BYTE-IDENTICAL to the pre-F2 engine
    (``git show HEAD:tcg/engine/hold_pnl.py``).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.engine.hold_pnl import (
    _compound_with_hold,
    _HoldPnLSpec,
    delta_hedge_qty,
)


def _run(spec: _HoldPnLSpec, T: int):
    return _compound_with_hold(np.zeros(max(T - 1, 0), dtype=np.float64), [spec])


def _base_spec(**kw) -> _HoldPnLSpec:
    """A single-segment hold-mode option spec with a CONSTANT premium (so the
    option's own P&L is 0 and ``ratio`` moves ONLY from the hedge), plus the
    hedge overlay fields from ``kw``."""
    T = kw.pop("T")
    P0 = kw.pop("P0", 10.0)
    premium = np.full(T, P0, dtype=np.float64)
    is_roll = np.zeros(T, dtype=np.bool_)
    is_roll[0] = True
    roll_premium = np.full(T, np.nan, dtype=np.float64)
    roll_premium[0] = P0
    return _HoldPnLSpec(
        ref_id="_leg",
        sign=kw.pop("sign", 1.0),
        nav_times=kw.pop("nav_times", 1.0),
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(T, dtype=np.bool_),
        **kw,
    )


# ── The pure sizing law ─────────────────────────────────────────────────────
def test_delta_hedge_qty_sign_opposite_and_magnitude() -> None:
    # Long call (option_qty>0, delta>0) → SHORT future (qty_hedge<0).
    q = delta_hedge_qty(1.0 / 3.0, option_qty=0.5, option_delta=0.4)
    assert q == pytest.approx(-(1.0 / 3.0) * 0.5 * 0.4)
    assert q < 0.0  # opposite the positive net delta
    # Magnitude = factor·|option_qty·delta|.
    assert abs(q) == pytest.approx((1.0 / 3.0) * abs(0.5 * 0.4))
    # Negative net delta (long put, delta<0) → LONG future (qty_hedge>0).
    q2 = delta_hedge_qty(1.0 / 3.0, option_qty=0.5, option_delta=-0.4)
    assert q2 > 0.0


# ── Exact hedge-P&L accrual through the compounding recurrence ───────────────
def test_hedge_pnl_accrual_exact() -> None:
    """hold_contrib[s] == -factor·sign·nav_times·(seg_er/ratio[s])·delta[s]·ΔVX1[s]/P0.

    Single segment ⇒ seg_er == 1.  Compared to the actual returned ``ratio`` so
    the 1/ratio[s] equity-coupling is verified to machine epsilon."""
    T = 6
    factor = 1.0 / 3.0
    P0 = 10.0
    sign = 1.0
    nav_times = 1.0
    delta = np.array([0.5, 0.6, 0.4, 0.7, 0.55, 0.5])
    vx1 = np.array([20.0, 21.0, 20.5, 22.0, 21.0, 20.0])
    spec = _base_spec(
        T=T,
        P0=P0,
        sign=sign,
        nav_times=nav_times,
        hedge_factor=factor,
        hedge_delta=delta,
        hedge_price=vx1,
        hedge_active=np.ones(T, dtype=np.bool_),
    )
    ratio, _scale, contrib = _run(spec, T)
    booked = contrib["_leg"]
    for s in range(T - 1):
        dvx = vx1[s + 1] - vx1[s]
        expected = -factor * sign * nav_times * (1.0 / ratio[s]) * delta[s] * dvx / P0
        assert booked[s] == pytest.approx(expected, abs=1e-12), f"step {s}"
    # Reconciliation: equity_ratio-1 == cumulative booked contribs (ratio[0]==1,
    # step_scale==1 while solvent) — the hedge really lands in the leg equity.
    recon = np.cumsum(_scale * ratio[:-1] * booked)
    np.testing.assert_allclose(ratio[1:] - 1.0, recon, atol=1e-12)


def test_gate_off_zero_hedge() -> None:
    """hedge_active all-False ⇒ the hedge books nothing ⇒ constant-premium leg
    stays flat at ratio==1 (no option P&L, no hedge P&L)."""
    T = 5
    spec = _base_spec(
        T=T,
        hedge_factor=1.0 / 3.0,
        hedge_delta=np.full(T, 0.5),
        hedge_price=np.array([20.0, 25.0, 18.0, 30.0, 12.0]),
        hedge_active=np.zeros(T, dtype=np.bool_),
    )
    ratio, _scale, contrib = _run(spec, T)
    np.testing.assert_array_equal(contrib["_leg"], np.zeros(T - 1))
    np.testing.assert_allclose(ratio, np.ones(T), atol=1e-15)


def test_gate_partial_days() -> None:
    """The hedge accrues ONLY on gate-on days."""
    T = 4
    delta = np.full(T, 0.5)
    vx1 = np.array([20.0, 21.0, 22.0, 23.0])  # +1 each step
    active = np.array([True, False, True, True], dtype=np.bool_)
    spec = _base_spec(
        T=T,
        hedge_factor=1.0 / 3.0,
        hedge_delta=delta,
        hedge_price=vx1,
        hedge_active=active,
    )
    _ratio, _scale, contrib = _run(spec, T)
    booked = contrib["_leg"]
    # step 1 gated off → exactly 0; steps 0,2 gated on → non-zero.
    assert booked[1] == 0.0
    assert booked[0] != 0.0
    assert booked[2] != 0.0


def test_daily_resize_tracks_changing_delta() -> None:
    """With a CONSTANT ΔVX1 per step, the booked hedge contrib is proportional to
    that day's delta (÷ratio[s]) — daily rebalance really re-sizes off delta[s]."""
    T = 4
    factor = 1.0 / 3.0
    P0 = 10.0
    delta = np.array([0.2, 0.8, 0.5, 0.5])
    vx1 = np.array([20.0, 21.0, 22.0, 23.0])  # ΔVX1 == +1 every step
    spec = _base_spec(
        T=T,
        P0=P0,
        hedge_factor=factor,
        hedge_delta=delta,
        hedge_price=vx1,
        hedge_active=np.ones(T, dtype=np.bool_),
    )
    ratio, _scale, contrib = _run(spec, T)
    booked = contrib["_leg"]
    # booked[s] = -factor·(1/ratio[s])·delta[s]·1/P0 ; ratio[0]==1 so booked[0]
    # exactly -factor·delta[0]/P0.
    assert booked[0] == pytest.approx(-factor * delta[0] / P0, abs=1e-12)
    # Larger delta on step 1 ⇒ larger |contrib| than step 0 (ratio≈1).
    assert abs(booked[1]) > abs(booked[0])


def test_no_hedge_leaves_ratio_flat() -> None:
    """hedge_factor is None ⇒ overlay OFF ⇒ constant-premium leg is exactly flat."""
    T = 5
    spec = _base_spec(
        T=T,
        hedge_delta=np.full(T, 0.5),
        hedge_price=np.array([20.0, 25.0, 18.0, 30.0, 12.0]),
    )  # hedge_factor omitted → None
    ratio, _scale, contrib = _run(spec, T)
    np.testing.assert_array_equal(contrib["_leg"], np.zeros(T - 1))
    np.testing.assert_allclose(ratio, np.ones(T), atol=1e-15)


def test_futures_notional_leg_ignores_hedge() -> None:
    """A hedge configured on a futures_notional leg is IGNORED by the engine
    (the caller rejects the combination) — so it must NOT perturb the P&L."""
    T = 4
    premium = np.array([10.0, 11.0, 12.0, 13.0])
    is_roll = np.array([True, False, False, False])
    roll_premium = np.array([10.0, np.nan, np.nan, np.nan])
    roll_fref = np.array([20.0, np.nan, np.nan, np.nan])
    common = dict(
        ref_id="_leg",
        sign=1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(T, dtype=np.bool_),
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=1000.0,
        mult_opt=100.0,
    )
    ratio_no = _run(_HoldPnLSpec(**common), T)[0]
    ratio_hedge = _run(
        _HoldPnLSpec(
            **common,
            hedge_factor=1.0 / 3.0,
            hedge_delta=np.full(T, 0.5),
            hedge_price=np.array([20.0, 30.0, 10.0, 40.0]),
            hedge_active=np.ones(T, dtype=np.bool_),
        ),
        T,
    )[0]
    np.testing.assert_array_equal(ratio_no, ratio_hedge)


# ── Property tests (Hypothesis) ─────────────────────────────────────────────
@given(
    factor=st.floats(0.01, 3.0),
    option_qty=st.floats(-5.0, 5.0),
    delta=st.floats(-1.0, 1.0),
)
def test_prop_qty_sign_and_magnitude(factor, option_qty, delta) -> None:
    q = delta_hedge_qty(factor, option_qty, delta)
    net = option_qty * delta
    # sign always opposite the net option delta (or 0 when net==0).
    if net > 0:
        assert q <= 0.0
    elif net < 0:
        assert q >= 0.0
    assert abs(q) == pytest.approx(factor * abs(net), abs=1e-12)


@given(
    delta=st.lists(st.floats(-1.0, 1.0), min_size=2, max_size=12),
    vx_step=st.lists(st.floats(-5.0, 5.0), min_size=2, max_size=12),
    factor=st.floats(0.05, 2.0),
)
@settings(max_examples=60)
def test_prop_gate_never_true_zero_contrib(delta, vx_step, factor) -> None:
    T = min(len(delta), len(vx_step))
    delta = np.asarray(delta[:T])
    vx1 = np.cumsum(np.asarray([20.0] + vx_step[: T - 1]))
    spec = _base_spec(
        T=T,
        hedge_factor=factor,
        hedge_delta=delta,
        hedge_price=vx1,
        hedge_active=np.zeros(T, dtype=np.bool_),  # never active
    )
    _ratio, _scale, contrib = _run(spec, T)
    np.testing.assert_array_equal(contrib["_leg"], np.zeros(max(T - 1, 0)))


# ── Byte-identical to the pre-F2 engine (no hedge configured) ───────────────
def _load_head_module():
    """Import ``tcg/engine/hold_pnl.py`` AT GIT HEAD as a standalone module."""
    src = subprocess.run(
        ["git", "show", "HEAD:tcg/engine/hold_pnl.py"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "hold_pnl_head.py"
    tmp.write_text(src)
    spec = importlib.util.spec_from_file_location("hold_pnl_head", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hold_pnl_head"] = mod
    spec.loader.exec_module(mod)
    return mod


@given(
    prem=st.lists(st.floats(1.0, 50.0), min_size=2, max_size=10),
    sign=st.sampled_from([-1.0, 1.0]),
    nav_times=st.floats(0.1, 3.0),
)
@settings(max_examples=40, deadline=None)
def test_prop_no_hedge_byte_identical_to_head(prem, sign, nav_times) -> None:
    """A spec with NO hedge (hedge_factor=None) must reproduce the HEAD engine
    output bit-for-bit — the new path is fully guarded."""
    head = _load_head_module()
    T = len(prem)
    premium = np.asarray(prem, dtype=np.float64)
    is_roll = np.zeros(T, dtype=np.bool_)
    is_roll[0] = True
    roll_premium = np.full(T, np.nan, dtype=np.float64)
    roll_premium[0] = premium[0]
    pos_active = np.ones(T, dtype=np.bool_)

    def mk(cls):
        return cls(
            ref_id="_leg",
            sign=sign,
            nav_times=nav_times,
            premium=premium,
            is_roll=is_roll,
            roll_premium=roll_premium,
            pos_active=pos_active,
        )

    r_new, s_new, c_new = _compound_with_hold(
        np.zeros(max(T - 1, 0)), [mk(_HoldPnLSpec)]
    )
    r_old, s_old, c_old = head._compound_with_hold(
        np.zeros(max(T - 1, 0)), [mk(head._HoldPnLSpec)]
    )
    np.testing.assert_array_equal(r_new, r_old)
    np.testing.assert_array_equal(s_new, s_old)
    np.testing.assert_array_equal(c_new["_leg"], c_old["_leg"])
