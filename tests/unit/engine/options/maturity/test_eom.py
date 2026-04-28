"""Tests for EndOfMonth maturity rule.

Uses CME_TradeDate calendar (mapped from spec's "CME" alias).
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.types.options import EndOfMonth

_r = DefaultMaturityResolver()


@pytest.mark.parametrize("ref_date, offset, expected", [
    # Feb 2024 is a leap year → last business day = Feb 29 (Thursday, not a holiday)
    (date(2024, 2, 1),  0, date(2024, 2, 29)),
    # Jan 2024 + 11 months = Dec 2024. Dec 31 2024 is Tuesday → last biz day = Dec 31
    (date(2024, 1, 15), 11, date(2024, 12, 31)),
    # Jan 2025 last business day: Jan 31 is Friday → Jan 31
    (date(2025, 1, 1),  0, date(2025, 1, 31)),
    # offset=0, mid-month: still resolves to same month's last biz day
    # Mar 29 2024 is Good Friday (CME_TradeDate holiday); last biz day = Mar 28
    (date(2024, 3, 10), 0, date(2024, 3, 28)),
    # offset=1
    (date(2024, 1, 1),  1, date(2024, 2, 29)),
    # Cross-year: Dec 2024 + 1 = Jan 2025
    (date(2024, 12, 1), 1, date(2025, 1, 31)),
])
def test_end_of_month(ref_date: date, offset: int, expected: date) -> None:
    result = _r.resolve(ref_date, EndOfMonth(offset_months=offset))
    assert result == expected, (
        f"resolve({ref_date}, EndOfMonth(offset={offset})) → {result}, expected {expected}"
    )


def test_eom_march_2024_good_friday() -> None:
    """Good Friday 2024 (Mar 29) is a CME_TradeDate holiday.

    March 2024 last business day should be March 28.
    """
    result = _r.resolve(date(2024, 3, 1), EndOfMonth(offset_months=0))
    assert result == date(2024, 3, 28)
