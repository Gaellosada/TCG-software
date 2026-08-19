"""Engine-level tests for F4.1 laddered multi-entry aggregation.

Pure, no dwh. Covers :func:`aggregate_ladder_day` — the fold of a day's
independent rungs into one day-aggregate DayResult:
* weighted sum of dollar P&L (equal-contracts and equal-notional weights);
* the DayPnl invariants (usd == pts*multiplier; total == option + hedge - cost);
* n_fallback_fills summed unweighted (it is an event count);
* skipped rungs contribute nothing; an all-skipped day is a skipped aggregate;
* the rungs are retained on ``entries`` for the per-rung readout.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tcg.engine.intraday_backtest import aggregate_ladder_day
from tcg.types.intraday import DayPnl, DayResult, LadderEntry


def _ts(minute: int) -> datetime:
    return datetime(2025, 2, 3, 14, minute, tzinfo=timezone.utc)


def _entry(
    minute: int,
    contracts: float,
    *,
    option_pts: float | None = None,
    hedge_pts: float = 0.0,
    cost_pts: float = 0.0,
    n_fallback: int = 0,
    mult: float = 50.0,
) -> LadderEntry:
    """A traded (``option_pts`` set) or skipped (``option_pts`` None) rung."""
    if option_pts is None:
        res = DayResult(date=20250203, status="skipped", skip_reason="no_contract")
    else:
        total_pts = option_pts + hedge_pts - cost_pts
        res = DayResult(
            date=20250203,
            status="ok",
            pnl=DayPnl(
                option_pnl_pts=option_pts,
                hedge_pnl_pts=hedge_pts,
                total_pnl_pts=total_pts,
                total_pnl_usd=total_pts * mult,
                cost_pts=cost_pts,
                cost_usd=cost_pts * mult,
                n_fallback_fills=n_fallback,
            ),
        )
    return LadderEntry(entry_ts=_ts(minute), contracts=contracts, result=res)


def test_equal_contracts_day_is_sum_of_rungs() -> None:
    rungs = [
        _entry(0, 1.0, option_pts=2.0, hedge_pts=-0.5, cost_pts=0.1),
        _entry(30, 1.0, option_pts=-1.0, hedge_pts=0.2, cost_pts=0.1),
    ]
    agg = aggregate_ladder_day(20250203, rungs, multiplier=50.0)
    assert agg.status == "ok"
    # total_pnl_usd == sum of per-rung weighted dollar contributions.
    expected_usd = sum(e.contracts * e.result.pnl.total_pnl_usd for e in rungs)
    assert agg.pnl.total_pnl_usd == expected_usd
    # DayPnl invariants preserved on the aggregate.
    assert agg.pnl.total_pnl_usd == agg.pnl.total_pnl_pts * 50.0
    assert abs(
        agg.pnl.total_pnl_pts
        - (agg.pnl.option_pnl_pts + agg.pnl.hedge_pnl_pts - agg.pnl.cost_pts)
    ) < 1e-9
    # Rungs retained for the per-rung readout.
    assert len(agg.entries) == 2


def test_equal_notional_weights_scale_the_contribution() -> None:
    # A rung with weight 2 contributes twice its per-contract dollars.
    rungs = [
        _entry(0, 1.0, option_pts=3.0),
        _entry(30, 2.0, option_pts=3.0),
    ]
    agg = aggregate_ladder_day(20250203, rungs, multiplier=50.0)
    # 1*150 + 2*150 = 450.
    assert agg.pnl.total_pnl_usd == 3.0 * 50.0 + 2.0 * 3.0 * 50.0


def test_n_fallback_summed_unweighted() -> None:
    rungs = [
        _entry(0, 5.0, option_pts=1.0, n_fallback=2),
        _entry(30, 5.0, option_pts=1.0, n_fallback=3),
    ]
    agg = aggregate_ladder_day(20250203, rungs, multiplier=50.0)
    assert agg.pnl.n_fallback_fills == 5  # counts, not weighted by contracts


def test_skipped_rung_contributes_nothing() -> None:
    rungs = [
        _entry(0, 1.0, option_pts=4.0),
        _entry(30, 1.0, option_pts=None),  # skipped
    ]
    agg = aggregate_ladder_day(20250203, rungs, multiplier=50.0)
    assert agg.status == "ok"
    assert agg.pnl.total_pnl_usd == 4.0 * 50.0
    assert len(agg.entries) == 2  # skipped rung still retained on the readout


def test_all_skipped_day_is_skipped_aggregate() -> None:
    rungs = [_entry(0, 1.0, option_pts=None), _entry(30, 1.0, option_pts=None)]
    agg = aggregate_ladder_day(20250203, rungs, multiplier=50.0)
    assert agg.status == "skipped"
    assert agg.skip_reason == "no_contract"
    assert agg.pnl is None
    assert len(agg.entries) == 2


def test_empty_rungs_is_skipped_no_fills() -> None:
    agg = aggregate_ladder_day(20250203, [], multiplier=50.0)
    assert agg.status == "skipped"
    assert agg.skip_reason == "no_ladder_fills"
