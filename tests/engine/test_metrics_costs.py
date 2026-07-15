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


def test_short_leg_wipeout_stays_finite_none_and_monthly():
    """FIX 1 -- gross equity that touches/crosses zero must not poison the
    costs-ON tail with NaN/inf.

    A single short leg whose underlying more than doubles drives gross equity
    to 0 and beyond. The costs path derives per-bar returns from the gross
    curve, and ``_derive_returns_from_equity`` yields 0/0 -> NaN (zero-touch)
    or x/0 -> +-inf (zero-cross) there; that then recompounds into a
    NaN/inf equity tail and, via a single 0*inf in ``cumulative_cost_pct``,
    nulls BOTH cost totals. The equity curve must stay entirely finite and
    both cost totals must be finite and >= 0.
    """
    cfg = CostConfig(slippage_bps=10.0, fees_bps=5.0)

    # rebalance='none' (buy-and-hold): prices [100,200,210,220], short -1.0
    # -> GROSS [100,0,-10,-20]; derived returns [nan,-1,-inf,1] pre-fix.
    bh = compute_weighted_portfolio(
        {"S": np.array([100.0, 200.0, 210.0, 220.0])},
        {"S": -1.0},
        "none",
        "normal",
        np.array([20200101, 20200102, 20200103, 20200106], dtype=np.int64),
        cost_config=cfg,
    )
    assert np.all(np.isfinite(bh.portfolio_equity)), bh.portfolio_equity
    assert np.isfinite(bh.total_slippage_paid_pct) and bh.total_slippage_paid_pct >= 0.0
    assert np.isfinite(bh.total_fees_paid_pct) and bh.total_fees_paid_pct >= 0.0

    # rebalance='monthly': price doubles at the (index-1) month boundary, short
    # -1.0 -> GROSS [100,0,0,0]; derived [nan,-1,nan,nan] pre-fix -> 0*nan totals.
    mo = compute_weighted_portfolio(
        {"S": np.array([100.0, 200.0, 200.0, 200.0])},
        {"S": -1.0},
        "monthly",
        "normal",
        np.array([20200131, 20200203, 20200204, 20200205], dtype=np.int64),
        cost_config=cfg,
    )
    assert np.all(np.isfinite(mo.portfolio_equity)), mo.portfolio_equity
    assert np.isfinite(mo.total_slippage_paid_pct) and mo.total_slippage_paid_pct >= 0.0
    assert np.isfinite(mo.total_fees_paid_pct) and mo.total_fees_paid_pct >= 0.0


def test_periodic_drift_turnover_boundary_worked_example():
    """FIX 2 -- hand-computed monthly drift-turnover ACROSS a boundary.

    Two legs A/B, weights 0.5/0.5, slippage_bps=10, fees_bps=0, normal.
    dates cross a month boundary at index 2 (Jan 30, Jan 31 | Feb 3, Feb 4),
    and index 3 exists so the boundary trade IS held into a step (charged).

    A=[100,110,110,110], B=[100,100,100,100]:
      i=1 (Jan, no boundary): A 50->55, B 50 ; portfolio 105.
      i=2 (Feb boundary): drifted weights 55/105, 50/105; target 0.5/0.5
          -> drift-turnover = |0.5-55/105|+|0.5-50/105| = 5/105 = 0.047619048.
          redistribute to 52.5/52.5; returns 0 -> portfolio stays 105.
      i=3: returns 0 -> 105.

    turnover_step (charged) = [1.0 (entry), 0 (i=1), 5/105 (boundary i=2)].
    slip_drag = 0.001 * that. gross returns [nan,0.05,0,0].
    adj[1:] = [0.05-0.001, 0-0, 0-0.001*5/105].
    """
    cfg = CostConfig(slippage_bps=10.0, fees_bps=0.0)
    res = compute_weighted_portfolio(
        {
            "A": np.array([100.0, 110.0, 110.0, 110.0]),
            "B": np.array([100.0, 100.0, 100.0, 100.0]),
        },
        {"A": 0.5, "B": 0.5},
        "monthly",
        "normal",
        np.array([20200130, 20200131, 20200203, 20200204], dtype=np.int64),
        cost_config=cfg,
    )
    boundary_turn = 5.0 / 105.0
    exp_equity = np.array(
        [
            100.0,
            104.9,
            104.9,
            104.9 * (1.0 - 0.001 * boundary_turn),
        ]
    )
    np.testing.assert_allclose(res.portfolio_equity, exp_equity, atol=1e-9)
    exp_slippage = 100.0 * (0.001 * 1.0 + 0.0 + 0.001 * boundary_turn * 1.049)
    assert abs(res.total_slippage_paid_pct - exp_slippage) < 1e-9
    assert res.total_fees_paid_pct == 0.0


def test_periodic_short_leg_boundary_charges_drift_turnover():
    """FIX 2 -- a periodic short (negative-weight) leg still charges the
    boundary drift-turnover and stays finite; crossing a boundary costs more
    than an identical same-month (no-boundary) run."""
    closes = {
        "L": np.array([100.0, 110.0, 110.0, 110.0]),
        "S": np.array([100.0, 100.0, 100.0, 100.0]),
    }
    weights = {"L": 0.5, "S": -0.5}
    cfg = CostConfig(slippage_bps=10.0, fees_bps=0.0)
    crossing = compute_weighted_portfolio(
        closes,
        weights,
        "monthly",
        "normal",
        np.array([20200130, 20200131, 20200203, 20200204], dtype=np.int64),
        cost_config=cfg,
    )
    same_month = compute_weighted_portfolio(
        closes,
        weights,
        "monthly",
        "normal",
        np.array([20200101, 20200102, 20200103, 20200106], dtype=np.int64),
        cost_config=cfg,
    )
    assert np.all(np.isfinite(crossing.portfolio_equity))
    assert crossing.total_slippage_paid_pct >= 0.0
    # The boundary rebalance adds turnover the same-month run never incurs.
    assert crossing.total_slippage_paid_pct > same_month.total_slippage_paid_pct


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
