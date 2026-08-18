"""Engine tests for ``Block.reset_on_actual_exit`` — the LEGACY §4.2
"since last ACTUAL exit" reset semantics for a THEN-chain entry targeted by
an exit (SPEC §5.2 ShortPutHVOLout GFC fix, PR #92).

BUG BEING FIXED
───────────────
A THEN-chain entry ``E`` (arm-group THEN fire-group) that is TARGETED by an
exit ``X`` receives an always-on ``chain_reset`` = the RAW OR of ``X``'s firing
bars. In ``_sequence_active`` that array aborts any in-flight candidate on
EVERY bar the exit CONDITION is true — even while ``E`` holds NO open position.

So if the arm fires, then the exit CONDITION is momentarily true for a few bars
(with the entry still FLAT — armed but not yet fired), the pending candidate is
wrongly disarmed and the later fire-group never completes. This is exactly the
2008-09 GFC miss: HVOL arms 09-03, ``HV20<HV30`` (the exit cond) holds
09-04..09-11 while the leg is still SHORT (not yet flat), and the fire on 09-12
is lost — so the regime never goes ON and the leg books the full crash drawdown.

The legacy §4.2 FSM keeps OFF_READY armed through that gap: its reset is "since
the last ACTUAL exit" (a real position CLOSE), NOT "since the exit condition was
last true".

``reset_on_actual_exit=True`` (opt-in, default False = byte-identical historical
behaviour) restores the legacy semantics: the in-flight arm is aborted ONLY on a
bar an exit ACTUALLY closes an OPEN position of this entry.

The synthetic series below reproduces the arm / exit-cond-true-while-flat / fire
pattern deterministically — NO live data.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pytest

from tcg.engine.signal_exec import evaluate_signal
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    Input,
    InstrumentOperand,
    InstrumentSpot,
    Signal,
    SignalRules,
)

DATES = np.array(
    [20080901, 20080902, 20080903, 20080904, 20080905, 20080908, 20080909, 20080912],
    dtype=np.int64,
)


def _make_fetcher(
    by_key: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]],
) -> Callable:
    async def fetch(instrument, field):
        key = (instrument.collection, instrument.instrument_id)
        return by_key[key]

    return fetch


def _gt(input_id: str, threshold: float) -> CompareCondition:
    return CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id=input_id),
        rhs=ConstantOperand(value=threshold),
    )


_LEG = Input(id="LEG", instrument=InstrumentSpot(collection="C", instrument_id="LEG"))
_ARM = Input(id="ARM", instrument=InstrumentSpot(collection="C", instrument_id="ARM"))
_FIRE = Input(id="FIRE", instrument=InstrumentSpot(collection="C", instrument_id="FIRE"))
_EXIT = Input(id="EXIT", instrument=InstrumentSpot(collection="C", instrument_id="EXIT"))


def _leg_prices() -> np.ndarray:
    # Strictly rising, arbitrary — positions reflect the LATCH, not the price.
    return np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0])


def _signal(*, arm, fire, exit_, reset_on_actual_exit: bool) -> tuple[Signal, Callable]:
    fetcher = _make_fetcher(
        {
            ("C", "LEG"): (DATES, _leg_prices()),
            ("C", "ARM"): (DATES, np.asarray(arm, dtype=np.float64)),
            ("C", "FIRE"): (DATES, np.asarray(fire, dtype=np.float64)),
            ("C", "EXIT"): (DATES, np.asarray(exit_, dtype=np.float64)),
        }
    )
    entry = Block(
        id="E_flat",
        name="hvol_on",
        input_id="LEG",
        weight=100.0,
        conditions=(_gt("ARM", 0.5), _gt("FIRE", 0.5)),
        links={1: 100},  # THEN boundary at index 1: group0={arm} THEN group1={fire}
        reset_on_actual_exit=reset_on_actual_exit,
    )
    exit_blk = Block(
        id="X_off",
        conditions=(_gt("EXIT", 0.5),),
        target_entry_block_names=("hvol_on",),
    )
    sig = Signal(
        id="s",
        name="s",
        inputs=(_LEG, _ARM, _FIRE, _EXIT),
        rules=SignalRules(entries=(entry,), exits=(exit_blk,)),
    )
    return sig, fetcher


def _leg_positions(result) -> np.ndarray:
    pos = next(p for p in result.positions if p.input_id == "LEG")
    return np.asarray(pos.values, dtype=np.float64)


# --------------------------------------------------------------------------- #
# The GFC pattern: arm at bar 2, exit CONDITION true bars 3..5 WHILE FLAT,
# fire at bar 7.  A faithful engine must let the candidate survive the exit-cond
# window (no open position to close) and fire at bar 7.
# --------------------------------------------------------------------------- #

_ARM_SER = [0, 0, 1, 0, 0, 0, 0, 0]
_FIRE_SER = [0, 0, 0, 0, 0, 0, 0, 1]
_EXIT_SER = [0, 0, 0, 1, 1, 1, 0, 0]


@pytest.mark.asyncio
async def test_gfc_pattern_fixed_when_reset_on_actual_exit_true():
    """FIX asserted: with the flag ON the armed candidate survives the
    exit-condition-true-while-flat window and FIRES at bar 7 → the leg latches."""
    sig, fetcher = _signal(
        arm=_ARM_SER, fire=_FIRE_SER, exit_=_EXIT_SER, reset_on_actual_exit=True
    )
    result = await evaluate_signal(sig, indicators={}, fetcher=fetcher)
    pos = _leg_positions(result)

    # Latches at the fire bar (7) and stays open (no exit after) → +1.0.
    assert pos[7] == pytest.approx(1.0), pos
    assert result.trades and result.trades[0].open_bar == 7, result.trades
    # No open position during the exit-cond window (bars 3..5): still FLAT.
    assert list(pos[:7]) == [0.0] * 7, pos


@pytest.mark.asyncio
async def test_gfc_pattern_historical_default_misses_the_fire():
    """CONTROL (documents the bug + proves opt-out is byte-identical): with the
    flag OFF (default), the raw exit-condition abort disarms the pending
    candidate during bars 3..5, so the fire at bar 7 is LOST → never latches."""
    sig, fetcher = _signal(
        arm=_ARM_SER, fire=_FIRE_SER, exit_=_EXIT_SER, reset_on_actual_exit=False
    )
    result = await evaluate_signal(sig, indicators={}, fetcher=fetcher)
    pos = _leg_positions(result)

    assert list(pos) == [0.0] * 8, pos  # never latches — the documented gap
    assert result.trades == (), result.trades


# --------------------------------------------------------------------------- #
# Guard: with the flag ON, a REAL exit (fired while a position is OPEN) still
# closes the leg, AND the entry re-arms + re-fires afterwards ("since last actual
# exit").  Proves the fix does not disable exits.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_actual_exit_still_closes_and_rearms_when_flag_true():
    arm = [0, 1, 0, 0, 0, 1, 0, 0]
    fire = [0, 0, 1, 0, 0, 0, 1, 0]
    exit_ = [0, 0, 0, 0, 1, 0, 0, 0]  # bar 4: fires WHILE the leg is open → closes
    sig, fetcher = _signal(
        arm=arm, fire=fire, exit_=exit_, reset_on_actual_exit=True
    )
    result = await evaluate_signal(sig, indicators={}, fetcher=fetcher)
    pos = _leg_positions(result)

    # Latch at bar 2 (arm@1, fire@2); OPEN bars 2..3; real exit@4 closes; re-arm@5
    # (arm@5, fire@6) → latch again at bar 6.
    assert list(pos) == [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0], pos
    assert [t.open_bar for t in result.trades] == [2, 6], result.trades
    assert result.trades[0].close_bar == 4, result.trades
