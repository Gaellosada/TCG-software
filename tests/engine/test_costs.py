"""Unit tests for the shared slippage/fees cost math (:mod:`tcg.engine.costs`).

All expected numbers are HAND-COMPUTED in the docstrings/comments so the test is
an independent check of the arithmetic, not a snapshot of the implementation.
"""

from __future__ import annotations

import numpy as np

from tcg.engine.costs import (
    CostConfig,
    cumulative_cost_pct,
    establish_turnover,
    roll_turnover_from_flags,
    split_drag,
)


def test_roll_turnover_open_is_one_side_rolls_are_round_trip():
    """is_roll at bars [0, 2, 4], nav_times=0.5, T=6 (n_steps=5).

    bar 0 = initial open -> 1 side  -> 0.5
    bar 2 = roll         -> 2 sides -> 1.0
    bar 4 = roll         -> 2 sides -> 1.0
    """
    is_roll = np.array([True, False, True, False, True, False])
    t = roll_turnover_from_flags(is_roll, 0.5, 5)
    np.testing.assert_allclose(t, [0.5, 0.0, 1.0, 0.0, 1.0], atol=1e-15)


def test_roll_turnover_last_bar_dropped():
    """A roll on the very last bar is never held into a step -> dropped."""
    is_roll = np.array([True, False, False, True])  # T=4, n_steps=3, roll at bar 3
    t = roll_turnover_from_flags(is_roll, 1.0, 3)
    np.testing.assert_allclose(t, [1.0, 0.0, 0.0], atol=1e-15)


def test_config_bps_to_rate_and_is_zero():
    cfg = CostConfig(slippage_bps=10.0, fees_bps=2.5)
    assert cfg.slippage_rate == 10.0 / 10_000.0
    assert cfg.fees_rate == 2.5 / 10_000.0
    assert not cfg.is_zero()
    assert CostConfig().is_zero()
    assert CostConfig(slippage_bps=0.0, fees_bps=0.0).is_zero()
    assert not CostConfig(slippage_bps=0.0, fees_bps=1.0).is_zero()


def test_turnover_single_leg_daily_only_initial_entry():
    """Single leg at weight 1.0, daily rebalance -> only the entry costs.

    prices A = [100, 110, 99] -> retA = [nan, 0.10, -0.10]
    pos = 1.0 constant (K=1); net_step = [0.10, -0.10].
    turnover[0] = |1.0| = 1.0 (entry from cash)
    turnover[1]: drift = 1.0*(1+0.10)/(1+0.10) = 1.0 -> |1.0-1.0| = 0.0
    """
    pos = np.array([[1.0], [1.0], [1.0]])
    rets = np.array([[np.nan], [0.10], [-0.10]])
    net_step = np.array([0.10, -0.10])
    turnover = establish_turnover(pos, rets, net_step)
    assert turnover.shape == (2,)
    np.testing.assert_allclose(turnover, [1.0, 0.0], atol=1e-15)


def test_turnover_two_legs_drift_rebalance():
    """Two legs 0.5/0.5 daily rebalance -> nonzero drift turnover.

    A = [100,110,110] -> retA = [nan, 0.10, 0.0]
    B = [100, 90, 90] -> retB = [nan,-0.10, 0.0]
    net_step[0] = 0.5*0.10 + 0.5*(-0.10) = 0.0 ; net_step[1] = 0.0
    turnover[0] = 0.5 + 0.5 = 1.0
    turnover[1]: denom = 1+0.0 = 1.0
      driftA = 0.5*(1+0.10)/1.0 = 0.55 ; driftB = 0.5*(1-0.10)/1.0 = 0.45
      |0.5-0.55| + |0.5-0.45| = 0.05 + 0.05 = 0.10
    """
    pos = np.array([[0.5, 0.5], [0.5, 0.5], [0.5, 0.5]])
    rets = np.array([[np.nan, np.nan], [0.10, -0.10], [0.0, 0.0]])
    net_step = np.array([0.0, 0.0])
    turnover = establish_turnover(pos, rets, net_step)
    np.testing.assert_allclose(turnover, [1.0, 0.10], atol=1e-15)


def test_split_drag_independent_rates():
    turnover = np.array([1.0, 0.10])
    slip, fees = split_drag(turnover, CostConfig(slippage_bps=10.0, fees_bps=0.0))
    np.testing.assert_allclose(slip, [0.001, 0.0001], atol=1e-18)
    np.testing.assert_allclose(fees, [0.0, 0.0], atol=1e-18)

    slip2, fees2 = split_drag(turnover, CostConfig(slippage_bps=0.0, fees_bps=5.0))
    np.testing.assert_allclose(slip2, [0.0, 0.0], atol=1e-18)
    np.testing.assert_allclose(fees2, [0.0005, 0.00005], atol=1e-18)


def test_cumulative_cost_pct_worked_example():
    """slippage_bps=10 on turnover=[1.0, 0.10], daily rebalance (see two-leg test).

    slip_drag = [0.001, 0.0001]
    net_step_adj = [0-0.001, 0-0.0001] = [-0.001, -0.0001]
    equity_ratio (start 1.0): er[0]=1.0, er[1]=1.0*(1-0.001)=0.999
    total_slippage_pct = 100 * (0.001*1.0 + 0.0001*0.999)
                       = 100 * (0.001 + 0.00009990) = 0.1099900 %
    """
    slip_drag = np.array([0.001, 0.0001])
    er_start = np.array([1.0, 0.999])
    pct = cumulative_cost_pct(slip_drag, er_start)
    assert abs(pct - 0.10999) < 1e-9


def test_turnover_holds_nonfinite_leg_flat():
    """A NaN leg return is treated as a 0-return drift (held flat)."""
    pos = np.array([[0.5, 0.5], [0.5, 0.5], [0.5, 0.5]])
    rets = np.array([[np.nan, np.nan], [np.nan, 0.10], [0.0, 0.0]])
    net_step = np.array([0.05, 0.0])  # only leg B contributes 0.5*0.10
    turnover = establish_turnover(pos, rets, net_step)
    # leg A: drift = 0.5*(1+0)/(1+0.05) = 0.47619..., |0.5-0.47619|=0.02381
    # leg B: drift = 0.5*(1+0.10)/(1.05) = 0.52381, |0.5-0.52381|=0.02381
    np.testing.assert_allclose(turnover[0], 1.0, atol=1e-15)
    np.testing.assert_allclose(turnover[1], 0.0476190476190, atol=1e-9)


def test_turnover_short_leg_uses_absolute_change():
    """A short leg (negative target weight) contributes |Δweight| to turnover."""
    pos = np.array([[0.5, -0.5], [0.5, -0.5], [0.5, -0.5]])
    rets = np.array([[np.nan, np.nan], [0.10, 0.10], [0.0, 0.0]])
    # net_step[0] = 0.5*0.10 + (-0.5)*0.10 = 0.0
    net_step = np.array([0.0, 0.0])
    turnover = establish_turnover(pos, rets, net_step)
    assert turnover[0] == 1.0  # |0.5| + |-0.5|
