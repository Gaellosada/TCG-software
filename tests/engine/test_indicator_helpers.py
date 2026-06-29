"""Unit tests for ``tcg.engine.indicator_helpers``.

Hand-checked cases for every combinator, with explicit coverage of the
sharp edge: NaN / warmup propagation. The output contract is:

* 1-D float64, aligned to input length
* masks are floats in ``{0.0, 1.0, np.nan}``
* counts are floats (with ``np.nan`` in warmup)
* an index that is undefined (NaN in input, or in warmup) MUST be ``np.nan``
  in the output, NEVER a silent ``0.0`` that the downstream Compare would
  read as a real (false) event.
"""

from __future__ import annotations

import numpy as np

from tcg.engine import indicator_helpers as ta


NAN = np.nan


def arr(*xs: float) -> np.ndarray:
    return np.asarray(xs, dtype=np.float64)


def assert_mask(got: np.ndarray, expected: list[float]) -> None:
    """Compare a float mask/count array elementwise, NaN-aware."""
    assert got.dtype == np.float64, got.dtype
    assert got.shape == (len(expected),), (got.shape, len(expected))
    exp = np.asarray(expected, dtype=np.float64)
    np.testing.assert_array_equal(
        np.isnan(got), np.isnan(exp), err_msg=f"NaN mask mismatch: {got} vs {exp}"
    )
    finite = ~np.isnan(exp)
    np.testing.assert_array_equal(got[finite], exp[finite])


# --------------------------------------------------------------------------
# crossed_up / crossed_down
# --------------------------------------------------------------------------


def test_crossed_up_basic() -> None:
    # level = 3. cross up when prev < 3 <= now.
    s = arr(1, 2, 4, 5, 2, 3)
    # bar0: undefined (no prev) -> NaN
    # bar1: 1->2, no
    # bar2: 2->4, crosses up -> 1
    # bar3: 4->5, both above, no
    # bar4: 5->2, no (down)
    # bar5: 2->3, prev<3<=3 -> 1
    assert_mask(ta.crossed_up(s, 3.0), [NAN, 0, 1, 0, 0, 1])


def test_crossed_down_basic() -> None:
    s = arr(5, 4, 2, 1, 4, 3)
    # level=3, cross down when prev > 3 >= now (prev>=level? use prev>level>=now)
    # bar0 NaN
    # bar1: 5->4, both above, no
    # bar2: 4->2, crosses down -> 1
    # bar3: 2->1, both below, no
    # bar4: 1->4, up, no
    # bar5: 4->3, prev>3>=3 -> 1
    assert_mask(ta.crossed_down(s, 3.0), [NAN, 0, 1, 0, 0, 1])


def test_crossed_up_nan_input_propagates() -> None:
    s = arr(1, NAN, 4, 5)
    # bar0 NaN (warmup)
    # bar1 NaN (input NaN)
    # bar2: prev is NaN -> undefined -> NaN
    # bar3: 4->5 both above -> 0
    assert_mask(ta.crossed_up(s, 3.0), [NAN, NAN, NAN, 0])


def test_crossed_no_event_when_touching_level_from_above() -> None:
    # Equality semantics: cross_up requires prev STRICTLY below level.
    s = arr(3, 3, 4)
    # bar1: 3->3, prev not < level -> 0
    # bar2: 3->4, prev not < level (==) -> 0
    assert_mask(ta.crossed_up(s, 3.0), [NAN, 0, 0])


def test_crossed_up_down_empty_input() -> None:
    # Empty series degrades gracefully to an empty float64 array (no IndexError),
    # matching the n==0 contract of the other 5 helpers.
    empty = arr()
    up = ta.crossed_up(empty, 3.0)
    down = ta.crossed_down(empty, 3.0)
    assert up.dtype == np.float64 and up.shape == (0,)
    assert down.dtype == np.float64 and down.shape == (0,)


# --------------------------------------------------------------------------
# bars_since
# --------------------------------------------------------------------------


