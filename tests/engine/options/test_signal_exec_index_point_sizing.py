"""GAP C — per-index-point sizing on a futures_notional option leg (dwh-free).

Drives ``evaluate_signal`` with a hold-mode futures_notional VIX option leg and
proves that ``apply_contract_multiplier=False`` (per-INDEX-POINT, legacy VIX
sizing) collapses the m_opt/m_fut ratio to 1.0 — sizing the leg 10x higher than the
default multiplier path (VIX m_fut=1000, m_opt=100 ⇒ ratio 0.1), while the SPX case
(m_fut==m_opt==50 ⇒ ratio 1.0) is BYTE-IDENTICAL either way (the flag is a no-op).

The multipliers are supplied by the synthetic fetcher exactly as the core layer's
``fetch_hold_multipliers`` resolves them; signal_exec then applies the collapse.
"""

from __future__ import annotations

import numpy as np
import pytest

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

_ROLL_FREF = np.array([18.0, np.nan, np.nan, 17.5, np.nan, np.nan])


def _opt(
    *, collection: str, apply_contract_multiplier: bool
) -> InstrumentOptionStream:
    return InstrumentOptionStream(
        collection=collection,
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=0.30, tolerance=0.20),
        stream="mid",
        hold_between_rolls=True,
        nav_times=1.0,
        sizing_mode="futures_notional",
        apply_contract_multiplier=apply_contract_multiplier,
    )


def _signal(*, collection: str, apply_contract_multiplier: bool):
    return Signal(
        id="s",
        name="index_point",
        inputs=(
            Input(
                id="P",
                instrument=_opt(
                    collection=collection,
                    apply_contract_multiplier=apply_contract_multiplier,
                ),
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
                    weight=10.0,
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


async def test_vix_index_point_matches_collapsed_oracle() -> None:
    """apply_contract_multiplier=False ⇒ the leg sizes as if m_opt == m_fut."""
    fetch = make_hold_fetch(
        held_premium=_HELD_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        roll_future_ref=_ROLL_FREF,
        multipliers=(1000.0, 100.0),  # VIX raw split
    )
    res = await evaluate_signal(
        _signal(collection="OPT_VIX", apply_contract_multiplier=False), {}, fetch
    )
    # per-index-point ⇒ oracle with m_opt collapsed to m_fut (ratio 1.0).
    expected = oracle_ratio_futures(
        _OWNER_PREV,
        _OWNER_CUR,
        _IS_ROLL,
        _ROLL_FREF,
        nav_times=1.0,
        weight=10.0,
        m_fut=1000.0,
        m_opt=1000.0,
    )
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-10, atol=1e-10)


async def test_vix_index_point_is_10x_the_multiplier_path() -> None:
    """VIX magnitude is amplified ~10x under per-index-point (m_opt/m_fut 0.1 → 1.0).

    The FIRST booked step (bars 0→1, ratio[0]==1 in BOTH runs) is EXACTLY 10x — the
    clean linear signature of the m_opt collapse.  Beyond it the two equities
    compound off different NAVs (and re-size at the roll), so the exact per-step
    scaling gives way to a uniform AMPLIFICATION: |dev_idx| >= |dev_mult| on every
    bar (strictly on the non-zero ones)."""
    # Single-segment-then-roll fixture (the shared oracle fixture): a two-segment
    # short call whose premium falls (a long-call loss in this weight sign).
    common = dict(
        held_premium=_HELD_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        roll_future_ref=_ROLL_FREF,
        multipliers=(1000.0, 100.0),
    )
    r_mult = await evaluate_signal(
        _signal(collection="OPT_VIX", apply_contract_multiplier=True),
        {},
        make_hold_fetch(**common),
    )
    r_idx = await evaluate_signal(
        _signal(collection="OPT_VIX", apply_contract_multiplier=False),
        {},
        make_hold_fetch(**common),
    )
    dev_mult = r_mult.equity_ratio - 1.0
    dev_idx = r_idx.equity_ratio - 1.0
    # bar 1: first booked step, ratio[0]==1 in both → EXACT 10x.
    assert abs(dev_mult[1]) > 1e-12
    assert dev_idx[1] == pytest.approx(10.0 * dev_mult[1], rel=1e-9)
    # uniform amplification everywhere (never smaller in magnitude).
    assert np.all(np.abs(dev_idx) >= np.abs(dev_mult) - 1e-15)
    nz = np.abs(dev_mult) > 1e-12
    assert np.all(np.abs(dev_idx[nz]) > np.abs(dev_mult[nz]))


async def test_spx_unchanged_either_way() -> None:
    """SP_500 m_fut==m_opt==50 ⇒ the flag is a NO-OP (byte-identical equity)."""
    common = dict(
        held_premium=_HELD_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        roll_future_ref=_ROLL_FREF,
        multipliers=(50.0, 50.0),
    )
    r_true = await evaluate_signal(
        _signal(collection="OPT_SP_500", apply_contract_multiplier=True),
        {},
        make_hold_fetch(**common),
    )
    r_false = await evaluate_signal(
        _signal(collection="OPT_SP_500", apply_contract_multiplier=False),
        {},
        make_hold_fetch(**common),
    )
    np.testing.assert_array_equal(r_true.equity_ratio, r_false.equity_ratio)
