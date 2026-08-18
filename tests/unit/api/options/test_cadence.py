"""Unit tests for the pure cadence classifier (``tcg.core.api._cadence``).

Pure/deterministic — no I/O.  Pins the segment boundaries produced for the
real W3-v2 shape (quarterly-only 2010–2015 → monthly 2016+) plus the
degenerate / single-cadence / empty inputs.
"""

from __future__ import annotations

from datetime import date

from tcg.core.api._cadence import classify_cadence_segments


def _monthly(y0: int, m0: int, y1: int, m1: int) -> list[date]:
    """Every month's mid-month expiry in [y0-m0 .. y1-m1] inclusive."""
    out: list[date] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(date(y, m, 18))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _quarterly(y0: int, y1: int) -> list[date]:
    """Mar/Jun/Sep/Dec mid-month expiries for each year in [y0..y1]."""
    return [date(y, mth, 18) for y in range(y0, y1 + 1) for mth in (3, 6, 9, 12)]


# ---------------------------------------------------------------------------
# The headline case: W3-v2 shape → two segments, cliff into a monthly era.
# ---------------------------------------------------------------------------


def test_w3_v2_shape_quarterly_then_monthly():
    """Quarterly-only 2010–2015 then fully monthly 2016+ must yield exactly two
    contiguous segments (quarterly then monthly) covering [start, end] with a
    ``recommended_start`` that lands inside the monthly segment."""
    expiries = _quarterly(2010, 2015) + _monthly(2016, 1, 2026, 7)
    start, end = date(2010, 6, 7), date(2026, 7, 27)

    rec, segs = classify_cadence_segments(expiries, start, end)

    assert [s.cadence for s in segs] == ["quarterly", "monthly"]
    # Contiguous, gap-free, clamped to [start, end].
    assert segs[0].start == start
    assert segs[-1].end == end
    for a, b in zip(segs, segs[1:]):
        # next starts the day after the previous ends
        assert (b.start - a.end).days == 1
    # recommendation is the monthly-era floor, well after the quarterly years.
    assert rec == segs[1].start
    assert rec.year >= 2015  # excludes the clearly-quarterly 2010–2014
    assert rec < date(2017, 1, 1)  # and lands by/near the 2016 cliff
    # The quarterly era stays visible as the first segment (from raw start).
    assert segs[0].end < rec


# ---------------------------------------------------------------------------
# No cliff → single segment, recommended_start == start.
# ---------------------------------------------------------------------------


def test_pure_monthly_single_segment():
    expiries = _monthly(2016, 1, 2026, 7)
    start, end = date(2016, 2, 22), date(2026, 7, 27)
    rec, segs = classify_cadence_segments(expiries, start, end)
    assert len(segs) == 1
    assert segs[0].cadence == "monthly"
    assert segs[0].start == start and segs[0].end == end
    assert rec == start


def test_pure_quarterly_single_segment_no_false_cliff():
    """A cycle that is quarterly for its WHOLE life → one quarterly segment,
    recommended_start == start (no false cliff; cycle-agnostic)."""
    expiries = _quarterly(2010, 2026)
    start, end = date(2010, 3, 18), date(2026, 12, 18)
    rec, segs = classify_cadence_segments(expiries, start, end)
    assert len(segs) == 1
    assert segs[0].cadence == "quarterly"
    assert rec == start


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_empty_expiries_single_sparse_segment():
    start, end = date(2020, 1, 1), date(2020, 6, 30)
    rec, segs = classify_cadence_segments([], start, end)
    assert len(segs) == 1
    assert segs[0].start == start and segs[0].end == end
    assert rec == start


def test_recommended_start_uses_latest_segment_cadence():
    """recommended_start = start of the EARLIEST run matching the LAST segment's
    cadence — so a quarterly→monthly cliff recommends the monthly floor."""
    expiries = _quarterly(2010, 2015) + _monthly(2016, 1, 2026, 7)
    start, end = date(2010, 6, 7), date(2026, 7, 27)
    rec, segs = classify_cadence_segments(expiries, start, end)
    monthly_segs = [s for s in segs if s.cadence == "monthly"]
    assert rec == monthly_segs[0].start
    # last segment is the natural (current) cadence
    assert segs[-1].cadence == "monthly"
