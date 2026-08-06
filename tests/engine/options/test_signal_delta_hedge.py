"""Delta-hedge overlay on the SIGNAL-driven option-leg path (F2 follow-up, P-F2-1).

The F2 commit (52fd782) wired the ⅓-delta VX1 hedge onto the PORTFOLIO option-leg
path but left the SIGNAL-leg build site (``signal_exec`` ~1950) constructing
``_HoldPnLSpec`` with NO hedge fields, so SPEC §5.5/§5.6 (both SIGNAL legs) could
not reproduce end-to-end.  This suite pins the newly-wired signal-leg hedge:

  * hedge $ P&L accrues into the SAME signal-leg equity, ``qty_hedge =
    -factor·option_qty·delta``, rebalanced daily (mirrors
    ``test_delta_hedge.test_hedge_pnl_accrual_exact`` but through the FULL
    ``evaluate_signal`` path);
  * the hedge is OFF on a roll bar and OFF when the VVIX gate is closed;
  * a signal option leg WITHOUT a ``delta_hedge`` config is BYTE-IDENTICAL to the
    pre-change engine (the F2 accrual path stays fully guarded);
  * the loud wiring errors (no ``fetch_delta_hedge_series``; futures_notional +
    hedge) fire.

DB-free / deterministic — a synthetic fetcher exposes ``fetch``,
``fetch_hold_roll_info`` and ``fetch_delta_hedge_series`` exactly as the production
``make_signal_fetcher`` does.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.engine.signal_exec import SignalDataError, evaluate_signal
from tcg.types.options import ByDelta, NearestToTarget
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    DeltaHedgeSpec,
    Input,
    InstrumentOperand,
    InstrumentOptionStream,
    InstrumentSpot,
    Signal,
    SignalRules,
)

# Async tests auto-marked (asyncio_mode="auto").

_DATES = np.array(
    [20200302, 20200303, 20200304, 20200305, 20200306, 20200309], dtype=np.int64
)


def _opt(
    *,
    hold: bool = True,
    nav_times: float = 1.0,
    delta_hedge: DeltaHedgeSpec | None = None,
    sizing_mode: str = "premium_notional",
) -> InstrumentOptionStream:
    return InstrumentOptionStream(
        collection="OPT_VIX",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=0.30, tolerance=0.20),
        stream="mid",
        hold_between_rolls=hold,
        nav_times=nav_times,
        sizing_mode=sizing_mode,  # type: ignore[arg-type]
        delta_hedge=delta_hedge,
    )


def _long_call_signal(
    *,
    weight: float = 10.0,
    nav_times: float = 1.0,
    delta_hedge: DeltaHedgeSpec | None = None,
    sizing_mode: str = "premium_notional",
) -> Signal:
    """Always-latched LONG call (weight>0) + a spot 'always-true' condition input."""
    return Signal(
        id="s_hedge",
        name="signal delta hedge",
        inputs=(
            Input(
                id="C",
                instrument=_opt(
                    nav_times=nav_times,
                    delta_hedge=delta_hedge,
                    sizing_mode=sizing_mode,
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
                    input_id="C",
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


def _make_fetcher(
    *,
    premium: np.ndarray,
    is_roll: np.ndarray,
    roll_premium: np.ndarray,
    delta: np.ndarray | None = None,
    vx1: np.ndarray | None = None,
    gate: np.ndarray | None = None,
    factor: float = 1.0 / 3.0,
    gate_threshold: float = 150.0,
    gate_op: str = "gt",
    with_hedge_channel: bool = True,
    roll_future_ref: np.ndarray | None = None,
    multipliers: tuple[float, float] | None = None,
):
    """Synthetic signal fetcher: option premium LEVEL + spot flat-100 + the
    hold-roll and (optionally) delta-hedge side-channels.

    When ``roll_future_ref`` / ``multipliers`` are supplied the roll-info side-
    channel emits the 4-tuple (futures_notional) shape + a ``fetch_hold_multipliers``
    accessor, so a futures_notional hold leg can be driven end-to-end."""
    spx = np.full(len(_DATES), 100.0, dtype=np.float64)

    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            return _DATES, spx
        if isinstance(instrument, InstrumentOptionStream):
            return _DATES, np.asarray(premium, dtype=np.float64).copy()
        raise KeyError(f"no data for {instrument!r} ({field})")

    async def fetch_hold_roll_info(instrument):
        assert isinstance(instrument, InstrumentOptionStream)
        if roll_future_ref is not None:
            return (
                _DATES,
                np.asarray(is_roll, dtype=np.float64).copy(),
                np.asarray(roll_premium, dtype=np.float64).copy(),
                np.asarray(roll_future_ref, dtype=np.float64).copy(),
            )
        return (
            _DATES,
            np.asarray(is_roll, dtype=np.float64).copy(),
            np.asarray(roll_premium, dtype=np.float64).copy(),
        )

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]

    if multipliers is not None:

        async def fetch_hold_multipliers(instrument):
            return multipliers

        fetch.fetch_hold_multipliers = fetch_hold_multipliers  # type: ignore[attr-defined]

    if with_hedge_channel:

        async def fetch_delta_hedge_series(instrument):
            assert isinstance(instrument, InstrumentOptionStream)
            return (
                (_DATES, np.asarray(delta, dtype=np.float64).copy()),
                (_DATES, np.asarray(vx1, dtype=np.float64).copy()),
                (_DATES, np.asarray(gate, dtype=np.float64).copy()),
                float(factor),
                float(gate_threshold),
                gate_op,
            )

        fetch.fetch_delta_hedge_series = fetch_delta_hedge_series  # type: ignore[attr-defined]

    return fetch


# Constant premium ⇒ the option's OWN P&L is 0, so the signal-leg equity moves
# ONLY from the hedge (the same isolation trick as test_delta_hedge._base_spec).
_P0 = 10.0
_PREMIUM = np.full(len(_DATES), _P0, dtype=np.float64)
_IS_ROLL = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0])  # single segment, open at bar 0
_ROLL_PREMIUM = np.array([_P0, np.nan, np.nan, np.nan, np.nan, np.nan])
_DELTA = np.array([0.5, 0.6, 0.4, 0.7, 0.55, 0.5])
_VX1 = np.array([40.0, 42.0, 41.0, 44.0, 42.0, 40.0])


async def test_signal_hedge_accrual_exact_through_full_path() -> None:
    """Per-step hedge P&L booked into the signal-leg equity equals
    ``-factor·sign·nav_times·(1/ratio[s])·delta[s]·ΔVX1[s]/P0`` to machine epsilon.

    Constant premium ⇒ option contrib is 0 ⇒ ``net_step[s] = ratio[s+1]/ratio[s]-1``
    is PURELY the hedge, so the recurrence is checked against the actual returned
    equity (the 1/ratio equity-coupling included).  The hedge is OFF on the roll bar
    (s=0), verifying the roll-exit gate."""
    factor = 1.0 / 3.0
    sign = 1.0  # weight>0 → long call
    nav_times = 1.0
    gate = np.full(len(_DATES), 200.0)  # VVIX=200 > 150 everywhere → gate ON
    fetch = _make_fetcher(
        premium=_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        delta=_DELTA,
        vx1=_VX1,
        gate=gate,
        factor=factor,
    )
    hedge = DeltaHedgeSpec(factor=factor)
    res = await evaluate_signal(
        _long_call_signal(weight=10.0, nav_times=nav_times, delta_hedge=hedge),
        {},
        fetch,
    )
    ratio = res.equity_ratio
    assert np.all(np.isfinite(ratio))
    T = len(_DATES)
    for s in range(T - 1):
        booked = ratio[s + 1] / ratio[s] - 1.0  # net step = hedge only
        if _IS_ROLL[s] > 0.5:
            # Roll bar ⇒ hedge OFF ⇒ zero (SPEC §5.5 exit (3) "the call rolls").
            assert booked == pytest.approx(0.0, abs=1e-13), f"roll step {s}"
            continue
        dvx = _VX1[s + 1] - _VX1[s]
        expected = -factor * sign * nav_times * (1.0 / ratio[s]) * _DELTA[s] * dvx / _P0
        assert booked == pytest.approx(expected, abs=1e-12), f"step {s}"


async def test_signal_hedge_reconciliation_invariant_holds() -> None:
    """Σ per-input realized_pnl == equity_ratio − 1 with the hedge attached."""
    gate = np.full(len(_DATES), 200.0)
    fetch = _make_fetcher(
        premium=_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        delta=_DELTA,
        vx1=_VX1,
        gate=gate,
    )
    res = await evaluate_signal(
        _long_call_signal(nav_times=2.0, delta_hedge=DeltaHedgeSpec()), {}, fetch
    )
    total = np.zeros_like(res.equity_ratio)
    for p in res.positions:
        total = total + p.realized_pnl
    np.testing.assert_allclose(total, res.equity_ratio - 1.0, rtol=1e-11, atol=1e-13)


async def test_signal_hedge_gate_off_is_flat_and_equals_no_hedge() -> None:
    """VVIX below the threshold ⇒ hedge OFF everywhere ⇒ constant-premium leg stays
    flat at 1.0 AND is BYTE-IDENTICAL to the same leg with no delta_hedge config."""
    gate_off = np.full(len(_DATES), 100.0)  # 100 < 150 → gate closed all days
    fetch_gated = _make_fetcher(
        premium=_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        delta=_DELTA,
        vx1=_VX1,
        gate=gate_off,
    )
    res_gated = await evaluate_signal(
        _long_call_signal(delta_hedge=DeltaHedgeSpec()), {}, fetch_gated
    )
    # Flat: no option P&L (constant premium) + no hedge (gate closed).
    np.testing.assert_allclose(
        res_gated.equity_ratio, np.ones(len(_DATES)), rtol=1e-12, atol=1e-14
    )
    # And identical to the un-hedged leg (guard: gate-closed == no overlay).
    fetch_none = _make_fetcher(
        premium=_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        with_hedge_channel=False,
    )
    res_none = await evaluate_signal(_long_call_signal(delta_hedge=None), {}, fetch_none)
    np.testing.assert_array_equal(res_gated.equity_ratio, res_none.equity_ratio)


async def test_signal_no_hedge_byte_identical_to_pre_change() -> None:
    """A signal option leg with NO delta_hedge is BIT-identical to the leg run
    through a fetcher that has no hedge channel at all — the new path is fully
    guarded (``delta_hedge=None`` ⇒ untouched).  Uses a MOVING premium so the leg
    actually books P&L (a non-trivial curve, not just a flat 1.0)."""
    prem = np.array([10.0, 12.0, 9.0, 11.0, 13.0, 10.0])
    roll_prem = np.array([10.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    # Same fetcher inputs, one WITH a (present but unused) hedge channel and the
    # signal carrying delta_hedge=None, one WITHOUT the channel.
    fetch_a = _make_fetcher(
        premium=prem,
        is_roll=_IS_ROLL,
        roll_premium=roll_prem,
        delta=_DELTA,
        vx1=_VX1,
        gate=np.full(len(_DATES), 200.0),  # channel present + gate open, but…
    )
    res_a = await evaluate_signal(
        _long_call_signal(weight=10.0, delta_hedge=None),  # …config is None ⇒ skip
        {},
        fetch_a,
    )
    fetch_b = _make_fetcher(
        premium=prem,
        is_roll=_IS_ROLL,
        roll_premium=roll_prem,
        with_hedge_channel=False,
    )
    res_b = await evaluate_signal(
        _long_call_signal(weight=10.0, delta_hedge=None), {}, fetch_b
    )
    np.testing.assert_array_equal(res_a.equity_ratio, res_b.equity_ratio)
    # And the curve is genuinely non-flat (the test would be vacuous otherwise).
    assert not np.allclose(res_a.equity_ratio, 1.0)


async def test_signal_hedge_changes_equity_vs_unhedged() -> None:
    """The hedge MOVES the signal-leg equity (proves it is actually accrued): a
    hedged long call over a rising-then-falling VX1 differs from the unhedged leg."""
    prem = np.array([10.0, 12.0, 9.0, 11.0, 13.0, 10.0])
    roll_prem = np.array([10.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    gate = np.full(len(_DATES), 200.0)
    fetch_h = _make_fetcher(
        premium=prem,
        is_roll=_IS_ROLL,
        roll_premium=roll_prem,
        delta=_DELTA,
        vx1=_VX1,
        gate=gate,
    )
    res_h = await evaluate_signal(
        _long_call_signal(delta_hedge=DeltaHedgeSpec()), {}, fetch_h
    )
    fetch_u = _make_fetcher(
        premium=prem, is_roll=_IS_ROLL, roll_premium=roll_prem, with_hedge_channel=False
    )
    res_u = await evaluate_signal(_long_call_signal(delta_hedge=None), {}, fetch_u)
    assert not np.allclose(res_h.equity_ratio, res_u.equity_ratio)


async def test_signal_hedge_daily_rebalance_tracks_changing_delta() -> None:
    """A CHANGING delta re-sizes the hedge every bar: two runs that differ ONLY in
    the delta on one interior bar produce different equities (daily rebalance)."""
    gate = np.full(len(_DATES), 200.0)
    base = dict(
        premium=_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM, vx1=_VX1, gate=gate
    )
    fetch1 = _make_fetcher(delta=_DELTA, **base)
    delta2 = _DELTA.copy()
    delta2[3] = 0.1  # change one interior bar's delta
    fetch2 = _make_fetcher(delta=delta2, **base)
    r1 = await evaluate_signal(_long_call_signal(delta_hedge=DeltaHedgeSpec()), {}, fetch1)
    r2 = await evaluate_signal(_long_call_signal(delta_hedge=DeltaHedgeSpec()), {}, fetch2)
    assert not np.allclose(r1.equity_ratio, r2.equity_ratio)


async def test_signal_hedge_without_channel_raises_loudly() -> None:
    """A hedged hold option whose fetcher LACKS ``fetch_delta_hedge_series`` must
    fail LOUDLY — the hedge cannot be built without the delta/VX1/gate arrays."""
    fetch = _make_fetcher(
        premium=_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        with_hedge_channel=False,
    )
    with pytest.raises(SignalDataError, match="fetch_delta_hedge_series"):
        await evaluate_signal(
            _long_call_signal(delta_hedge=DeltaHedgeSpec()), {}, fetch
        )


async def test_signal_hedge_on_futures_notional_accepted() -> None:
    """GAP B: delta_hedge on a futures_notional hold leg is now ACCEPTED and accrues.

    The hedge sizes off the option's futures-notional quantity (the coefficient of
    ``dprem`` in the option's own contrib): ``option_qty = sign·nav_times·
    (1/ratio[s])·m_opt/(F_ref·m_fut)``.  Constant premium isolates the hedge, so the
    booked step P&L is byte-checked against ``-factor·option_qty·delta[s]·ΔVX1[s]``."""
    factor = 1.0 / 3.0
    sign = 1.0
    nav_times = 1.0
    m_fut, m_opt = 1000.0, 100.0
    F_ref = 18.0
    roll_fref = np.array([F_ref, np.nan, np.nan, np.nan, np.nan, np.nan])
    gate = np.full(len(_DATES), 200.0)  # VVIX=200 > 150 → gate ON
    fetch = _make_fetcher(
        premium=_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        delta=_DELTA,
        vx1=_VX1,
        gate=gate,
        factor=factor,
        roll_future_ref=roll_fref,
        multipliers=(m_fut, m_opt),
    )
    res = await evaluate_signal(
        _long_call_signal(
            weight=10.0,
            nav_times=nav_times,
            delta_hedge=DeltaHedgeSpec(factor=factor),
            sizing_mode="futures_notional",
        ),
        {},
        fetch,
    )
    ratio = res.equity_ratio
    assert np.all(np.isfinite(ratio))
    # the hedge really moved the leg equity (no longer rejected / zeroed).
    assert not np.allclose(ratio, np.ones(len(_DATES)))
    T = len(_DATES)
    for s in range(T - 1):
        booked = ratio[s + 1] / ratio[s] - 1.0  # constant premium ⇒ hedge only
        if _IS_ROLL[s] > 0.5:
            assert booked == pytest.approx(0.0, abs=1e-13), f"roll step {s}"
            continue
        dvx = _VX1[s + 1] - _VX1[s]
        option_qty = sign * nav_times * (1.0 / ratio[s]) * m_opt / (F_ref * m_fut)
        expected = -factor * option_qty * _DELTA[s] * dvx
        assert booked == pytest.approx(expected, abs=1e-12), f"step {s}"


# ── Airtight byte-identical: current signal_exec vs git HEAD (no-hedge leg) ──
def _load_head_signal_exec():
    """Import ``tcg/engine/signal_exec.py`` AT GIT HEAD (pre-signal-hedge-wiring)
    as a standalone module.  Its cross-module imports resolve to the CURRENT tcg
    package (``tcg.types.signal`` gained only a defaulted ``delta_hedge=None`` field
    and ``hold_pnl`` gained only defaulted hedge fields — both backward-compatible),
    so a no-hedge signal run through HEAD's engine == the pre-change behaviour."""
    repo = Path(__file__).resolve().parents[3]
    src = subprocess.run(
        ["git", "show", "HEAD:tcg/engine/signal_exec.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "signal_exec_head.py"
    tmp.write_text(src)
    spec = importlib.util.spec_from_file_location("signal_exec_head", tmp)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["signal_exec_head"] = mod
    spec.loader.exec_module(mod)
    return mod


@given(
    prem=st.lists(st.floats(1.0, 50.0), min_size=2, max_size=8),
    weight=st.sampled_from([-100.0, -10.0, 5.0, 100.0]),
    nav_times=st.floats(0.1, 3.0),
)
@settings(max_examples=30, deadline=None)
def test_prop_no_hedge_signal_leg_byte_identical_to_head(prem, weight, nav_times):
    """A no-hedge hold-mode signal option leg reproduces the HEAD ``evaluate_signal``
    output BIT-for-bit — the new section-3c / 6a wiring is fully guarded when
    ``delta_hedge is None`` (the DEFERRED-gap fix perturbs nothing else)."""
    head = _load_head_signal_exec()
    T = len(prem)
    dates = np.array([20200302 + i for i in range(T)], dtype=np.int64)
    premium = np.asarray(prem, dtype=np.float64)
    is_roll = np.zeros(T, dtype=np.float64)
    is_roll[0] = 1.0
    roll_premium = np.full(T, np.nan, dtype=np.float64)
    roll_premium[0] = premium[0]
    spx = np.full(T, 100.0, dtype=np.float64)

    def make_fetch():
        async def fetch(instrument, field):
            if isinstance(instrument, InstrumentSpot):
                return dates, spx
            if isinstance(instrument, InstrumentOptionStream):
                return dates, premium.copy()
            raise KeyError(instrument)

        async def fetch_hold_roll_info(instrument):
            return (dates, is_roll.copy(), roll_premium.copy())

        fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
        return fetch

    sig = _long_call_signal(weight=weight, nav_times=nav_times, delta_hedge=None)
    r_new = asyncio.run(evaluate_signal(sig, {}, make_fetch())).equity_ratio
    r_old = asyncio.run(head.evaluate_signal(sig, {}, make_fetch())).equity_ratio
    np.testing.assert_array_equal(r_new, r_old)


async def test_signal_hedge_long_call_vx1_up_loses() -> None:
    """DIRECTION check: a LONG call is SHORT the VX1 hedge (qty_hedge<0), so a
    RISING VX1 (with the gate open, off the roll bar) LOSES on the hedge — the
    hedged leg ends BELOW 1.0 while its option P&L is 0 (constant premium)."""
    vx1_up = np.array([40.0, 41.0, 42.0, 43.0, 44.0, 45.0])  # monotonic up
    gate = np.full(len(_DATES), 200.0)
    fetch = _make_fetcher(
        premium=_PREMIUM,
        is_roll=_IS_ROLL,
        roll_premium=_ROLL_PREMIUM,
        delta=np.full(len(_DATES), 0.5),  # constant positive delta
        vx1=vx1_up,
        gate=gate,
    )
    res = await evaluate_signal(
        _long_call_signal(weight=10.0, delta_hedge=DeltaHedgeSpec()), {}, fetch
    )
    # Short the future + rising future ⇒ hedge loses ⇒ equity < 1 (option P&L = 0).
    assert res.equity_ratio[-1] < 1.0
