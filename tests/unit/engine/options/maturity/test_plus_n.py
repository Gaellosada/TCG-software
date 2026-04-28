"""Tests for PlusNDays maturity rule.

Spec §3.4: pure calendar arithmetic, no business-day adjustment.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.types.options import PlusNDays

_r = DefaultMaturityResolver()


@pytest.mark.parametrize("ref_date, n, expected", [
    # Spec-mandated cases
    (date(2024, 3, 15), 30, date(2024, 4, 14)),
    # No business-day adjustment: lands on a Sunday → returned as-is
    (date(2024, 3, 15),  2, date(2024, 3, 17)),   # Sunday
    # n=0 → same day
    (date(2024, 6, 21),  0, date(2024, 6, 21)),
    # Lands on a public holiday → returned as-is (no adjustment)
    (date(2024, 3, 28),  1, date(2024, 3, 29)),   # Good Friday, still returned
    # Cross-month
    (date(2024, 1, 31),  1, date(2024, 2, 1)),
    # Cross-year
    (date(2024, 12, 31), 1, date(2025, 1, 1)),
    # Large n
    (date(2024, 1, 1), 365, date(2024, 12, 31)),
])
def test_plus_n_days(ref_date: date, n: int, expected: date) -> None:
    result = _r.resolve(ref_date, PlusNDays(n=n))
    assert result == expected, (
        f"resolve({ref_date}, PlusNDays({n})) → {result}, expected {expected}"
    )


def test_plus_n_no_calendar_query() -> None:
    """PlusNDays must NOT query the calendar; verify by checking result on a known holiday."""
    # Good Friday 2024 + 0 days → 2024-03-29 (holiday) returned as-is
    result = _r.resolve(date(2024, 3, 29), PlusNDays(n=0))
    assert result == date(2024, 3, 29)
