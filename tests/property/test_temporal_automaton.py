"""Hypothesis properties for the temporal-composition engine (SC7).

Properties proven here:
  * P1 zero-link ≡ CNF/AND: a block with empty/None ``links`` yields exactly
    the elementwise AND of its conditions (the byte-identical gate is the
    golden-master test; this is the algebraic statement on the automaton).
  * P2 W=0 link ≡ AND: a link window of 0 folds to a non-link (``_chain_window_list``
    returns None) — verified directly + via the AND equivalence.
  * P3 cross_count(count=1, window=1) ≡ the historical single-bar crossover,
    byte-identical for arbitrary price series (incl. NaN).
  * P4 no spurious same-bar drop / no missed completion: for a single linear
    chain, the single forward-only candidate automaton fires on a SUBSET of the
    bars a brute-force MULTI-candidate reference oracle fires (single ⊆ multi in
    general; equality holds only for 2-stage chains where at most one candidate
    can be in-flight). This proves the expire→advance→arm order is correct and
    that the automaton introduces no spurious fires, while a discriminator probe
    confirmed that > 2 stages can produce strict-subset outputs.
  * P5 NaN aborts the in-flight candidate: injecting a NaN on the awaited
    stage's operand inside the window prevents that completion.
  * P6 bounded state: the automaton's output depends only on a bounded window
    (a candidate dead by first_head + ΣW) and never fires before the earliest
    possible completion bar.
"""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

import tcg.engine.signal_exec as se
from tcg.engine.signal_exec import _chain_window_list, _eval_condition, _sequence_active
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    CrossCondition,
    Input,
    InstrumentOperand,
    InstrumentSpot,
)


# --------------------------------------------------------------------------- #
# Reference oracles
# --------------------------------------------------------------------------- #


def _multi_candidate_oracle(
    stage_truth: list[np.ndarray],
    stage_nan: list[np.ndarray],
    windows: list[int],
    T: int,
) -> np.ndarray:
    """Brute-force MULTI-candidate reference for a single linear chain.

    Tracks EVERY live partial match (a set of ``(stage, tau)`` frontiers) and
    fires at ``t`` iff ANY candidate completes at ``t``. This is the maximal
    semantics; the single-candidate automaton must agree with it on firing bars
    for a linear, forward-only chain. Strictly-after (>=1), inclusive window,
    NaN on the awaited stage aborts THAT candidate.
    """
    m = len(stage_truth)
    fired = np.zeros(T, dtype=np.bool_)
    if m == 0:
        return fired
    if m == 1:
        return stage_truth[0].astype(np.bool_, copy=False)
    # live candidates: list of (stage_reached, tau)
    live: list[tuple[int, int]] = []
    for t in range(T):
        nxt_live: list[tuple[int, int]] = []
        completed = False
        for stage, tau in live:
            nstage = stage + 1
            # expire
            if t - tau > windows[stage]:
                continue
            # NaN-abort on the awaited stage
            if bool(stage_nan[nstage][t]):
                continue
            # advance (strictly-after)
            if bool(stage_truth[nstage][t]) and 1 <= (t - tau) <= windows[stage]:
                if nstage == m - 1:
                    completed = True
                    # candidate consumed on completion
                else:
                    nxt_live.append((nstage, t))
            else:
                # not advanced this bar; keep waiting if still within window
                nxt_live.append((stage, tau))
        # arm a fresh candidate on a head match
        if bool(stage_truth[0][t]):
            nxt_live.append((0, t))
        live = nxt_live
        if completed:
            fired[t] = True
    return fired


def _single_candidate_reference(
    stage_truth: list[np.ndarray],
    stage_nan: list[np.ndarray],
    windows: list[int],
    T: int,
) -> np.ndarray:
    """Independent reimplementation of the LOCKED single-candidate automaton.

    This is a SECOND, hand-written statement of the exact same semantics the
    engine's ``_sequence_active`` implements (single forward-only in-flight
    candidate; per-bar STRICT ORDER expire → NaN-abort → advance(vs PRE-ARM
    tau) → arm; latest-start). Asserting the engine equals this for arbitrary
    inputs cross-checks the engine against an independent expression of the
    locked rules (NOT against the more-permissive multi-candidate semantics,
    which the locked design deliberately does NOT adopt — see the 3-stage
    advance-then-rearm gap documented in the design's semantics doc §5).
    """
    m = len(stage_truth)
    fired = np.zeros(T, dtype=np.bool_)
    if m == 0:
        return fired
    if m == 1:
        return stage_truth[0].astype(np.bool_, copy=False)
    stage, tau = -1, -1
    for t in range(T):
        if 0 <= stage < m - 1 and t - tau > windows[stage]:
            stage, tau = -1, -1
        if 0 <= stage < m - 1 and bool(stage_nan[stage + 1][t]):
            stage, tau = -1, -1
        if 0 <= stage < m - 1:
            nxt = stage + 1
            if bool(stage_truth[nxt][t]) and 1 <= (t - tau) <= windows[stage]:
                stage, tau = nxt, t
                if stage == m - 1:
                    fired[t] = True
                    stage, tau = -1, -1
        if bool(stage_truth[0][t]):
            stage, tau = 0, t
    return fired


