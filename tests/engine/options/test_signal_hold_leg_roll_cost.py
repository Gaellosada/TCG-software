"""Integration test: slippage/fees on a SIGNAL option HOLD-leg that rolls in-window.

The reviewer flagged that no test exercised the signal hold-leg roll cost
end-to-end — only the ``roll_turnover_from_flags`` primitive and an
injected-array engine test existed.  This drives the FULL pipeline
(``resolve_option_stream`` behind a ``PriceFetcher`` with ``fetch_hold_roll_info``
→ ``evaluate_signal``) with a ``cost_config``, so the assembly at
``signal_exec`` §6b — hold-leg rolls → ``roll_turnover_from_flags`` →
``vectorized_net_step`` drag → reduced ``equity_ratio`` — is actually run.

Reuses the oracle-exact APR→MAY hold fixture from
``test_stream_hold_signal_e2e`` (a NearestToTarget(35) short put that rolls on
2024-04-01 while APR still quotes).  ``_IS_ROLL = [1,0,0,1,0,0]``: the index-0
initial open (1 side) + the index-3 interior roll (round-trip, 2 sides).
"""

from __future__ import annotations

from datetime import date

import numpy as np

from tcg.engine.costs import CostConfig
from tcg.engine.signal_exec import evaluate_signal

from test_stream_hold_signal_e2e import (
    _build_chains,
    _make_fetcher,
    _short_put_signal,
    _DATES,
)


def _spx():
    return np.full(len(_DATES), 100.0, dtype=np.float64)  # always > 0 -> latched


def _no_interior_roll_fetcher(chains, *, spx_series):
    """Same fetcher, but its ``fetch_hold_roll_info`` zeroes the INTERIOR roll
    flag (keeps only the index-0 initial open).  Used to isolate the cost the
    in-window roll adds on top of the entry open."""
    fetcher = _make_fetcher(chains, spx_series=spx_series)
    real_roll_info = fetcher.fetch_hold_roll_info

    async def patched(instrument):
        dates, is_roll, roll_premium = await real_roll_info(instrument)
        is_roll = np.asarray(is_roll, dtype=np.float64).copy()
        is_roll[1:] = 0.0  # drop every interior roll; keep the initial open
        return dates, is_roll, roll_premium

    fetcher.fetch_hold_roll_info = patched  # type: ignore[attr-defined]
    return fetcher


async def test_hold_leg_roll_cost_charged_and_reduces_equity():
    """Nonzero bps on a hold-leg signal that rolls in-window: both cost totals
    are positive (on the 0.xx% scale) and final equity drops below the 0-bps run."""
    chains = _build_chains()
    sig = _short_put_signal(hold=True)

    zero = await evaluate_signal(sig, {}, _make_fetcher(chains, spx_series=_spx()))
    costed = await evaluate_signal(
        sig, {}, _make_fetcher(chains, spx_series=_spx()), CostConfig(10.0, 5.0)
    )

    assert zero.total_slippage_paid_pct == 0.0
    assert zero.total_fees_paid_pct == 0.0

    assert costed.total_slippage_paid_pct > 0.0
    assert costed.total_fees_paid_pct > 0.0
    # Percent units, not x100 inflated.
    assert 0.0 < costed.total_slippage_paid_pct < 5.0
    assert 0.0 < costed.total_fees_paid_pct < 5.0
    # 10 bps slippage vs 5 bps fees over identical turnover -> 2:1.
    assert costed.total_slippage_paid_pct == 2.0 * costed.total_fees_paid_pct

    # The drag bites: equity is reduced vs the 0-bps run.
    assert costed.equity_ratio[-1] < zero.equity_ratio[-1]


