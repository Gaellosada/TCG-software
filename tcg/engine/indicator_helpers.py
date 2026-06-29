"""Path-dependence combinators for user indicator code (the ``ta`` facade).

Pure engine module. Depends ONLY on numpy + stdlib — NOT on ``tcg.data``
or ``tcg.core`` (it may import ``tcg.types`` but needs nothing from it).

These helpers give indicator authors a small, orthogonal vocabulary for
*stateful* (path-dependent) constructs — crossings, "bars since", rolling
event counts, ordered multi-stage sequences — without writing per-bar
Python loops in the sandbox and without any statefulness leaking into the
signal engine. Each is single-pass O(T) and vectorised where possible.

Output contract (CRITICAL — the sharp edge)
--------------------------------------------
Every helper returns a **1-D float64 array aligned to the input length**.

* Event/boolean results are encoded as floats in ``{0.0, 1.0, np.nan}``
  (Python ``bool``/numpy ``bool_`` cannot hold NaN, so we must use float).
* Count results are floats, with ``np.nan`` in warmup.
* **NaN propagation is mandatory.** Any output index that is *undefined* —
  because an input it depends on is NaN, or because it falls in a warmup
  region (e.g. bar 0 of a cross has no predecessor; the first ``window``
  bars of a rolling count have no full window) — MUST be ``np.nan``, never
  a silent ``0.0``.

Why this matters: the downstream signal-engine Compare poisons NaN to
False (``signal_exec.py``: ``truth = truth & ~nan_at_t``). So a spurious
``0.0`` where the answer is genuinely unknown reads as a real (if false)
event, and a spurious ``1.0`` is catastrophic (a fabricated entry/exit).
The conservative rule is therefore: **when unknown, NaN — never a false
event.** A NaN at index *t* in any consumed mask propagates to NaN at *t*
(and, for the cumulative helpers, forward until a known event re-anchors
the state, because the running answer is genuinely ambiguous in between).
"""

from __future__ import annotations

from collections import deque

import numpy as np
import numpy.typing as npt

__all__ = [
    "crossed_up",
    "crossed_down",
    "bars_since",
    "count_in_window",
    "sequence_within",
    "nth_event",
    "regime_gate",
]


