"""Engine tests for the per-block require-reset binding (supersedes Task 1).

This feature replaces Task 1's signal-global ``reset_armed`` with a
per-block ``block_arm`` map keyed by ``Block.id``, populated only for
entries and exits carrying a non-None ``requires_reset_block_id``.
Unbound blocks have NO reset gate (pre-Task-1 unconditional firing).

Coverage map:
  T1   legacy parity: ``resets=()`` byte-identical to pre-reset main.
  T2   resets present, ALL bound entries with reset that never fires →
       blocks can fire once, then gate stays closed.
  T3   single mid-run reset bound to entry → exactly two trades.
  T4   same-bar entry+reset (bound): entry consumes initial arm, reset
       re-arms after entry pass → next eligible bar can re-fire.
  T5   same-bar exit+reset (bound entry+exit): flat-and-rearm in one bar.
  T6   reset fires while position open (bound entry): arm flips silently.
  T7   reset fires BEFORE any bound entry → arm already True → no marker.
  T8   legacy spec (default-constructed rules, no resets) → identical to
       resets=() + no bindings.
  T9   multiple reset blocks bound by different entries → each reset arms
       its own bound block only.
  T10  reset operand NaN (bound entry) → not in fired/latched, arm unchanged.
  T11  disabled reset block excluded from BlockEvents; binding to it
       becomes effectively unbound at runtime.
  T12  ALL resets disabled while len(resets)>0 → behaves as resets=()
       (no usable reset → bindings can't transition).
  T13  BlockEvent payload for reset (bound).
  T14  multi-entry binding to one reset: one reset re-arms; both entries
       can latch on next bar; ONE latched_indices entry on the reset.
  T15  reset condition fires every bar (bound entry) → observationally
       identical to no-binding control once reset events are filtered.

  B1   resets=(R,) + zero bindings → byte-identical to resets=() control.
  B2   bound entry: first fire passes, second fire blocked until reset.
  B3   reset arms bound entry post-exit; without reset, second cycle fails.
  B4   bound exit: arm-after-fire-then-need-reset cycle.
  B5   two entries bound to same R: R fires once → both arm; ONE marker.
  B6   two entries bound to DIFFERENT resets R1, R2: each reset arms its
       own entry only; cross-reset fires don't arm the other.
  B7   reset fires when ONE of 3 bound blocks needs arming → marker.
  B8   reset fires when ALL bound blocks already armed → NO marker.
  B9   bound entry: latched==True blocks re-latch regardless of arm.
  B10  bound exit: target_entry not latched → no fire, no arm consumption.
  B11  API: requires_reset_block_id="<unknown>" on entry → SignalValidationError.
  B12  API: reset block carrying requires_reset_block_id → SignalValidationError.
  B13  bound entry to a DISABLED reset → fires once, then can never refire.
  B14  reset NaN at fire bar → no fire, no arm transition.
  B15  placeholder block carrying requires_reset_block_id → accepted +
       filtered by ``_usable_*``.
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
# T2 — bound entry + reset that never fires → entry latches once (initial
#      arm), then can never refire because the bound reset never arms it.
#      Rewritten for per-block semantics: WITHOUT a binding the entry
#      would re-latch freely; WITH a binding to a never-firing reset the
#      arm is consumed and never re-armed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_reset_never_fires_locks_after_first_trade():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 12.0, 11.0, 12.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
        requires_reset_block_id="R1",
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
    # Initial arm=True → entry latches at t=2. arm becomes False.
    # Exit fires at t=5 → closes. R1 never fires → arm stays False forever.
    assert len(result.trades) == 1
    assert result.trades[0].open_bar == 2
    assert result.trades[0].close_bar == 5


# ---------------------------------------------------------------------------
# T3 — single mid-run reset (bound) → exactly two trades.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_single_midrun_reset_yields_two_trades():
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
        requires_reset_block_id="R1",
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
    assert len(result.trades) == 2
    assert (result.trades[0].open_bar, result.trades[0].close_bar) == (2, 5)
    assert result.trades[1].open_bar == 6


# ---------------------------------------------------------------------------
# T4 — same-bar entry+reset (bound entry).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t4_same_bar_entry_and_reset_arm_holds():
    closes = np.array([10.0, 12.0, 12.0, 11.0, 12.0, 12.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
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
    assert len(result.trades) == 2
    assert result.trades[0].open_bar == 1
    assert result.trades[0].close_bar == 3
    assert result.trades[1].open_bar == 4


# ---------------------------------------------------------------------------
# T5 — same-bar exit+reset → flat-and-rearm in one bar (bound entry).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_same_bar_exit_and_reset():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 12.0, 11.0, 13.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
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
    assert len(result.trades) >= 2
    assert result.trades[0].open_bar == 1
    assert result.trades[0].close_bar == 3
    assert result.trades[1].open_bar == 4


# ---------------------------------------------------------------------------
# T6 — reset fires while position open → arm flips silently, no duplicate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t6_reset_while_open_is_silent():
    closes = np.array([10.0, 12.0, 13.0, 14.0, 11.0, 13.0, 14.0, 15.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", conditions=(_gt("X", 12.5),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.trades) == 2
    assert result.trades[0].open_bar == 1
    assert result.trades[0].close_bar == 4


# ---------------------------------------------------------------------------
# T7 — reset fires BEFORE any bound entry → first entry unaffected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t7_reset_before_any_entry():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 11.0, 12.0, 13.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
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
    # Initial arm=True. Reset at t=0 sees arm already True → ineffective
    # (per-fire effectiveness, Sign 2) → fired but NOT latched.
    assert result.trades[0].open_bar == 2
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    r = events_by[("R1", "reset")]
    assert 0 in r.fired_indices
    assert 0 not in r.latched_indices


# ---------------------------------------------------------------------------
# T8 — legacy spec parity (default-constructed rules, no resets).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8_legacy_parity_default_constructed_rules():
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
    assert len(result.trades) >= 1


# ---------------------------------------------------------------------------
# T9 — multiple reset blocks, each bound by a different entry. R1 fires;
#      R2 never fires. Only R1 should arm its bound entry, and only R1
#      records a latched_indices transition.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t9_multiple_resets_or_semantics():
    INPUT_Y = Input(
        id="Y", instrument=InstrumentSpot(collection="INDEX", instrument_id="NDX"),
    )
    closes_x = np.array([10.0, 12.0, 13.0, 11.0, 14.0, 13.0, 12.0, 13.0])
    closes_y = np.array([10.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 13.0])
    fetcher = _make_fetcher(
        {("INDEX", "SPX"): (DATES, closes_x), ("INDEX", "NDX"): (DATES, closes_y)}
    )
    eX = Block(
        id="EX", name="EX", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    eY = Block(
        id="EY", name="EY", input_id="Y", weight=100.0,
        conditions=(_gt("Y", 11.5),),
        requires_reset_block_id="R2",
    )
    xX = Block(id="XX", conditions=(_lt("X", 12.0),), target_entry_block_name="EX")
    xY = Block(id="XY", conditions=(_lt("Y", 12.0),), target_entry_block_name="EY")
    # R1 fires at X==11 (t=3); R2 fires only at X==99 (never).
    r1 = Block(id="R1", conditions=(_eq("X", 11.0),))
    r2 = Block(id="R2", conditions=(_eq("X", 99.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X, INPUT_Y),
        rules=SignalRules(
            entries=(eX, eY), exits=(xX, xY), resets=(r1, r2),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    # R1 arms EX at t=3. R2 never fires → never arms EY.
    assert events_by[("R1", "reset")].latched_indices == (3,)
    assert events_by[("R2", "reset")].latched_indices == ()


# ---------------------------------------------------------------------------
# T10 — reset operand NaN (bound entry) → not in fired/latched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t10_reset_nan_operand_excluded():
    closes = np.array([10.0, 12.0, 13.0, np.nan, 14.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
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
    assert 3 not in r.fired_indices
    assert 3 not in r.latched_indices


# ---------------------------------------------------------------------------
# T11 — disabled reset block excluded; bound block becomes effectively
#       unbound at runtime (its binding points to no usable reset, so the
#       arm never flips back to True after the first fire).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t11_disabled_reset_excluded():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 14.0, 13.0, 12.0, 13.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
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
# T12 — ALL resets disabled while binding present → bound entry fires
#       once (initial arm), then never refires because the bound reset is
#       not usable. The disabled reset emits no event.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t12_all_resets_disabled_acts_as_empty():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 11.0, 13.0, 11.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    r_disabled = Block(
        id="R1", conditions=(_eq("X", 99.0),), enabled=False,
    )

    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(r_disabled,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Initial arm True → first cycle opens; arm consumed → never refires.
    assert len(result.trades) == 1
    # Disabled reset emits no BlockEvent.
    assert all(ev.kind != "reset" for ev in result.events)


# ---------------------------------------------------------------------------
# T13 — BlockEvent payload for reset (bound entry).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t13_reset_block_event_payload():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 11.0, 13.0, 11.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
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
    # Resets fire at every X==11 bar: t=3,5,7.
    assert all(i in r.fired_indices for i in (3, 5, 7))


# ---------------------------------------------------------------------------
# T14 — multi-entry binding to one reset: both arm together; ONE marker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t14_multi_entry_arm_sharing():
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
        requires_reset_block_id="R1",
    )
    eY = Block(
        id="EY", name="EY", input_id="Y", weight=100.0,
        conditions=(_gt("Y", 11.5),),
        requires_reset_block_id="R1",
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
    # ONE latched_indices entry per reset fire that armed ≥1 block (Sign 2).
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    assert events_by[("R1", "reset")].latched_indices == (3,)


# ---------------------------------------------------------------------------
# T15 — reset condition fires on EVERY bar (bound entry). Observationally
#       identical to no-binding control once reset events are filtered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t15_reset_fires_every_bar_equivalent_to_no_resets():
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    # Control: unbound entry, no resets.
    entry_ctrl = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    # Bound: identical entry, binding to a reset that fires every bar.
    entry_bound = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
        requires_reset_block_id="R1",
    )
    always_reset = Block(id="R1", conditions=(_gt("X", 0.0),))

    control = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry_ctrl,), exits=(exit_blk,), resets=()),
    )
    with_always_reset = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry_bound,), exits=(exit_blk,), resets=(always_reset,),
        ),
    )
    r_ctrl = await evaluate_signal(control, indicators={}, fetcher=fetcher)
    r_armed = await evaluate_signal(
        with_always_reset, indicators={}, fetcher=fetcher,
    )

    assert list(r_ctrl.positions[0].values) == list(r_armed.positions[0].values)
    assert r_ctrl.trades == r_armed.trades
    armed_non_reset = tuple(e for e in r_armed.events if e.kind != "reset")
    assert r_ctrl.events == armed_non_reset


# ---------------------------------------------------------------------------
# B1 — SUPERSESSION proof: resets=(R,) + zero bindings → byte-identical
#      to resets=() control. Tests that a reset block with NO bound blocks
#      decays to decorative observation (fired_indices populated;
#      latched_indices empty; positions/trades unchanged).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_resets_with_zero_bindings_equals_no_resets():
    closes = np.array([10.0, 12.0, 13.0, 11.0, 14.0, 11.0, 13.0, 14.0])
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

    control = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_blk,)),
    )
    with_unbound_reset = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), exits=(exit_blk,), resets=(reset,)),
    )
    r_ctrl = await evaluate_signal(control, indicators={}, fetcher=fetcher)
    r_unbound = await evaluate_signal(with_unbound_reset, indicators={}, fetcher=fetcher)

    # Positions and trades byte-identical.
    assert list(r_ctrl.positions[0].values) == list(r_unbound.positions[0].values)
    assert r_ctrl.trades == r_unbound.trades
    # Entry/exit events identical.
    non_reset_ctrl = tuple(e for e in r_ctrl.events if e.kind != "reset")
    non_reset_unbound = tuple(e for e in r_unbound.events if e.kind != "reset")
    assert non_reset_ctrl == non_reset_unbound
    # Reset emits decorative-only: fired bars (X==11 → t=3,5) but NO
    # latched (no bound block to arm).
    events_by = {(ev.block_id, ev.kind): ev for ev in r_unbound.events}
    r = events_by[("R1", "reset")]
    assert 3 in r.fired_indices and 5 in r.fired_indices
    assert r.latched_indices == ()


# ---------------------------------------------------------------------------
# B2 — Bound entry: first fire passes (initial arm=True); second fire
#      blocked because the bound reset never fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_bound_entry_first_fire_passes_second_blocked():
    # Entry: X > 11 fires t∈{2..4, 6, 7}. Exit: X < 11.5 fires at t=5
    # (X=11). Reset never fires. Expect ONE trade.
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    reset = Block(id="R1", conditions=(_eq("X", 99.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.trades) == 1
    assert (result.trades[0].open_bar, result.trades[0].close_bar) == (2, 5)


# ---------------------------------------------------------------------------
# B3 — After R fires post-exit, bound entry refires; without R fire,
#      second cycle never opens.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b3_reset_after_exit_rearms_bound_entry():
    # Entry: X > 11. Exit: X < 11.5. Reset: X == 11.
    # With reset arming at t=5 → second trade opens at t=6.
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    arming_reset = Block(id="R1", conditions=(_eq("X", 11.0),))
    never_reset = Block(id="R1", conditions=(_eq("X", 99.0),))

    s_arming = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(arming_reset,),
        ),
    )
    s_never = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(never_reset,),
        ),
    )
    r_arming = await evaluate_signal(s_arming, indicators={}, fetcher=fetcher)
    r_never = await evaluate_signal(s_never, indicators={}, fetcher=fetcher)
    # With arming reset: 2 trades. Without arming: 1 trade.
    assert len(r_arming.trades) == 2
    assert len(r_never.trades) == 1


# ---------------------------------------------------------------------------
# B4 — Bound exit: arm-after-fire-then-need-reset cycle. The exit fires
#      once, then disarms; even if the exit condition fires again later,
#      it cannot clear another latch until the bound reset re-arms it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b4_bound_exit_arm_after_fire():
    # Bound EXIT (not the entry). Entry unbound — can re-latch freely.
    # Exit: X < 11.5 fires at t=5 then again at... we engineer two exit
    # bars. Without a reset between them, the second exit fire is
    # blocked by the bound-exit arm.
    closes = np.array([10.0, 11.0, 13.0, 14.0, 13.0, 11.0, 13.0, 11.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
    )
    exit_bound = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
        requires_reset_block_id="R1",
    )
    # Reset never fires.
    reset = Block(id="R1", conditions=(_eq("X", 99.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_bound,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # First trade closes at t=5 (exit fires + initial arm True).
    # Trade 2 opens at t=6 (entry unbound). Exit fires again at t=7,
    # but exit is bound and disarmed → cannot clear → trade 2 stays open.
    assert len(result.trades) == 2
    assert (result.trades[0].open_bar, result.trades[0].close_bar) == (2, 5)
    assert result.trades[1].open_bar == 6
    assert result.trades[1].close_bar is None


# ---------------------------------------------------------------------------
# B5 — Two entries bound to the same R: R fires once → both arm; ONE
#      latched_indices entry on R (Sign 2: per-fire effectiveness).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b5_two_entries_same_reset_one_marker():
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
        requires_reset_block_id="R1",
    )
    eY = Block(
        id="EY", name="EY", input_id="Y", weight=100.0,
        conditions=(_gt("Y", 11.5),),
        requires_reset_block_id="R1",
    )
    xX = Block(id="XX", conditions=(_lt("X", 12.0),), target_entry_block_name="EX")
    xY = Block(id="XY", conditions=(_lt("Y", 12.0),), target_entry_block_name="EY")
    reset = Block(id="R1", conditions=(_eq("X", 11.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X, INPUT_Y),
        rules=SignalRules(
            entries=(eX, eY), exits=(xX, xY), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    # ONE marker, even though TWO blocks were armed.
    assert events_by[("R1", "reset")].latched_indices == (3,)


# ---------------------------------------------------------------------------
# B6 — Two entries bound to DIFFERENT resets R1, R2: each reset arms its
#      own entry only.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b6_two_entries_different_resets_independent_arms():
    INPUT_Y = Input(
        id="Y", instrument=InstrumentSpot(collection="INDEX", instrument_id="NDX"),
    )
    # X: open at t=1, close at t=3, refire at t=4.
    # Y: open at t=1, close at t=3, refire at t=4 — but ONLY if its R2
    # arms. R2 fires at t=3 (X==11). R1 fires at t=3 also (X==11).
    spx = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 14.0, 15.0, 16.0])
    ndx = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 14.0, 15.0, 16.0])
    fetcher = _make_fetcher(
        {("INDEX", "SPX"): (DATES, spx), ("INDEX", "NDX"): (DATES, ndx)}
    )
    eX = Block(
        id="EX", name="EX", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    eY = Block(
        id="EY", name="EY", input_id="Y", weight=100.0,
        conditions=(_gt("Y", 11.5),),
        requires_reset_block_id="R2",
    )
    xX = Block(id="XX", conditions=(_lt("X", 12.0),), target_entry_block_name="EX")
    xY = Block(id="XY", conditions=(_lt("Y", 12.0),), target_entry_block_name="EY")
    # R1 fires at t=3 (X==11); R2 never fires.
    r1 = Block(id="R1", conditions=(_eq("X", 11.0),))
    r2 = Block(id="R2", conditions=(_eq("X", 99.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X, INPUT_Y),
        rules=SignalRules(
            entries=(eX, eY), exits=(xX, xY), resets=(r1, r2),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    by_entry: dict[str, list] = {"EX": [], "EY": []}
    for tr in result.trades:
        by_entry[tr.entry_block_id].append(tr)
    # EX re-fires at t=4 because R1 armed it. EY only first cycle.
    assert len(by_entry["EX"]) == 2
    assert len(by_entry["EY"]) == 1


# ---------------------------------------------------------------------------
# B7 — Reset fires when ONE of N bound blocks needs arming → marker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b7_reset_arms_one_of_three_emits_marker():
    INPUT_Y = Input(
        id="Y", instrument=InstrumentSpot(collection="INDEX", instrument_id="NDX"),
    )
    INPUT_Z = Input(
        id="Z", instrument=InstrumentSpot(collection="INDEX", instrument_id="DJI"),
    )
    # Three entries all bound to R1. Engineered so only ONE (EX) actually
    # fires + disarms before R1 fires. EY and EZ never fire (their entry
    # conditions never become true). Therefore at R1's fire bar only EX
    # is disarmed; EY and EZ are still in the initial-armed state.
    # R1 fires → EX flips False→True; EY and EZ stay True→True. Marker
    # because ≥1 transition.
    spx = np.array([10.0, 12.0, 13.0, 11.0, 13.0, 14.0, 15.0, 16.0])
    ndx = np.array([5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    dji = np.array([5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    fetcher = _make_fetcher(
        {
            ("INDEX", "SPX"): (DATES, spx),
            ("INDEX", "NDX"): (DATES, ndx),
            ("INDEX", "DJI"): (DATES, dji),
        }
    )
    eX = Block(
        id="EX", name="EX", input_id="X", weight=50.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    eY = Block(
        id="EY", name="EY", input_id="Y", weight=50.0,
        conditions=(_gt("Y", 100.0),),
        requires_reset_block_id="R1",
    )
    eZ = Block(
        id="EZ", name="EZ", input_id="Z", weight=50.0,
        conditions=(_gt("Z", 100.0),),
        requires_reset_block_id="R1",
    )
    xX = Block(id="XX", conditions=(_lt("X", 12.0),), target_entry_block_name="EX")
    reset = Block(id="R1", conditions=(_eq("X", 11.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X, INPUT_Y, INPUT_Z),
        rules=SignalRules(
            entries=(eX, eY, eZ), exits=(xX,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    # R1 fires at t=3 with one transition (EX False→True) → marker.
    assert events_by[("R1", "reset")].latched_indices == (3,)


# ---------------------------------------------------------------------------
# B8 — Reset fires while ALL bound blocks already armed → NO marker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b8_reset_with_all_armed_emits_no_marker():
    # Single bound entry that never fires (condition never True). R1
    # fires at t=3. Bound entry's arm is still in initial-True state →
    # R1's fire produces no transitions → NO marker.
    closes = np.array([10.0, 9.0, 8.0, 11.0, 7.0, 6.0, 5.0, 4.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 100.0),),  # never fires
        requires_reset_block_id="R1",
    )
    reset = Block(id="R1", conditions=(_eq("X", 11.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), resets=(reset,)),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    r = events_by[("R1", "reset")]
    # R1 fired at t=3 but no transition → NO marker.
    assert 3 in r.fired_indices
    assert r.latched_indices == ()


# ---------------------------------------------------------------------------
# B9 — Bound entry: latched[entry.id]==True blocks re-latch even if arm
#      True. Position-state guard preserved INDEPENDENTLY of the arm
#      (Sign 3).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b9_bound_entry_no_double_latch_when_already_latched():
    # Entry fires at t=1 (X=12 > 11.5) and stays True at t=2,3. Reset
    # fires at t=2 (X=12) — would arm if it had been disarmed, but the
    # entry was disarmed by its t=1 fire. After reset, arm is True
    # again. But latched[E] is still True → can't double-latch.
    closes = np.array([10.0, 12.0, 12.0, 12.0, 9.0, 9.0, 9.0, 9.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.5),),
        requires_reset_block_id="R1",
    )
    reset = Block(id="R1", conditions=(_eq("X", 12.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(entries=(entry,), resets=(reset,)),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Only ONE open bar (t=1) — t=2 and t=3 don't double-latch.
    assert len(result.trades) == 1
    assert result.trades[0].open_bar == 1


# ---------------------------------------------------------------------------
# B10 — Bound exit: target_entry not latched → no fire, no arm
#       consumption. The arm stays True until a fire with a real target.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b10_bound_exit_target_not_latched_preserves_arm():
    # Entry: X > 100 (never fires) → latched[E] stays False.
    # Exit: X < 100 (always fires) bound to R1.
    # The exit condition is True throughout but target entry is never
    # open → no exit_latched, no arm consumption. The exit's arm stays
    # True. R1 fires at some bar → no transition needed → NO marker.
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 100.0),),
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 100.0),),
        target_entry_block_name="Entry",
        requires_reset_block_id="R1",
    )
    reset = Block(id="R1", conditions=(_eq("X", 12.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    # No exit_latched entries (no fires recorded as effective).
    assert events_by[("X1", "exit")].latched_indices == ()
    # Reset fires at t=2 but arm already True → no transition → NO marker.
    r = events_by[("R1", "reset")]
    assert 2 in r.fired_indices
    assert r.latched_indices == ()


# ---------------------------------------------------------------------------
# B11 — API: requires_reset_block_id="<unknown>" on entry → rejected.
# ---------------------------------------------------------------------------


def test_b11_api_rejects_binding_to_unknown_reset_id():
    from tcg.core.api.signals import SignalIn, parse_signal
    from tcg.engine.signal_exec import SignalValidationError

    spec = SignalIn.model_validate(
        {
            "id": "s",
            "name": "s",
            "inputs": [
                {
                    "id": "X",
                    "instrument": {
                        "type": "spot",
                        "collection": "INDEX",
                        "instrument_id": "SPX",
                    },
                }
            ],
            "rules": {
                "entries": [
                    {
                        "id": "E",
                        "name": "Entry",
                        "input_id": "X",
                        "weight": 100.0,
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {"kind": "instrument", "input_id": "X"},
                                "rhs": {"kind": "constant", "value": 1.0},
                            }
                        ],
                        "requires_reset_block_id": "DOES_NOT_EXIST",
                    }
                ],
                "exits": [],
                "resets": [
                    {
                        "id": "R1",
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {"kind": "instrument", "input_id": "X"},
                                "rhs": {"kind": "constant", "value": 0.0},
                            }
                        ],
                    }
                ],
            },
        }
    )
    with pytest.raises(SignalValidationError) as exc:
        parse_signal(spec)
    msg = str(exc.value)
    assert "requires_reset_block_id" in msg
    assert "'DOES_NOT_EXIST'" in msg
    assert "does not match any reset block id" in msg


# ---------------------------------------------------------------------------
# B12 — API: reset block carrying requires_reset_block_id → rejected.
# ---------------------------------------------------------------------------


def test_b12_api_rejects_reset_block_with_binding():
    from tcg.core.api.signals import SignalIn, parse_signal
    from tcg.engine.signal_exec import SignalValidationError

    spec = SignalIn.model_validate(
        {
            "id": "s",
            "name": "s",
            "inputs": [
                {
                    "id": "X",
                    "instrument": {
                        "type": "spot",
                        "collection": "INDEX",
                        "instrument_id": "SPX",
                    },
                }
            ],
            "rules": {
                "entries": [],
                "exits": [],
                "resets": [
                    {
                        "id": "R1",
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {"kind": "instrument", "input_id": "X"},
                                "rhs": {"kind": "constant", "value": 0.0},
                            }
                        ],
                        "requires_reset_block_id": "R1",
                    }
                ],
            },
        }
    )
    with pytest.raises(SignalValidationError) as exc:
        parse_signal(spec)
    assert "reset blocks must not set requires_reset_block_id" in str(exc.value)


# ---------------------------------------------------------------------------
# B13 — Bound entry to a DISABLED reset → fires once, arm never flips.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b13_binding_to_disabled_reset_locks_after_first_fire():
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, 11.0, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    # Reset would fire at t=5, but it's disabled → filtered out by
    # _usable_reset. bound_target still maps E -> "R1" but no usable
    # reset has that id, so block_arm[E] never flips True after the
    # first disarm.
    reset = Block(
        id="R1", conditions=(_eq("X", 11.0),), enabled=False,
    )
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    assert len(result.trades) == 1


# ---------------------------------------------------------------------------
# B14 — Reset NaN at fire bar → no fire, no arm transition.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b14_reset_nan_no_arm_transition():
    # The reset's operand has NaN at t=5. Entry would also fire there
    # but the reset's nan suppresses its arm flip.
    closes = np.array([10.0, 11.0, 13.0, 14.0, 15.0, np.nan, 13.0, 14.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 11.0),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 11.5),),
        target_entry_block_name="Entry",
    )
    # Reset condition would fire at any X bar, but t=5 has NaN.
    reset = Block(id="R1", conditions=(_gt("X", 0.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    events_by = {(ev.block_id, ev.kind): ev for ev in result.events}
    r = events_by[("R1", "reset")]
    # t=5 nan: not in fired or latched.
    assert 5 not in r.fired_indices
    assert 5 not in r.latched_indices


# ---------------------------------------------------------------------------
# B16 — NaN at a bound entry's own operand at bar t suppresses the fire
#       AND must NOT consume the arm (no transition occurred).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b16_nan_on_bound_entry_preserves_arm():
    # NaN at t=1 would mask the entry's condition truth. Without the NaN
    # guard, the engine could mis-consume the arm despite the entry never
    # actually firing. Reset is wired to NEVER fire so any latch beyond
    # the first proves the arm was preserved by an earlier (correct)
    # ineffective bar, not by re-arming.
    closes = np.array([10.0, np.nan, 14.0, 10.0, 14.0, 10.0, 10.0, 10.0])
    fetcher = _make_fetcher({("INDEX", "SPX"): (DATES, closes)})
    entry = Block(
        id="E", name="Entry", input_id="X", weight=100.0,
        conditions=(_gt("X", 12.0),),
        requires_reset_block_id="R1",
    )
    exit_blk = Block(
        id="X1",
        conditions=(_lt("X", 12.0),),
        target_entry_block_name="Entry",
    )
    # Reset cond never satisfied (X < 0 is impossible for these closes).
    reset = Block(id="R1", conditions=(_lt("X", 0.0),))
    signal = Signal(
        id="s", name="s", inputs=(INPUT_X,),
        rules=SignalRules(
            entries=(entry,), exits=(exit_blk,), resets=(reset,),
        ),
    )
    result = await evaluate_signal(signal, indicators={}, fetcher=fetcher)
    # Trace (NaN guard correct):
    #   t=0 X=10: exit no-op (not latched), entry no-op (10 < 12). arm=True.
    #   t=1 NaN: all truth masked False. arm UNCHANGED = True.
    #   t=2 X=14: entry latches (arm=True, latched=False). arm→False.
    #   t=3 X=10: exit fires; latched→False. arm stays False (no reset).
    #   t=4 X=14: entry SKIPPED (arm=False, reset never armed).
    # Expected: entry latched exactly once at t=2.
    entry_evt = next(ev for ev in result.events if ev.block_id == "E")
    assert entry_evt.latched_indices == (2,), (
        f"expected entry latched at (2,) — if NaN at t=1 consumed the arm, "
        f"entry would not latch at t=2 either; got {entry_evt.latched_indices}"
    )


# ---------------------------------------------------------------------------
# B15 — Placeholder block carrying requires_reset_block_id → accepted,
#       filtered by _usable_*.
# ---------------------------------------------------------------------------


def test_b15_placeholder_block_with_binding_accepted():
    from tcg.core.api.signals import SignalIn, parse_signal

    # A fully-empty placeholder block (no id, no conditions, no input)
    # may carry a requires_reset_block_id; parse should accept it (the
    # engine's _usable_entry filter will skip it). No SignalValidationError.
    spec = SignalIn.model_validate(
        {
            "id": "s",
            "name": "s",
            "inputs": [
                {
                    "id": "X",
                    "instrument": {
                        "type": "spot",
                        "collection": "INDEX",
                        "instrument_id": "SPX",
                    },
                }
            ],
            "rules": {
                "entries": [
                    # Placeholder: empty id, no conditions, no input.
                    {"requires_reset_block_id": "R1"},
                ],
                "exits": [],
                "resets": [
                    {
                        "id": "R1",
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {"kind": "instrument", "input_id": "X"},
                                "rhs": {"kind": "constant", "value": 0.0},
                            }
                        ],
                    }
                ],
            },
        }
    )
    signal = parse_signal(spec)
    # Placeholder accepted; binding is on the resulting Block.
    assert signal.rules.entries[0].requires_reset_block_id == "R1"
    assert signal.rules.entries[0].id == ""
