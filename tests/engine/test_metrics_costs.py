"""Functional tests for slippage/fees in the portfolio engine (metrics.py)."""

from __future__ import annotations

import numpy as np

from tcg.engine.costs import CostConfig
from tcg.engine.metrics import compute_metrics, compute_weighted_portfolio

# Two legs, weights 0.5/0.5. A rises then flat, B falls then flat.
_A = np.array([100.0, 110.0, 110.0])
_B = np.array([100.0, 90.0, 90.0])
_CLOSES = {"A": _A, "B": _B}
_WEIGHTS = {"A": 0.5, "B": 0.5}
_DATES = np.array([20200101, 20200102, 20200103], dtype=np.int64)


def _run(freq, cfg=None, roll_turnover=None):
    return compute_weighted_portfolio(
        _CLOSES,
        _WEIGHTS,
        freq,
        "normal",
        _DATES,
        cost_config=cfg,
        roll_turnover=roll_turnover,
    )


def test_zero_bps_byte_identical_all_modes():
    for freq in ("daily", "none", "monthly"):
        base = _run(freq)
        zero = _run(freq, CostConfig(0.0, 0.0))
        assert np.array_equal(base.portfolio_equity, zero.portfolio_equity), freq
        assert zero.total_slippage_paid_pct == 0.0
        assert zero.total_fees_paid_pct == 0.0
        # None cost_config path is the historical one.
        assert base.total_slippage_paid_pct == 0.0


def test_daily_positive_bps_reduces_equity():
    base = _run("daily")
    costed = _run("daily", CostConfig(slippage_bps=10.0, fees_bps=0.0))
    assert costed.portfolio_equity[-1] < base.portfolio_equity[-1]


def test_daily_worked_example():
    """slippage_bps=10, fees_bps=0, two legs 0.5/0.5 daily rebalance.

    retA=[nan,0.10,0.0], retB=[nan,-0.10,0.0] -> net_step gross = [0.0, 0.0]
    turnover = [1.0, 0.10]  (entry from cash; then drift-rebalance)
    slip_drag = [0.001, 0.0001] ; net_step_adj = [-0.001, -0.0001]
    equity = [100, 99.9, 99.89001]
    total_slippage_pct = 100*(0.001*1.0 + 0.0001*0.999) = 0.10999 %
    """
    res = _run("daily", CostConfig(slippage_bps=10.0, fees_bps=0.0))
    np.testing.assert_allclose(res.portfolio_equity, [100.0, 99.9, 99.89001], atol=1e-9)
    assert abs(res.total_slippage_paid_pct - 0.10999) < 1e-9
    assert res.total_fees_paid_pct == 0.0


def test_slippage_and_fees_isolate():
    slip_only = _run("daily", CostConfig(slippage_bps=10.0, fees_bps=0.0))
    fees_only = _run("daily", CostConfig(slippage_bps=0.0, fees_bps=10.0))
    both = _run("daily", CostConfig(slippage_bps=10.0, fees_bps=10.0))
    # Same rate on each side -> mirror totals.
    assert (
        abs(slip_only.total_slippage_paid_pct - fees_only.total_fees_paid_pct) < 1e-12
    )
    assert fees_only.total_slippage_paid_pct == 0.0
    assert slip_only.total_fees_paid_pct == 0.0
    # Both together = twice the drag; slightly less than 2x the single total
    # because the second bp of drag compounds on a slightly lower equity.
    assert both.total_slippage_paid_pct > 0.0
    assert both.total_fees_paid_pct > 0.0
    assert both.portfolio_equity[-1] < slip_only.portfolio_equity[-1]


def test_buy_and_hold_only_initial_entry_cost():
    """Buy-and-hold charges turnover ONLY at entry (Σ|w| = 1.0), no rebalance.

    slippage_bps=10 -> slip_drag[0] = 0.001, charged on step 0->1.
    total_slippage_pct = 100 * 0.001 * er[0]=1.0 = 0.1 %.
    """
    res = _run("none", CostConfig(slippage_bps=10.0, fees_bps=0.0))
    assert abs(res.total_slippage_paid_pct - 0.1) < 1e-9
    base = _run("none")
    assert res.portfolio_equity[-1] < base.portfolio_equity[-1]


def test_continuous_roll_incurs_round_trip():
    """A roll at bar 1 adds round-trip turnover -> more cost than no roll."""
    no_roll = _run("daily", CostConfig(slippage_bps=10.0, fees_bps=0.0))
    # Round-trip (2 sides) on leg A's 0.5 notional at bar 1: 2*0.5 = 1.0.
    roll_t = np.array([0.0, 1.0, 0.0])
    with_roll = _run(
        "daily", CostConfig(slippage_bps=10.0, fees_bps=0.0), roll_turnover=roll_t
    )
    assert with_roll.total_slippage_paid_pct > no_roll.total_slippage_paid_pct
    assert with_roll.portfolio_equity[-1] < no_roll.portfolio_equity[-1]


def test_compute_metrics_carries_cost_totals():
    res = _run("daily", CostConfig(slippage_bps=10.0, fees_bps=3.0))
    m = compute_metrics(
        res.portfolio_equity,
        return_type="normal",
        total_slippage_paid_pct=res.total_slippage_paid_pct,
        total_fees_paid_pct=res.total_fees_paid_pct,
    )
    assert m.total_slippage_paid_pct == res.total_slippage_paid_pct
    assert m.total_fees_paid_pct == res.total_fees_paid_pct
    # Default (no cost args) stays 0.0 -> byte-compatible.
    assert compute_metrics(res.portfolio_equity).total_slippage_paid_pct == 0.0