def _as_1d_float(name: str, value: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Coerce *value* to a contiguous 1-D float64 array (no copy if possible)."""
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be 1-D, got shape {arr.shape}")
    return arr


def _check_mask(name: str, mask: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Coerce a mask to 1-D float64. Mask values are interpreted as:
    NaN = unknown, 0 = no event, anything else (incl. negatives) = event.

    We deliberately treat ``!= 0`` (and finite) as an event so that a
    signed cascade mask ({-1,0,1}) can be fed in directly when appropriate;
    callers that need strict 0/1 should pass clean masks.
    """
    return _as_1d_float(name, mask)


def _require_positive_int(name: str, value: int) -> int:
    iv = int(value)
    if iv != value or iv < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return iv


# ---------------------------------------------------------------------------
# Crossings
# ---------------------------------------------------------------------------


def crossed_up(series: npt.ArrayLike, level: float) -> npt.NDArray[np.float64]:
    """1.0 where *series* crosses *up* through *level*, else 0.0; NaN undefined.

    A cross-up at bar *t* requires ``series[t-1] < level <= series[t]``.
    Bar 0 has no predecessor → NaN. If either ``series[t-1]`` or
    ``series[t]`` is NaN, the cross is undefined → NaN at *t*.
    """
    s = _as_1d_float("series", series)
    lvl = float(level)
    out = np.zeros_like(s)
    if s.shape[0] == 0:
        return out
    out[0] = np.nan
    if s.shape[0] >= 2:
        prev = s[:-1]
        cur = s[1:]
        undefined = np.isnan(prev) | np.isnan(cur)
        crossed = (prev < lvl) & (cur >= lvl)
        seg = np.where(crossed, 1.0, 0.0)
        seg[undefined] = np.nan
        out[1:] = seg
    return out


def crossed_down(series: npt.ArrayLike, level: float) -> npt.NDArray[np.float64]:
    """1.0 where *series* crosses *down* through *level*, else 0.0; NaN undefined.

    A cross-down at bar *t* requires ``series[t-1] > level >= series[t]``.
    Bar 0 → NaN. NaN in either bar → NaN.
    """
    s = _as_1d_float("series", series)
    lvl = float(level)
    out = np.zeros_like(s)
    if s.shape[0] == 0:
        return out
    out[0] = np.nan
    if s.shape[0] >= 2:
        prev = s[:-1]
        cur = s[1:]
        undefined = np.isnan(prev) | np.isnan(cur)
        crossed = (prev > lvl) & (cur <= lvl)
        seg = np.where(crossed, 1.0, 0.0)
        seg[undefined] = np.nan
        out[1:] = seg
    return out


# ---------------------------------------------------------------------------
# bars_since
# ---------------------------------------------------------------------------


def bars_since(mask: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Bars since the most recent event in *mask* (0 at the event bar).

    * Before any event has occurred → NaN.
    * At an event bar → 0.0, then 1.0, 2.0, … until the next event.
    * NaN handling: a NaN in *mask* means "we don't know whether bar *t*
      was an event". That poisons the running answer — at *t* and at every
      later bar the true count is ambiguous (it could be measured from the
      unknown bar) — until a *known* event re-anchors the count. So output
      is NaN from the first unknown bar up to (but not including) the next
      known event.
    """
    m = _check_mask("mask", mask)
    n = m.shape[0]
    out = np.empty(n, dtype=np.float64)
    since = np.nan  # bars since last KNOWN event; NaN = no anchor / ambiguous
    for t in range(n):
        v = m[t]
        if np.isnan(v):
            # Unknown event-status here: lose the anchor — anything after is
            # ambiguous until a known event re-establishes it.
            since = np.nan
            out[t] = np.nan
        elif v != 0.0:
            since = 0.0
            out[t] = 0.0
        else:
            if np.isnan(since):
                out[t] = np.nan
            else:
                since += 1.0
                out[t] = since
    return out


# ---------------------------------------------------------------------------
# count_in_window
# ---------------------------------------------------------------------------


def count_in_window(mask: npt.ArrayLike, window: int) -> npt.NDArray[np.float64]:
    """Rolling count of events in the trailing *window* bars (inclusive).

    * The first ``window - 1`` bars lack a full window → NaN (warmup).
    * If any bar inside the trailing window is NaN, the count is undefined
      → NaN (we cannot know how many events the window held).
    """
    m = _check_mask("mask", mask)
    w = _require_positive_int("window", window)
    n = m.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < w:
        return out
    events = (~np.isnan(m)) & (m != 0.0)
    nan_in = np.isnan(m)
    ev_f = events.astype(np.float64)
    nan_f = nan_in.astype(np.float64)
    # Trailing-window sums via cumulative sums (single pass, O(T)).
    ev_cum = np.concatenate(([0.0], np.cumsum(ev_f)))
    nan_cum = np.concatenate(([0.0], np.cumsum(nan_f)))
    idx = np.arange(w - 1, n)
    lo = idx - w + 1
    ev_win = ev_cum[idx + 1] - ev_cum[lo]
    nan_win = nan_cum[idx + 1] - nan_cum[lo]
    counts = np.where(nan_win > 0.0, np.nan, ev_win)
    out[idx] = counts
    return out


# ---------------------------------------------------------------------------
# sequence_within
# ---------------------------------------------------------------------------


def sequence_within(
    stages: list[npt.ArrayLike],
    window: int,
    abort: npt.ArrayLike | None = None,
) -> npt.NDArray[np.float64]:
    """Ordered multi-stage completion within a rolling *window*.

    *stages* is an ordered list of event masks ``[s0, s1, …, sk]``. The
    sequence fires (1.0) at the bar where the **final** stage ``sk``
    completes, provided every intermediate stage occurred in order and the
    final completion happens within *window* bars of the bar that started
    the sequence (``sk_bar - s0_bar <= window``).

    Greedy single forward pass: stage 0 (re)starts a candidate sequence at
    each of its events (the most recent start wins); each later stage, when
    it fires while the predecessor stage has been satisfied and we are still
    inside the window, advances the candidate; the final stage firing emits
    the event. An optional *abort* mask resets any in-progress candidate.

    NaN handling: at any bar where the **currently awaited** stage mask (or
    the abort mask) is NaN, the completion status is undefined → output is
    NaN at that bar and the in-progress candidate is invalidated (we can no
    longer trust the ordering through an unknown bar). Bars before/after,
    with known inputs, evaluate normally.
    """
    if len(stages) < 2:
        raise ValueError("sequence_within needs at least 2 stages")
    masks = [_check_mask(f"stages[{i}]", s) for i, s in enumerate(stages)]
    n = masks[0].shape[0]
    for i, s in enumerate(masks):
        if s.shape[0] != n:
            raise ValueError(
                f"all stage masks must share length {n}; stages[{i}] has {s.shape[0]}"
            )
    if abort is not None:
        ab = _check_mask("abort", abort)
        if ab.shape[0] != n:
            raise ValueError(f"abort must have length {n}, got {ab.shape[0]}")
    else:
        ab = None
    w = _require_positive_int("window", window)
    last_idx = len(masks) - 1

    out = np.zeros(n, dtype=np.float64)
    start_bar = -1  # bar where stage 0 started the active candidate; -1=none
    awaiting = 0  # index of the next stage we need to see (>=1 once started)

    for t in range(n):
        # 1) Abort check (known abort=event resets; NaN abort is undefined).
        if ab is not None:
            av = ab[t]
            if np.isnan(av):
                out[t] = np.nan
                start_bar = -1
                awaiting = 0
                continue
            if av != 0.0:
                start_bar = -1
                awaiting = 0

        # 2) Expire a stale candidate (started too long ago).
        if start_bar >= 0 and (t - start_bar) > w:
            start_bar = -1
            awaiting = 0

        # 3) The relevant stage mask at this bar is the one we await (or
        #    stage 0 if no candidate is active). NaN there is undefined.
        relevant = awaiting if start_bar >= 0 else 0
        rv = masks[relevant][t]
        if np.isnan(rv):
            out[t] = np.nan
            # Unknown ordering through this bar invalidates the candidate.
            start_bar = -1
            awaiting = 0
            continue

        if start_bar < 0:
            # No active candidate: a stage-0 event starts one.
            if rv != 0.0:
                start_bar = t
                awaiting = 1
            out[t] = 0.0
            continue

        # Active candidate, awaiting stage `awaiting`.
        if rv != 0.0:
            if awaiting == last_idx:
                # Final stage completed within the window -> fire.
                out[t] = 1.0
                start_bar = -1
                awaiting = 0
            else:
                awaiting += 1
                out[t] = 0.0
        else:
            out[t] = 0.0
    return out


# ---------------------------------------------------------------------------
# nth_event
# ---------------------------------------------------------------------------


def nth_event(
    mask: npt.ArrayLike,
    n: int,
    window: int,
    reset: npt.ArrayLike | None = None,
) -> npt.NDArray[np.float64]:
    """Fire (1.0) at the *n*-th event within a trailing *window* / since *reset*.

    At each event bar we count the events in the trailing *window* bars
    (inclusive); the output is 1.0 at the bar where that running count first
    equals *n*, else 0.0. An optional *reset* mask clears the running tally
    (a reset event at bar *t* means events at/after *t* start counting from
    1 again, ignoring the trailing window for events before the reset).

    NaN handling: a NaN in *mask* or *reset* makes the running count
    ambiguous → NaN at that bar and forward until the ambiguity leaves the
    window (for *mask* NaNs) or a known reset re-anchors the tally. We are
    conservative: while any NaN-mask bar is still inside the trailing
    window, the count is undefined → NaN (never a spurious fire).
    """
    m = _check_mask("mask", mask)
    target = _require_positive_int("n", n)
    w = _require_positive_int("window", window)
    length = m.shape[0]
    if reset is not None:
        rs = _check_mask("reset", reset)
        if rs.shape[0] != length:
            raise ValueError(f"reset must have length {length}, got {rs.shape[0]}")
    else:
        rs = None

    out = np.zeros(length, dtype=np.float64)
    # event_bars: indices of KNOWN events still relevant; nan_bars: indices
    # of unknown bars still inside the window.
    event_bars: deque[int] = deque()
    nan_bars: deque[int] = deque()
    anchor = 0  # earliest bar index the window may consider (reset boundary)

    for t in range(length):
        # Reset handling first.
        if rs is not None:
            rv = rs[t]
            if np.isnan(rv):
                out[t] = np.nan
                # Unknown reset status: ambiguous tally. Treat conservatively
                # by anchoring here and clearing — and marking this bar
                # unknown so it poisons the window like a mask-NaN.
                nan_bars.append(t)
                event_bars.clear()
                anchor = t + 1
                continue
            if rv != 0.0:
                event_bars.clear()
                nan_bars.clear()
                anchor = t

        lo = t - w + 1  # trailing window start
        lo = max(lo, anchor)
        # Evict bars that fell out of the window / before the anchor.
        while event_bars and event_bars[0] < lo:
            event_bars.popleft()
        while nan_bars and nan_bars[0] < lo:
            nan_bars.popleft()

        v = m[t]
        if np.isnan(v):
            nan_bars.append(t)
            out[t] = np.nan
            continue

        # If any unknown bar is still inside the window, the count is
        # ambiguous -> NaN (conservative; never fabricate a fire).
        if nan_bars:
            if v != 0.0:
                event_bars.append(t)
            out[t] = np.nan
            continue

        if v != 0.0:
            event_bars.append(t)
            out[t] = 1.0 if len(event_bars) == target else 0.0
        else:
            out[t] = 0.0
    return out


# ---------------------------------------------------------------------------
# regime_gate
# ---------------------------------------------------------------------------


def regime_gate(
    mask: npt.ArrayLike, regime_ok: npt.ArrayLike
) -> npt.NDArray[np.float64]:
    """Gate events in *mask* by a regime mask (logical AND, NaN-aware).

    Output is 1.0 where both *mask* and *regime_ok* are known events, 0.0
    where both known and at least one is non-event, and NaN where either
    input is NaN at that bar.
    """
    m = _check_mask("mask", mask)
    r = _check_mask("regime_ok", regime_ok)
    if m.shape[0] != r.shape[0]:
        raise ValueError(
            f"mask and regime_ok must share length; got {m.shape[0]} vs {r.shape[0]}"
        )
    undefined = np.isnan(m) | np.isnan(r)
    gated = (m != 0.0) & (r != 0.0)
    out = np.where(gated, 1.0, 0.0)
    out[undefined] = np.nan
    return out
