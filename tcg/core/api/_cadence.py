"""Pure cadence classification for option-cycle coverage (presentation layer).

A single expiration *cycle* (e.g. the monthly-serial "W3 Friday") can list
contracts at different densities over its life: the S&P 500 W3 cycle only
listed *quarterly* 3rd-Fridays before ~2016, then became fully *monthly*.
Presence-based coverage therefore floors the picker at the quarterly era and a
"monthly W3" strategy would silently run quarterly over that span.

This module turns a per-cycle **expiration** list into contiguous cadence
``segments`` and a ``recommended_start`` = the floor of the cycle's *natural*
(current) cadence.  It is pure and deterministic — no I/O, no data/engine
imports — so it lives in the API/presentation layer and is unit-tested hard.

Algorithm (see design A.3): bucket expiries by calendar month, then for each
month in ``[start, end]`` count how many distinct expiry-months fall in the
rolling 12-month window ``[m-5, m+6]``.  Label by density
(``monthly ≥ 8`` / ``quarterly 3–7`` / ``sparse ≤ 2``), coalesce equal
consecutive labels into runs, and map the runs onto the ``[start, end]``
trade-date span.  ``recommended_start`` is the start of the earliest run whose
cadence equals the last (most-recent) segment's cadence — i.e. no cliff ⇒
``recommended_start == start``.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Literal

Cadence = Literal["monthly", "quarterly", "sparse"]

# Rolling-12-month distinct-expiry-month thresholds, expressed as *densities*
# (present-months / window-months) so the classifier is stable at the data
# boundaries.  A raw absolute count over a FIXED [m-5, m+6] window collapses
# near ``end`` (its leading half has no future data yet), which would spuriously
# fragment the newest era into a trailing lower-cadence segment.  Clamping the
# window to the data extent and thresholding on the ratio keeps the exact
# 8/3-per-12-months semantics of the design while removing that artifact.
_MONTHLY_MIN_DENSITY = 8 / 12  # >= 8 of 12 months  -> monthly
_QUARTERLY_MIN_DENSITY = 3 / 12  # >= 3 of 12 months -> quarterly (< 3 -> sparse)
# Rolling window offsets around a month bucket (trailing 5 + current + leading 6 = 12).
_WIN_BACK = 5
_WIN_FWD = 6

__all__ = ["Cadence", "Segment", "classify_cadence_segments"]


@dataclass(frozen=True)
class Segment:
    """A contiguous cadence run expressed on the trade-date span."""

    start: date
    end: date
    cadence: Cadence


def _month_index(d: date) -> int:
    """Integer month bucket: year*12 + (month-1), monotonic and gap-free."""
    return d.year * 12 + (d.month - 1)


def _month_first_day(idx: int) -> date:
    y, m = divmod(idx, 12)
    return date(y, m + 1, 1)


def _month_last_day(idx: int) -> date:
    y, m = divmod(idx, 12)
    last = calendar.monthrange(y, m + 1)[1]
    return date(y, m + 1, last)


def _label(present_count: int, window_months: int) -> Cadence:
    density = present_count / window_months if window_months else 0.0
    if density >= _MONTHLY_MIN_DENSITY:
        return "monthly"
    if density >= _QUARTERLY_MIN_DENSITY:
        return "quarterly"
    return "sparse"


def classify_cadence_segments(
    expiries: list[date],
    start: date,
    end: date,
) -> tuple[date, list[Segment]]:
    """Classify a cycle's expiries into contiguous cadence segments.

    Args:
        expiries: distinct expiration dates for ONE cycle (order irrelevant).
        start, end: the raw trade-date coverage extent for that cycle. The
            returned segments are clamped to and cover exactly ``[start, end]``.

    Returns:
        ``(recommended_start, segments)`` where ``segments`` is ordered,
        contiguous and gap-free over ``[start, end]`` (≥ 1 entry), and
        ``recommended_start`` is the floor of the cycle's current cadence
        (``== start`` when there is no cadence cliff).
    """
    lo, hi = _month_index(start), _month_index(end)
    # Only expiries WITHIN the coverage window shape the cadence.  Cycles list
    # contracts far into the future (LEAP-like quarterly/sparse listings out to
    # +5y); those out-of-window months must not fragment the tail of the span.
    present = {b for e in expiries if lo <= (b := _month_index(e)) <= hi}

    # Per-month cadence label from the rolling ~12-month distinct-month density.
    # The window is clamped to the data extent [lo, hi] so its ends are not
    # penalised for months that lie outside the cycle's life.
    labels: list[Cadence] = []
    for m in range(lo, hi + 1):
        w_lo = max(lo, m - _WIN_BACK)
        w_hi = min(hi, m + _WIN_FWD)
        window_months = w_hi - w_lo + 1
        present_count = sum(1 for b in present if w_lo <= b <= w_hi)
        labels.append(_label(present_count, window_months))

    # Coalesce consecutive equal labels into runs of month buckets.
    runs: list[tuple[int, int, Cadence]] = []  # (start_month, end_month, cadence)
    for offset, lab in enumerate(labels):
        m = lo + offset
        if runs and runs[-1][2] == lab:
            s0, _, c0 = runs[-1]
            runs[-1] = (s0, m, c0)
        else:
            runs.append((m, m, lab))

    # Map month-runs onto the [start, end] trade-date span (clamp the ends).
    segments: list[Segment] = []
    for i, (rs, re, cad) in enumerate(runs):
        seg_start = start if i == 0 else _month_first_day(rs)
        seg_end = end if i == len(runs) - 1 else _month_last_day(re)
        segments.append(Segment(seg_start, seg_end, cad))

    # recommended_start = start of the earliest run matching the LAST (current)
    # segment's cadence → no cliff ⇒ the first segment ⇒ == start.
    natural = segments[-1].cadence
    recommended_start = next(s.start for s in segments if s.cadence == natural)

    return recommended_start, segments