async def test_interior_roll_adds_cost_on_top_of_entry():
    """Isolate the ROLL assembly: at the SAME bps, the in-window APR->MAY roll
    (round-trip) costs strictly more than a hold leg with only the entry open."""
    chains = _build_chains()
    sig = _short_put_signal(hold=True)

    with_roll = await evaluate_signal(
        sig, {}, _make_fetcher(chains, spx_series=_spx()), CostConfig(10.0, 0.0)
    )
    entry_only = await evaluate_signal(
        sig,
        {},
        _no_interior_roll_fetcher(chains, spx_series=_spx()),
        CostConfig(10.0, 0.0),
    )

    assert with_roll.total_slippage_paid_pct > entry_only.total_slippage_paid_pct


# ── Position-aware hold-leg turnover (FIX round 4) ──────────────────────────
# The entry condition is ``S.close > 0``, so the spx_series drives the leg's
# LATCH: <=0 -> flat (pos_active False), >0 -> held. is_roll = [1,0,0,1,0,0]
# (initial open bar 0 + interior roll bar 3).


def _flat_then_latch(latch_from: int) -> np.ndarray:
    """spx series that latches (>0) from bar ``latch_from`` onward, flat before."""
    s = np.full(len(_DATES), -1.0, dtype=np.float64)
    s[latch_from:] = 1.0
    return s


async def test_never_latched_hold_leg_costs_nothing():
    """A hold leg that NEVER latches (all flat) must incur ZERO cost even though
    its roll flags fire -- no phantom entry/roll cost while flat. Pre-fix the
    flag-only primitive charged 1+2 sides of the never-held notional."""
    chains = _build_chains()
    sig = _short_put_signal(hold=True)
    spx_flat = np.full(len(_DATES), -1.0, dtype=np.float64)  # condition never true

    costed = await evaluate_signal(
        sig, {}, _make_fetcher(chains, spx_series=spx_flat), CostConfig(10.0, 5.0)
    )
    assert costed.total_slippage_paid_pct == 0.0
    assert costed.total_fees_paid_pct == 0.0
    # Never held -> no P&L, no cost -> equity is flat at 1.0.
    np.testing.assert_allclose(costed.equity_ratio, np.ones(len(_DATES)), atol=1e-12)


async def test_roll_in_flat_window_not_charged():
    """Leg latches at bar 4 -- AFTER the interior roll (bar 3). The roll happens
    while flat, so it must NOT be charged: total cost equals the same leg with the
    interior roll removed. Pre-fix the flat-window roll was billed a round-trip."""
    chains = _build_chains()
    sig = _short_put_signal(hold=True)
    spx = _flat_then_latch(4)  # flat bars 0..3 (incl. the bar-3 roll), held 4..5
    cfg = CostConfig(10.0, 5.0)

    with_roll = await evaluate_signal(
        sig, {}, _make_fetcher(chains, spx_series=spx), cfg
    )
    no_interior = await evaluate_signal(
        sig, {}, _no_interior_roll_fetcher(chains, spx_series=spx), cfg
    )

    assert with_roll.total_slippage_paid_pct == no_interior.total_slippage_paid_pct
    assert with_roll.total_fees_paid_pct == no_interior.total_fees_paid_pct
    # The genuine entry (bar-4 latch) is still charged -> a positive cost.
    assert with_roll.total_slippage_paid_pct > 0.0


async def test_roll_while_held_is_charged():
    """Leg latches at bar 1 -- BEFORE the interior roll (bar 3) and held across
    it. The roll survived-while-held IS charged: cost strictly exceeds the same
    leg with the interior roll removed."""
    chains = _build_chains()
    sig = _short_put_signal(hold=True)
    spx = _flat_then_latch(1)  # held bars 1..5, spanning the bar-3 roll
    cfg = CostConfig(10.0, 5.0)

    with_roll = await evaluate_signal(
        sig, {}, _make_fetcher(chains, spx_series=spx), cfg
    )
    no_interior = await evaluate_signal(
        sig, {}, _no_interior_roll_fetcher(chains, spx_series=spx), cfg
    )

    assert with_roll.total_slippage_paid_pct > no_interior.total_slippage_paid_pct
    assert with_roll.total_fees_paid_pct > no_interior.total_fees_paid_pct