def test_bars_since_basic() -> None:
    m = arr(0, 1, 0, 0, 1, 0)
    # before first event -> NaN; at event -> 0; then counts up
    assert_mask(ta.bars_since(m), [NAN, 0, 1, 2, 0, 1])


def test_bars_since_event_at_zero() -> None:
    m = arr(1, 0, 0)
    assert_mask(ta.bars_since(m), [0, 1, 2])


def test_bars_since_nan_in_mask_propagates() -> None:
    # A NaN in the mask means "event status unknown at that bar"; the count
    # is undefined there AND for all subsequent bars until the next known
    # event, because we cannot know how long ago the last event was.
    m = arr(0, 1, NAN, 0, 1, 0)
    # bar0: no event yet -> NaN
    # bar1: event -> 0
    # bar2: NaN -> undefined -> NaN
    # bar3: unknown whether bar2 was an event -> NaN
    # bar4: known event -> 0 (resets ambiguity)
    # bar5: 1
    assert_mask(ta.bars_since(m), [NAN, 0, NAN, NAN, 0, 1])


# --------------------------------------------------------------------------
# count_in_window
# --------------------------------------------------------------------------


def test_count_in_window_basic() -> None:
    m = arr(0, 1, 0, 1, 1, 0)
    # window=3, inclusive trailing. Undefined until 3 bars available.
    # bar0,bar1: warmup (fewer than window bars) -> NaN
    # bar2: window [b0,b1,b2] = 0+1+0 = 1
    # bar3: [b1,b2,b3] = 1+0+1 = 2
    # bar4: [b2,b3,b4] = 0+1+1 = 2
    # bar5: [b3,b4,b5] = 1+1+0 = 2
    assert_mask(ta.count_in_window(m, 3), [NAN, NAN, 1, 2, 2, 2])


def test_count_in_window_nan_propagates() -> None:
    m = arr(0, 1, NAN, 1, 1, 0)
    # window=3. Any NaN inside the trailing window -> count undefined -> NaN.
    # bar0,bar1: warmup -> NaN
    # bar2: window contains NaN -> NaN
    # bar3: [b1,b2,b3] contains NaN -> NaN
    # bar4: [b2,b3,b4] contains NaN -> NaN
    # bar5: [b3,b4,b5] = 1+1+0 = 2
    assert_mask(ta.count_in_window(m, 3), [NAN, NAN, NAN, NAN, NAN, 2])


def test_count_in_window_window_one() -> None:
    m = arr(1, 0, NAN, 1)
    assert_mask(ta.count_in_window(m, 1), [1, 0, NAN, 1])


# --------------------------------------------------------------------------
# sequence_within
# --------------------------------------------------------------------------


def test_sequence_within_two_stages_fires() -> None:
    # stage0 starts the sequence, stage1 completes it; fire when stage1
    # happens within `window` bars of the stage0 start.
    stage0 = arr(0, 1, 0, 0, 0, 0)
    stage1 = arr(0, 0, 0, 1, 0, 0)
    # stage0 at bar1, stage1 at bar3 -> gap = 3-1 = 2 <= window(3) -> fire at bar3
    assert_mask(ta.sequence_within([stage0, stage1], 3), [0, 0, 0, 1, 0, 0])


def test_sequence_within_too_late_no_fire() -> None:
    stage0 = arr(0, 1, 0, 0, 0, 0)
    stage1 = arr(0, 0, 0, 0, 0, 1)
    # stage0 at bar1, stage1 at bar5 -> gap = 4 > window(3) -> no fire
    assert_mask(ta.sequence_within([stage0, stage1], 3), [0, 0, 0, 0, 0, 0])


def test_sequence_within_three_stages_ordered() -> None:
    s0 = arr(1, 0, 0, 0, 0)
    s1 = arr(0, 1, 0, 0, 0)
    s2 = arr(0, 0, 1, 0, 0)
    # s0@0, s1@1, s2@2; final within window(4) of start@0 -> fire at bar2
    assert_mask(ta.sequence_within([s0, s1, s2], 4), [0, 0, 1, 0, 0])


