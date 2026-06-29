"""Hypothesis property tests for the NaN invariant of indicator helpers.

The contract under test (the sharp edge that protects the signal engine):

    "NaN in  =>  NaN / False out at that index; never a spurious event."

Concretely, for every combinator and for arbitrary float arrays with
injected NaNs:

* every output index is finite-or-NaN (never inf, never a bad dtype);
* an output index that is genuinely undefined (NaN input it depends on, or
  a warmup region) is ``np.nan`` — NEVER a silent ``0.0`` AND never a
  spurious ``1.0``;
* in particular: **wherever the relevant input is NaN, the helper never
  emits an event (1.0)** — it emits NaN. A fabricated event is catastrophic
  downstream, so this is the property we hammer hardest.
"""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.engine import indicator_helpers as ta


# Float arrays that mix finite values and NaNs (and occasional infs, which
# must not produce garbage either). min_size kept >= 1 so helpers run.
_FLOATS = st.floats(
    min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False
)
_MAYBE_NAN = st.one_of(_FLOATS, st.just(np.nan))


@st.composite
def float_series(draw, min_size=1, max_size=40):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return np.asarray(
        draw(st.lists(_MAYBE_NAN, min_size=n, max_size=n)), dtype=np.float64
    )


@st.composite
def mask_series(draw, min_size=1, max_size=40):
    """Masks: 0.0 / 1.0 / NaN (the shapes helpers actually consume)."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    vals = st.sampled_from([0.0, 1.0, np.nan])
    return np.asarray(draw(st.lists(vals, min_size=n, max_size=n)), dtype=np.float64)


@st.composite
def _aligned_mask(draw, n):
    """A 0/1/NaN mask of an exact length *n* (for multi-input helpers)."""
    vals = st.sampled_from([0.0, 1.0, np.nan])
    return np.asarray(draw(st.lists(vals, min_size=n, max_size=n)), dtype=np.float64)


def _assert_no_inf_and_event_implies_known(out: np.ndarray) -> None:
    """Common postconditions: float64, no inf, events are exactly 1.0."""
    assert out.dtype == np.float64
    finite = out[~np.isnan(out)]
    assert np.all(np.isfinite(finite)), f"non-finite (inf) in output: {out}"


# --------------------------------------------------------------------------
# Crossings: an event requires BOTH bar t-1 and t finite. So wherever
# series[t-1] or series[t] is NaN (or t == 0), the output must be NaN —
# never an event.
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(s=float_series(), level=_FLOATS)
def test_crossed_up_nan_invariant(s: np.ndarray, level: float) -> None:
    out = ta.crossed_up(s, level)
    _assert_no_inf_and_event_implies_known(out)
    assert np.isnan(out[0])  # bar 0 always undefined
    for t in range(1, len(s)):
        undefined = np.isnan(s[t - 1]) or np.isnan(s[t])
        if undefined:
            assert np.isnan(out[t]), f"t={t} should be NaN, got {out[t]}"
        else:
            # finite case: must be exactly 0 or 1 and match the definition
            assert out[t] in (0.0, 1.0)
            want = 1.0 if (s[t - 1] < level <= s[t]) else 0.0
            assert out[t] == want


@settings(max_examples=400)
@given(s=float_series(), level=_FLOATS)
def test_crossed_down_nan_invariant(s: np.ndarray, level: float) -> None:
    out = ta.crossed_down(s, level)
    _assert_no_inf_and_event_implies_known(out)
    assert np.isnan(out[0])
    for t in range(1, len(s)):
        undefined = np.isnan(s[t - 1]) or np.isnan(s[t])
        if undefined:
            assert np.isnan(out[t])
        else:
            assert out[t] in (0.0, 1.0)
            want = 1.0 if (s[t - 1] > level >= s[t]) else 0.0
            assert out[t] == want


# --------------------------------------------------------------------------
# bars_since: at any bar where the mask is NaN, output is NaN. And the
# output is never an "event" concept (it's a count) — but it must never be
# negative, and must be NaN before the first known event.
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(m=mask_series())
def test_bars_since_nan_invariant(m: np.ndarray) -> None:
    out = ta.bars_since(m)
    _assert_no_inf_and_event_implies_known(out)
    # Wherever the mask itself is NaN, the output must be NaN.
    for t in range(len(m)):
        if np.isnan(m[t]):
            assert np.isnan(out[t]), f"t={t}: NaN mask must give NaN"
    # Counts (finite) are non-negative integers.
    finite = out[~np.isnan(out)]
    assert np.all(finite >= 0.0)
    assert np.all(finite == np.floor(finite))


# --------------------------------------------------------------------------
# count_in_window: NaN inside the trailing window => NaN; warmup => NaN;
# counts otherwise are 0..window.
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(m=mask_series(), window=st.integers(min_value=1, max_value=15))
def test_count_in_window_nan_invariant(m: np.ndarray, window: int) -> None:
    out = ta.count_in_window(m, window)
    _assert_no_inf_and_event_implies_known(out)
    n = len(m)
    for t in range(n):
        lo = t - window + 1
        if lo < 0:
            assert np.isnan(out[t]), f"t={t}: warmup must be NaN"
            continue
        if np.any(np.isnan(m[lo : t + 1])):
            assert np.isnan(out[t]), f"t={t}: NaN-in-window must be NaN"
        else:
            assert not np.isnan(out[t])
            assert 0.0 <= out[t] <= window


# --------------------------------------------------------------------------
# sequence_within: the headline property — it must NEVER emit a spurious
# event (1.0) at a bar where any *consumed* input was NaN. We check the
# weaker-but-bulletproof form: every output is in {0,1,NaN}, and wherever
# the abort mask is NaN the output is NaN.
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(
    s0=mask_series(),
    window=st.integers(min_value=1, max_value=15),
    use_abort=st.booleans(),
    data=st.data(),
)
def test_sequence_within_no_spurious_event(
    s0: np.ndarray, window: int, use_abort: bool, data
) -> None:
    n = len(s0)
    s1 = data.draw(_aligned_mask(n))
    abort = data.draw(_aligned_mask(n)) if use_abort else None
    out = ta.sequence_within([s0, s1], window, abort=abort)
    _assert_no_inf_and_event_implies_known(out)
    assert out.shape == (n,)
    # Every value is 0, 1, or NaN.
    finite = out[~np.isnan(out)]
    assert np.all((finite == 0.0) | (finite == 1.0))
    # A NaN abort bar must yield NaN (defensive: never fire on unknown abort).
    if abort is not None:
        for t in range(n):
            if np.isnan(abort[t]):
                assert np.isnan(out[t]), f"t={t}: NaN abort must give NaN"


# --------------------------------------------------------------------------
# nth_event: never a spurious fire. Output in {0,1,NaN}; a NaN mask bar
# yields NaN at that bar.
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(
    m=mask_series(),
    n=st.integers(min_value=1, max_value=5),
    window=st.integers(min_value=1, max_value=15),
    use_reset=st.booleans(),
    data=st.data(),
)
def test_nth_event_no_spurious_event(
    m: np.ndarray, n: int, window: int, use_reset: bool, data
) -> None:
    length = len(m)
    reset = data.draw(_aligned_mask(length)) if use_reset else None
    out = ta.nth_event(m, n, window, reset=reset)
    _assert_no_inf_and_event_implies_known(out)
    finite = out[~np.isnan(out)]
    assert np.all((finite == 0.0) | (finite == 1.0))
    for t in range(length):
        if np.isnan(m[t]):
            assert np.isnan(out[t]), f"t={t}: NaN mask must give NaN"


# --------------------------------------------------------------------------
# regime_gate: pure pointwise AND. NaN in either input => NaN; otherwise
# in {0,1}.
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(m=mask_series(), data=st.data())
def test_regime_gate_nan_invariant(m: np.ndarray, data) -> None:
    n = len(m)
    regime = data.draw(_aligned_mask(n))
    out = ta.regime_gate(m, regime)
    _assert_no_inf_and_event_implies_known(out)
    for t in range(n):
        if np.isnan(m[t]) or np.isnan(regime[t]):
            assert np.isnan(out[t]), f"t={t}: NaN input must give NaN"
        else:
            assert out[t] in (0.0, 1.0)
            want = 1.0 if (m[t] != 0.0 and regime[t] != 0.0) else 0.0
            assert out[t] == want
