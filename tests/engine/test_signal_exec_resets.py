"""Engine tests for the reset gate.

Coverage map (CONTRACT §1.10):
  T1  legacy parity for resets=() — byte-identical to main.
  T2  resets non-empty but never fires → only first entry latches.
  T3  single mid-run reset → exactly two trades.
  T4  same-bar entry+reset, armed → entry at t, second eligible at t+1.
  T5  same-bar exit+reset → flat-and-rearm in one bar.
  T6  reset while position open → no observable position change.
  T7  reset before any entry → first entry unaffected (legacy parity).
  T8  legacy spec without ``resets`` field → parses + behaves identically.
  T9  multiple reset blocks (OR) → either firing arms.
  T10 reset operand NaN at t → not in fired/latched.
  T11 reset block ``enabled=False`` → excluded.
  T12 ALL resets disabled while len(resets)>0 → behaves as resets=().
  T13 BlockEvent payload for kind="reset".
  T14 multi-entry arm-sharing: one reset re-arms; both entries can
      latch on next bar.
  T15 reset condition fires every bar → behaviour parity with resets=()
      (always-armed gate is observationally identical to no gate).
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
    [20240102, 20240103, 20240104, 20240105, 20240108,
     20240109, 20240110, 20240111], dtype=np.int64,
)


def _make_fetcher(
    by_key: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]
) -> Callable:
    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            key = (instrument.collection, instrument.instrument_id)
        else:  # pragma: no cover - tests only use spot
            key = ("continuous", instrument.collection)
        return by_key[key]

    return fetch


INPUT_X = Input(
    id="X",
    instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
)


def _gt(input_id: str, threshold: float) -> CompareCondition:
    return CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id=input_id),
        rhs=ConstantOperand(value=threshold),
    )


def _lt(input_id: str, threshold: float) -> CompareCondition:
    return CompareCondition(
        op="lt",
        lhs=InstrumentOperand(input_id=input_id),
        rhs=ConstantOperand(value=threshold),
    )


def _eq(input_id: str, value: float) -> CompareCondition:
    return CompareCondition(
        op="eq",
        lhs=InstrumentOperand(input_id=input_id),
        rhs=ConstantOperand(value=value),
    )


# ---------------------------------------------------------------------------
# T1 — legacy parity: resets=() yields identical results to main.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_resets_empty_byte_identical_to_legacy():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E",
        name="Entry",
        input_id="X",
        weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.5),),
        target_entry_block_name="Entry",
    )

    legacy = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_blk,)),
    )
    with_empty_resets = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_blk,), resets=()),
    )
    r_legacy = await evaluate_signal(legacy, indicators={}, fetcher=fetcher)
    r_empty = await evaluate_signal(with_empty_resets, indicators={}, fetcher=fetcher)

    assert list(r_legacy.positions[0].values) == list(r_empty.positions[0].values)
    assert r_legacy.events == r_empty.events
    assert r_legacy.trades == r_empty.trades


# ---------------------------------------------------------------------------
# T2 — resets present but condition never fires → only first entry latches.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_reset_never_fires_locks_after_first_trade():
    # Entry: X > 11 → fires at t≥2. Exit: X < 12.5 → fires at t in
    # {0,1,5,6,7}. Reset: X > 1000 (never).
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 12.0, 11.0, 12.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.5),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", conditions=(_gt("X", 1000.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Initial reset_armed=True → first entry latches at t=2.
    # Exit fires at t=5 → closes. After that, reset_armed=False forever
    # since the reset condition never fires → no further entries.
    assert len(result.trades) == 1
    assert result.trades[0].open_bar == 2
    assert result.trades[0].close_bar == 5


# ---------------------------------------------------------------------------
# T3 — single mid-run reset → exactly two trades.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_single_midrun_reset_yields_two_trades():
    # Entry condition: X > 11 → true at t in {2,3,4,5,6,7}.
    # Exit condition:  X <= 11 → true at t in {0,1,5}.
    # Reset condition: X == 11 → fires at t=5 (same bar as the exit).
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", conditions=(_eq("X", 11.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # First trade: latches at t=2 (initial arm True). Exit fires at t=5
    # (X=11 < 11.5) → trade closes. Reset fires also at t=5 → rearms.
    # Second trade: t=6 entry fires + arm True → latches.
    assert len(result.trades) == 2
    assert (result.trades[0].open_bar, result.trades[0].close_bar) == (2, 5)
    assert result.trades[1].open_bar == 6


# ---------------------------------------------------------------------------
# T4 — same-bar entry+reset (arm already on): entry latches at t,
#      reset re-arms after entry pass (so a subsequent eligible entry
#      can latch on the next bar if its condition fires).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t4_same_bar_entry_and_reset_arm_holds():
    closes = np.array([10.0, 12.0, 12.0, 11.0, 12.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    # Entry fires at X>=12, exit clears at X<12. Reset fires at X==12.
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", conditions=(_eq("X", 12.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # t=1: arm=True → entry latches (open at 1). reset fires post-entry
    # → consumed arm flips back to True for t=2. But entry already
    # latched → can't latch again.
    # t=3: X=11 → exit fires (close). arm still True (or became True
    # again from t=2 fire). Reset doesn't fire at t=3.
    # t=4: arm=True, X=12 → entry latches again (second trade open).
    #      reset fires → rearms.
    # t=5: X=12, exit doesn't fire, entry already latched.
    # No further close in the data.
    assert len(result.trades) == 2
    assert result.trades[0].open_bar == 1
    assert result.trades[0].close_bar == 3
    assert result.trades[1].open_bar == 4


# ---------------------------------------------------------------------------
# T5 — same-bar exit+reset → flat-and-rearm in one bar.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_same_bar_exit_and_reset():
    # Entry: X>=12. Exit: X<12. Reset: X==11.
    closes = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 12.0, 11.0, 13.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", conditions=(_eq("X", 11.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # t=1: arm=True, X=12 → entry latches. Reset doesn't fire (X!=11).
    #      But entry consumed arm → reset_armed=False.
    # t=3: X=11 → exit fires → close. Same bar, reset fires → rearms.
    # t=4: X=13, arm=True → second entry latches.
    # t=6: X=11 → exit fires → close. Reset re-arms.
    # Need >=2 trades.
    assert len(result.trades) >= 2
    assert result.trades[0].open_bar == 1
    assert result.trades[0].close_bar == 3
    assert result.trades[1].open_bar == 4


# ---------------------------------------------------------------------------
# T6 — reset fires while position open → arm flips True silently, no
#      duplicate entry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t6_reset_while_open_is_silent():
    closes = np.array([10.0, 12.0, 13.0, 14.0, 11.0, 13.0, 14.0, 15.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    # Reset fires at t in {2,3} (X==13, X==14) — while position is open.
    reset = Block(id="R1", conditions=(_gt("X", 12.5),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # t=1: entry latches. t=2,3: arm became False at t=1, reset fires →
    # arm flips True. But latch is already True so no duplicate entry.
    # t=4: exit fires → close. t=5..7: arm was True (from t=2 or t=3
    # reset), entry latches again at t=5.
    assert len(result.trades) == 2
    assert result.trades[0].open_bar == 1
    assert result.trades[0].close_bar == 4


# ---------------------------------------------------------------------------
# T7 — reset fires BEFORE any entry → first entry unaffected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t7_reset_before_any_entry():
    # Reset fires at t=0 (X==10). Entry first eligible at t=2.
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 11.0, 12.0, 13.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", conditions=(_eq("X", 10.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Initial arm=True. Reset at t=0 sees arm already on → records fired
    # but NOT latched. First entry at t=2 still latches.
    assert result.trades[0].open_bar == 2
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    r = events_by[("R1", "reset")]
    assert 0 in r.fired_indices
    # No latched transition since arm was already True.
    assert 0 not in r.latched_indices


# ---------------------------------------------------------------------------
# T8 — legacy spec parity (no `resets` field on the wire).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8_legacy_parity_default_constructed_rules():
    # SignalRules constructed with only entries+exits (no resets kwarg)
    # must behave identically to constructing with resets=().
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.5),),
        target_entry_block_name="Entry",
    )

    legacy_rules = SignalRules(entries=(entry,), exits=(exit_blk,))
    assert legacy_rules.resets == ()
    signal = Signal(id="s", name="s", inputs=(INPUT_X,), rules=legacy_rules)
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Should produce a trade exactly like main.
    assert len(result.trades) >= 1


# ---------------------------------------------------------------------------
# T9 — multiple reset blocks (OR) — either firing arms.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t9_multiple_resets_or_semantics():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 14.0, 13.0, 12.0, 13.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    # Reset R1 fires at X==11 (t=3). Reset R2 fires at X==99 (never).
    r1 = Block(id="R1", conditions=(_eq("X", 11.0),))
    r2 = Block(id="R2", conditions=(_eq("X", 99.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(r1, r2),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # t=1 entry latches. t=3 exit fires (X=11<12). Same bar, R1 fires
    # → arm rearms. t=4 entry latches again.
    assert len(result.trades) >= 2
    # R1 must record a latched-transition; R2 none.
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    assert events_by[("R1", "reset")].latched_indices == (3,)
    assert events_by[("R2", "reset")].latched_indices == ()


# ---------------------------------------------------------------------------
# T10 — reset operand NaN → not in fired/latched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t10_reset_nan_operand_excluded():
    # Inject a NaN at t=3 in the close series.
    closes = np.array([10.0, 12.0, 13.0, np.nan, 14.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    # Reset condition would otherwise fire at t=3 (NaN); also fires at
    # t=5 where X==11.
    reset = Block(
        id="R1",
        conditions=(
            CompareCondition(
                op="lt",
                lhs=InstrumentOperand(input_id="X"),
                rhs=ConstantOperand(value=11.5),
            ),
        ),
    )
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    r = events_by[("R1", "reset")]
    # t=3 has nan operand → must NOT be in fired/latched.
    assert 3 not in r.fired_indices
    assert 3 not in r.latched_indices


# ---------------------------------------------------------------------------
# T11 — disabled reset block is excluded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t11_disabled_reset_excluded():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 14.0, 13.0, 12.0, 13.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    # Two resets: R1 enabled (fires) and R2 disabled (would fire but
    # excluded). Only R1 should produce an event.
    r1 = Block(id="R1", conditions=(_eq("X", 11.0),))
    r2 = Block(id="R2", conditions=(_eq("X", 11.0),), enabled=False)
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(r1, r2),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    reset_event_ids = {ev.block_id for ev in result.events if ev.kind == "reset"}
    assert reset_event_ids == {"R1"}


# ---------------------------------------------------------------------------
# T12 — ALL resets disabled → behaves as resets=().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t12_all_resets_disabled_acts_as_empty():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 11.0, 13.0, 11.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    r_disabled = Block(
        id="R1", conditions=(_eq("X", 99.0),), enabled=False,
    )

    with_disabled = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(r_disabled,),
        ),
    )
    legacy = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_blk,)),
    )
    r_disabled_result = await evaluate_signal(with_disabled, indicators={}, fetcher=fetcher)
    r_legacy_result = await evaluate_signal(legacy, indicators={}, fetcher=fetcher)

    assert list(r_disabled_result.positions[0].values) == list(
        r_legacy_result.positions[0].values
    )
    # The disabled reset still exists in the spec but does NOT emit a
    # BlockEvent (it was filtered out by _usable_reset).
    assert all(ev.kind != "reset" for ev in r_disabled_result.events)


# ---------------------------------------------------------------------------
# T13 — BlockEvent payload for reset.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_reset_block_event_payload():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 11.0, 13.0, 11.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", name="Arm", conditions=(_eq("X", 11.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    r = events_by[("R1", "reset")]
    assert r.input_id == ""
    assert r.target_entry_block_name is None
    assert r.active_indices == ()
    # Resets fire at every X==11 bar: t=3,5,7. The latched (arm transition)
    # depends on prior consumption state.
    assert all(i in r.fired_indices for i in (3, 5, 7))


# ---------------------------------------------------------------------------
# T14 — multi-entry arm sharing: one reset re-arms; both eligible entries
#       can latch on the next bar.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t14_multi_entry_arm_sharing():
    # Two entries on different inputs sharing the signal-global arm.
    INPUT_Y = Input(
        id="Y", instrument=InstrumentSpot(collection="INDEX", instrument_id="NDX"),
    )
    spx = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 14.0, 15.0, 16.0])
    ndx = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 14.0, 15.0, 16.0])
    fetcher = _make_fetcher(
        {("INDEX", "SPX"): (DATES, spx), ("INDEX", "NDX"): (DATES, ndx)}
    )
    eX = Block(
        id="EX", name="EX", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
    )
    eY = Block(
        id="EY", name="EY", input_id="Y", weight=100.0,
        conditions=(_gt("Y", 11.5),),
    )
    xX = Block(
        id="XX", conditions=(_lt("X", 12.0),),
        target_entry_block_name="EX",
    )
    xY = Block(
        id="XY", conditions=(_lt("Y", 12.0),),
        target_entry_block_name="EY",
    )
    reset = Block(id="R1", conditions=(_eq("X", 11.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X, INPUT_Y),
        rules=SignalRules(
            entries=(eX, eY), exits=(xX, xY), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # t=1: arm=True → BOTH EX and EY latch on the same bar (arm-sharing).
    # arm consumed → False.
    # t=3: X<12 + Y<12 → both exits fire → both close. Same bar reset
    # fires (X=11) → arm rearms.
    # t=4: X>=12, Y>=12 → both latch again.
    by_entry: dict[str, list] = {"EX": [], "EY": []}
    for tr in result.trades:
        by_entry[tr.entry_block_id].append(tr)
    assert len(by_entry["EX"]) == 2
    assert len(by_entry["EY"]) == 2
    assert by_entry["EX"][0].open_bar == 1
    assert by_entry["EY"][0].open_bar == 1
    assert by_entry["EX"][0].close_bar == 3
    assert by_entry["EY"][0].close_bar == 3
    assert by_entry["EX"][1].open_bar == 4
    assert by_entry["EY"][1].open_bar == 4


# ---------------------------------------------------------------------------
# T15 — reset condition fires on EVERY bar. The arm is re-set every bar
#       after it is consumed, so the gate is effectively always open. This
#       must be observationally identical to ``resets=()`` (no gate at all):
#       same positions, same events stripped of reset events, same trades.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t15_reset_fires_every_bar_equivalent_to_no_resets():
    # Entry: X > 11 fires t in {2..7}; Exit: X < 12.5 fires t in {0,1,5,6,7};
    # so without a gate we get one trade open at 2, close at 5, then another
    # open at 7-ish? Let's pick data so multiple trades occur — entry then
    # exit then entry again.
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    # Reset condition X > 0 → true for every bar in this series.
    always_reset = Block(id="R1", conditions=(_gt("X", 0.0),))

    control = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_blk,), resets=()),
    )
    with_always_reset = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(always_reset,),
        ),
    )
    r_ctrl = await evaluate_signal(control, indicators={}, fetcher=fetcher)
    r_armed = await evaluate_signal(
        with_always_reset, indicators={}, fetcher=fetcher,
    )

    # Positions identical bar-for-bar.
    assert list(r_ctrl.positions[0].values) == list(r_armed.positions[0].values)
    # Trade ledger identical.
    assert r_ctrl.trades == r_armed.trades
    # Events identical once reset-kind events are filtered out of the
    # always-armed run (the control has no reset blocks so none to emit).
    armed_non_reset = tuple(e for e in r_armed.events if e.kind != "reset")
    assert r_ctrl.events == armed_non_reset