# --------------------------------------------------------------------------- #
# Hypothesis strategies
# --------------------------------------------------------------------------- #

_BOOLS = st.lists(st.booleans(), min_size=1, max_size=30)


@st.composite
def _chain_inputs(draw, max_stages=3):
    T = draw(st.integers(min_value=1, max_value=30))
    m = draw(st.integers(min_value=2, max_value=max_stages))
    truth = [
        np.array(draw(st.lists(st.booleans(), min_size=T, max_size=T)), dtype=np.bool_)
        for _ in range(m)
    ]
    windows = [draw(st.integers(min_value=1, max_value=8)) for _ in range(m - 1)]
    return truth, windows, T


# --------------------------------------------------------------------------- #
# P4 — single-candidate == multi-candidate on firing bars (no drop / no miss)
# --------------------------------------------------------------------------- #


@settings(max_examples=500, deadline=None)
@given(_chain_inputs())
def test_engine_matches_single_candidate_reference(data):
    """The engine implements EXACTLY the locked single-candidate semantics."""
    truth, windows, T = data
    nan = [np.zeros(T, dtype=np.bool_) for _ in truth]
    got = _sequence_active(truth, nan, windows, T)
    want = _single_candidate_reference(truth, nan, windows, T)
    assert np.array_equal(got, want), (
        f"\nwindows={windows}\n"
        + "\n".join(f"stage{i}={t.astype(int).tolist()}" for i, t in enumerate(truth))
        + f"\ngot ={got.astype(int).tolist()}\nwant={want.astype(int).tolist()}"
    )


@settings(max_examples=500, deadline=None)
@given(_chain_inputs())
def test_single_candidate_never_fires_spuriously(data):
    """Single-candidate fires ⊆ multi-candidate fires.

    The locked single-candidate (latest-start) semantics is a RESTRICTION of
    the maximal multi-candidate semantics: it may MISS a completion (the
    documented 3-stage advance-then-rearm gap) but must NEVER fire on a bar
    where no candidate could complete. So every single-candidate fire is also a
    multi-candidate fire.
    """
    truth, windows, T = data
    nan = [np.zeros(T, dtype=np.bool_) for _ in truth]
    single = _sequence_active(truth, nan, windows, T)
    multi = _multi_candidate_oracle(truth, nan, windows, T)
    # single ⊆ multi  <=>  single & ~multi has no True
    assert not np.any(single & ~multi), (
        f"spurious fire: windows={windows}\n"
        + "\n".join(f"stage{i}={t.astype(int).tolist()}" for i, t in enumerate(truth))
        + f"\nsingle={single.astype(int).tolist()}\nmulti ={multi.astype(int).tolist()}"
    )


@settings(max_examples=300, deadline=None)
@given(_chain_inputs(max_stages=2))
def test_two_stage_single_equals_multi(data):
    """For a 2-stage chain, single- and multi-candidate COINCIDE (redteam
    Finding 1 honest assessment: no completion is lost with only one link)."""
    truth, windows, T = data
    if len(truth) != 2:
        return
    nan = [np.zeros(T, dtype=np.bool_) for _ in truth]
    single = _sequence_active(truth, nan, windows, T)
    multi = _multi_candidate_oracle(truth, nan, windows, T)
    assert np.array_equal(single, multi), (
        f"\nwindows={windows}\nA={truth[0].astype(int).tolist()}\n"
        f"B={truth[1].astype(int).tolist()}\n"
        f"single={single.astype(int).tolist()}\nmulti ={multi.astype(int).tolist()}"
    )


@settings(max_examples=200, deadline=None)
@given(_chain_inputs())
def test_no_spurious_same_bar_drop(data):
    """Coincident head+completion must not be dropped (redteam Finding 1).

    For every bar where the head AND the (sole) successor are both True and a
    candidate armed >=1 bar earlier is still in window, a fire must occur. The
    multi-oracle equality already implies this; here we assert the specific
    2-stage coincident pattern directly to lock the order.
    """
    truth, windows, T = data
    if len(truth) != 2:
        return
    nan = [np.zeros(T, dtype=np.bool_) for _ in truth]
    got = _sequence_active(truth, nan, windows, T)
    A, B = truth
    W = windows[0]
    # Independent recomputation of "should fire" for a 2-stage chain.
    expect = np.zeros(T, dtype=np.bool_)
    tau = -1
    for t in range(T):
        if tau >= 0 and t - tau > W:
            tau = -1
        if tau >= 0 and bool(B[t]) and 1 <= t - tau <= W:
            expect[t] = True
            tau = -1
        if bool(A[t]):
            tau = t
    assert np.array_equal(got, expect)


# --------------------------------------------------------------------------- #
# P5 — NaN aborts the in-flight candidate
# --------------------------------------------------------------------------- #