def test_sequence_within_abort_resets() -> None:
    s0 = arr(0, 1, 0, 0, 0, 0)
    s1 = arr(0, 0, 0, 0, 1, 0)
    abort = arr(0, 0, 1, 0, 0, 0)
    # s0@1 starts; abort@2 resets the in-progress sequence; s1@4 has no
    # active start -> no fire.
    assert_mask(ta.sequence_within([s0, s1], 4, abort=abort), [0, 0, 0, 0, 0, 0])


def test_sequence_within_nan_in_stage_propagates() -> None:
    # If the awaited stage mask is NaN at a bar, the completion status there
    # is undefined -> output is NaN AND the in-progress candidate is
    # invalidated (we cannot trust the ordering through an unknown bar).
    # This is the conservative rule: never fire after an unknown gap.
    s0 = arr(0, 1, 0, 0)
    s1 = arr(0, 0, NAN, 1)
    # bar1: s0 starts the candidate (awaiting s1)
    # bar2: awaited s1 is NaN -> NaN, candidate killed
    # bar3: s1 fires but no active candidate -> 0 (NOT a spurious fire)
    out = ta.sequence_within([s0, s1], 3)
    assert_mask(out, [0, 0, NAN, 0])


# --------------------------------------------------------------------------
# nth_event
# --------------------------------------------------------------------------


def test_nth_event_basic() -> None:
    m = arr(1, 0, 1, 0, 1, 0)
    # n=2, window=10 (no reset): fire at the bar of the 2nd event = bar2
    assert_mask(ta.nth_event(m, 2, 10), [0, 0, 1, 0, 0, 0])


def test_nth_event_window_limits_count() -> None:
    m = arr(1, 0, 0, 0, 1, 1)
    # n=2, window=3 (trailing inclusive). At each event count events within
    # the trailing window; fire when the count hits exactly n.
    # bar0: event, count in [b0..b0]? window=3 -> events in trailing 3 = 1
    # bar4: events in [b2,b3,b4] = 1 (only b4) -> count 1
    # bar5: events in [b3,b4,b5] = 2 -> hits n=2 -> fire at bar5
    assert_mask(ta.nth_event(m, 2, 3), [0, 0, 0, 0, 0, 1])


def test_nth_event_reset() -> None:
    m = arr(1, 1, 0, 1, 1, 0)
    reset = arr(0, 0, 1, 0, 0, 0)
    # n=2, big window. reset@2 clears the running count.
    # b0: 1st event (count 1)
    # b1: 2nd event -> fire
    # b2: reset
    # b3: 1st event after reset (count 1)
    # b4: 2nd event after reset -> fire
    assert_mask(ta.nth_event(m, 2, 100, reset=reset), [0, 1, 0, 0, 1, 0])


def test_nth_event_nan_propagates() -> None:
    m = arr(1, NAN, 1, 1)
    # n=2, window=100. bar1 NaN -> event status unknown -> the running count
    # is ambiguous from there on within the window -> NaN at bar1 and any
    # later bar whose window still contains the NaN.
    out = ta.nth_event(m, 2, 100)
    assert np.isnan(out[1])


# --------------------------------------------------------------------------
# regime_gate
# --------------------------------------------------------------------------


def test_regime_gate_basic() -> None:
    m = arr(1, 1, 0, 1)
    regime = arr(1, 0, 1, 1)
    # event AND regime_ok
    assert_mask(ta.regime_gate(m, regime), [1, 0, 0, 1])


def test_regime_gate_nan_propagates() -> None:
    m = arr(1, 1, NAN, 1)
    regime = arr(1, NAN, 1, 0)
    # bar0: 1 & 1 -> 1
    # bar1: regime NaN -> undefined -> NaN
    # bar2: mask NaN -> NaN
    # bar3: 1 & 0 -> 0
    assert_mask(ta.regime_gate(m, regime), [1, NAN, NAN, 0])
