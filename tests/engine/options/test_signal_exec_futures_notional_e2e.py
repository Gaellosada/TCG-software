"""End-to-end signal path in futures_notional sizing mode (dwh-free).

Drives ``evaluate_signal`` with a hold-mode option input whose ``sizing_mode`` is
``futures_notional`` and a synthetic fetcher supplying the 4-tuple roll info
(``roll_future_ref``) + the multiplier side-channel — proving signal_exec threads
the new fields into ``_HoldPnLSpec`` and the engine reproduces the futures oracle.
"""

from __future__ import annotations

import numpy as np

from tcg.engine.signal_exec import evaluate_signal
from tcg.types.options import ByDelta, NearestToTarget
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    Input,
    InstrumentOperand,
    InstrumentOptionStream,
    InstrumentSpot,
    Signal,
    SignalRules,
)

from _hold_pnl_oracle import (
    IS_ROLL as _IS_ROLL,
    HELD_PREMIUM as _HELD_PREMIUM,
    OWNER_CUR as _OWNER_CUR,
    OWNER_PREV as _OWNER_PREV,
    ROLL_PREMIUM as _ROLL_PREMIUM,
    make_hold_fetch,
    oracle_ratio_futures,
)

# Async tests auto-marked (asyncio_mode="auto").

_ROLL_FREF = np.array([4500.0, np.nan, np.nan, 4520.0, np.nan, np.nan])


def _opt(*, sizing_mode: str, nav_times: float = 1.0) -> InstrumentOptionStream:
    return InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=-0.10, tolerance=0.20),
        stream="mid",
        hold_between_rolls=True,
        nav_times=nav_times,
        sizing_mode=sizing_mode,
    )


def _signal(*, sizing_mode: str, weight: float, nav_times: float):
    return Signal(
        id="s",
        name="fut",
        inputs=(
            Input(
                id="P", instrument=_opt(sizing_mode=sizing_mode, nav_times=nav_times)
            ),
            Input(
                id="S",
                instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
            ),
        ),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="P",
                    weight=weight,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="S", field="close"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            )
        ),
    )


async def test_signal_futures_notional_matches_oracle_sp500() -> None:
    fetch = make_hold_fetch(
        held_premium=_HELD_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        roll_future_ref=_ROLL_FREF,
        multipliers=(50.0, 50.0),  # SP_500: M_fut == M_opt
    )
    res = await evaluate_signal(
        _signal(sizing_mode="futures_notional", weight=-10.0, nav_times=1.0), {}, fetch
    )
    expected = oracle_ratio_futures(
        _OWNER_PREV,
        _OWNER_CUR,
        _IS_ROLL,
        _ROLL_FREF,
        nav_times=1.0,
        weight=-10.0,
        m_fut=50.0,
        m_opt=50.0,
    )
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-10, atol=1e-10)


async def test_signal_futures_notional_vix_multiplier_split() -> None:
    """VIX M_fut=1000 != M_opt=100 flows through signal_exec correctly."""
    roll_fref = np.array([18.0, np.nan, np.nan, 17.5, np.nan, np.nan])
    fetch = make_hold_fetch(
        held_premium=_HELD_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        roll_future_ref=roll_fref,
        multipliers=(1000.0, 100.0),
    )
    res = await evaluate_signal(
        _signal(sizing_mode="futures_notional", weight=-10.0, nav_times=2.0), {}, fetch
    )
    expected = oracle_ratio_futures(
        _OWNER_PREV,
        _OWNER_CUR,
        _IS_ROLL,
        roll_fref,
        nav_times=2.0,
        weight=-10.0,
        m_fut=1000.0,
        m_opt=100.0,
    )
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-10, atol=1e-10)


async def test_signal_premium_vs_futures_differ() -> None:
    """Same premiums/rolls: premium_notional and futures_notional give distinct
    equity — proving the mode really switches the sizing basis end-to-end."""
    common = dict(
        held_premium=_HELD_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
    )
    fetch_prem = make_hold_fetch(**common)
    fetch_fut = make_hold_fetch(
        **common, roll_future_ref=_ROLL_FREF, multipliers=(50.0, 50.0)
    )
    r_prem = await evaluate_signal(
        _signal(sizing_mode="premium_notional", weight=-10.0, nav_times=1.0),
        {},
        fetch_prem,
    )
    r_fut = await evaluate_signal(
        _signal(sizing_mode="futures_notional", weight=-10.0, nav_times=1.0),
        {},
        fetch_fut,
    )
    assert not np.allclose(r_prem.equity_ratio, r_fut.equity_ratio)