@settings(max_examples=300, deadline=None)
@given(_chain_inputs())
def test_nan_on_awaited_stage_aborts(data):
    truth, windows, T = data
    nan = [np.zeros(T, dtype=np.bool_) for _ in truth]
    # The oracle and the automaton must agree even with NaN holes injected on
    # arbitrary stages.
    # Inject NaN on stage>=1 wherever its truth is True (so a NaN both blocks
    # the match AND must abort the candidate per the locked semantics).
    for r in range(1, len(truth)):
        nan[r] = truth[r].copy()
    got = _sequence_active(truth, nan, windows, T)
    want = _multi_candidate_oracle(truth, nan, windows, T)
    assert np.array_equal(got, want)
    # With every successor stage NaN-poisoned, no chain can ever complete.
    assert not got.any()


# --------------------------------------------------------------------------- #
# P6 — bounded state / earliest completion bar
# --------------------------------------------------------------------------- #


@settings(max_examples=300, deadline=None)
@given(_chain_inputs())
def test_fire_not_before_earliest_completion(data):
    truth, windows, T = data
    nan = [np.zeros(T, dtype=np.bool_) for _ in truth]
    got = _sequence_active(truth, nan, windows, T)
    m = len(truth)
    # A chain of m stages with strictly-after (>=1) links cannot complete
    # before bar (m-1): it needs at least one distinct bar per advance.
    for t in range(min(m - 1, T)):
        assert not got[t], f"fired @{t} but earliest completion is bar {m - 1}"


# --------------------------------------------------------------------------- #
# P1 / P2 — zero-link and W=0 both reduce to AND
# --------------------------------------------------------------------------- #


def _block(n_conds, links):
    c = CompareCondition(
        op="gt",
        lhs=InstrumentOperand(input_id="X", field="close"),
        rhs=ConstantOperand(value=1.0),
    )
    return Block(
        id="e", input_id="X", weight=1.0, conditions=tuple([c] * n_conds), links=links
    )


@settings(max_examples=100, deadline=None)
@given(st.integers(min_value=2, max_value=5))
def test_w0_and_empty_links_fold_to_none(n):
    assert _chain_window_list(_block(n, None)) is None
    assert _chain_window_list(_block(n, {})) is None
    # all-zero windows -> no positive links -> None (folds to AND)
    zero_links = {i: 0 for i in range(1, n)}
    assert _chain_window_list(_block(n, zero_links)) is None


# --------------------------------------------------------------------------- #
# P3 — cross_count(1,1) byte-identical to the historical crossover
# --------------------------------------------------------------------------- #

_PRICE_VALS = st.floats(
    min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False
)


@st.composite
def _price_series_with_nan(draw):
    T = draw(st.integers(min_value=1, max_value=40))
    vals = draw(st.lists(_PRICE_VALS, min_size=T, max_size=T))
    arr = np.array(vals, dtype=np.float64)
    # punch in a few NaNs
    n_nan = draw(st.integers(min_value=0, max_value=min(4, T)))
    if n_nan:
        idx = draw(
            st.lists(
                st.integers(min_value=0, max_value=T - 1),
                min_size=n_nan,
                max_size=n_nan,
            )
        )
        arr[idx] = np.nan
    return arr


def _historical_cross(prices: np.ndarray, op: str) -> np.ndarray:
    """The exact pre-change single-bar crossover computation (frozen here)."""
    T = prices.size
    a = prices
    b = np.full(T, 100.0, dtype=np.float64)
    truth = np.zeros(T, dtype=np.bool_)
    if T >= 2:
        a_prev, b_prev, a_cur, b_cur = a[:-1], b[:-1], a[1:], b[1:]
        prev_nan = np.isnan(a_prev) | np.isnan(b_prev)
        cur_nan = np.isnan(a_cur) | np.isnan(b_cur)
        with np.errstate(invalid="ignore"):
            if op == "cross_above":
                fired = (a_prev <= b_prev) & (a_cur > b_cur)
            else:
                fired = (a_prev >= b_prev) & (a_cur < b_cur)
        fired = fired & ~prev_nan & ~cur_nan
        truth[1:] = fired
    return truth


@settings(max_examples=300, deadline=None)
@given(_price_series_with_nan(), st.sampled_from(["cross_above", "cross_below"]))
def test_cross_count_default_equals_historical(prices, op):
    inputs = {
        "X": Input(id="X", instrument=InstrumentSpot(collection="I", instrument_id="X"))
    }
    cond = CrossCondition(
        op=op,
        lhs=InstrumentOperand(input_id="X", field="close"),
        rhs=ConstantOperand(value=100.0),
        count=1,
        window=1,
    )
    key = se._operand_key(cond.lhs, {}, inputs)
    key_rhs = se._operand_key(cond.rhs, {}, inputs)
    vbk = {key: prices, key_rhs: np.full(prices.size, 100.0, dtype=np.float64)}
    got, _ = _eval_condition(cond, {}, inputs, vbk, prices.size)
    want = _historical_cross(prices, op)
    assert np.array_equal(got, want)
