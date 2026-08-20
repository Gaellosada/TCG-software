"""Unit tests for the curated macro event-day calendar (F3.1).

Pure — no DB. Verifies the loader API (per-type dates, union, type-set union),
tentative-flag surfacing via the pure builder, config parsing, and a spot-check
that a couple of primary-source-verified real dates are present.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.types import event_calendar as ec


# --------------------------------------------------------------------------- #
# Config parses + shape
# --------------------------------------------------------------------------- #
def test_calendar_parses_and_is_nonempty() -> None:
    days = ec.all_event_days()
    assert len(days) > 0
    # Every entry is a well-formed EventDay of a known type.
    for e in days:
        assert isinstance(e.date, date)
        assert e.event_type in ec.EVENT_TYPES


def test_calendar_is_sorted_by_date_then_type() -> None:
    days = ec.all_event_days()
    keys = [(e.date, e.event_type) for e in days]
    assert keys == sorted(keys)


def test_event_types_are_the_three_expected() -> None:
    assert set(ec.EVENT_TYPES) == {"FOMC", "NFP", "CPI"}


# --------------------------------------------------------------------------- #
# Per-type dates
# --------------------------------------------------------------------------- #
def test_event_dates_per_type_all_match_type() -> None:
    for t in ec.EVENT_TYPES:
        for e in ec.event_days(t):
            assert e.event_type == t


def test_unknown_event_type_raises() -> None:
    with pytest.raises(ValueError):
        ec.event_dates("PPI")
    with pytest.raises(ValueError):
        ec.event_dates_for_types(["FOMC", "PPI"])


# --------------------------------------------------------------------------- #
# Spot-checks of REAL verified dates (primary-source confirmed)
# --------------------------------------------------------------------------- #
def test_known_fomc_dates_present() -> None:
    fomc = set(ec.event_dates("FOMC"))
    # federalreserve.gov FOMC statement days.
    assert date(2025, 1, 29) in fomc  # Jan 2025 decision
    assert date(2025, 6, 18) in fomc  # Jun 2025 decision
    assert date(2026, 3, 18) in fomc  # Mar 2026 decision


def test_known_nfp_and_cpi_dates_present() -> None:
    nfp = set(ec.event_dates("NFP"))
    cpi = set(ec.event_dates("CPI"))
    assert date(2025, 8, 1) in nfp     # Jul-2025 jobs report (empsit_08012025)
    assert date(2025, 11, 20) in nfp   # shutdown-delayed Sep data
    assert date(2025, 9, 11) in cpi    # Aug-2025 CPI (cpi_09112025)
    assert date(2025, 10, 24) in cpi   # shutdown-rescheduled Sep CPI


def test_shutdown_gap_no_phantom_dates() -> None:
    # October-2025-reference CPI was NEVER published; there is no mid-Nov 2025 CPI.
    cpi_2025_nov = [d for d in ec.event_dates("CPI")
                    if d.year == 2025 and d.month == 11]
    assert cpi_2025_nov == []


# --------------------------------------------------------------------------- #
# Union + type-set union (the F3.2 resolver surface)
# --------------------------------------------------------------------------- #
def test_all_event_dates_is_union_deduped_sorted() -> None:
    union = ec.all_event_dates()
    # Sorted + unique.
    assert list(union) == sorted(set(union))
    # Union == union of the three per-type sets.
    expected = (
        set(ec.event_dates("FOMC"))
        | set(ec.event_dates("NFP"))
        | set(ec.event_dates("CPI"))
    )
    assert set(union) == expected


def test_event_dates_for_types_unions_selected_only() -> None:
    fomc_only = ec.event_dates_for_types(["FOMC"])
    assert fomc_only == frozenset(ec.event_dates("FOMC"))

    two = ec.event_dates_for_types(["FOMC", "CPI"])
    assert two == frozenset(ec.event_dates("FOMC")) | frozenset(ec.event_dates("CPI"))

    # Empty selection => empty set (NOT all dates).
    assert ec.event_dates_for_types([]) == frozenset()

    # All three == the full union.
    assert ec.event_dates_for_types(ec.EVENT_TYPES) == frozenset(ec.all_event_dates())


# --------------------------------------------------------------------------- #
# Tentative-flag mechanism (surfaced by the pure builder)
# --------------------------------------------------------------------------- #
def test_current_calendar_has_no_tentative_dates() -> None:
    # Every shipped date is primary-source verified.
    assert ec.tentative_days() == ()


def test_builder_surfaces_tentative_flag() -> None:
    raw = {ec.FOMC: ("2027-01-27",), ec.CPI: ("2027-01-13", "2027-02-11")}
    built = ec._build_calendar(raw, frozenset({"2027-02-11"}))
    by_date = {e.date: e for e in built}
    assert by_date[date(2027, 2, 11)].tentative is True
    assert by_date[date(2027, 1, 27)].tentative is False
    assert by_date[date(2027, 1, 13)].tentative is False


def test_builder_rejects_malformed_date_loudly() -> None:
    with pytest.raises(ValueError):
        ec._build_calendar({ec.FOMC: ("not-a-date",)}, frozenset())
