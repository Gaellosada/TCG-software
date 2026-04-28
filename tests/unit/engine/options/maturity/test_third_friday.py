"""Tests for NextThirdFriday maturity rule.

2024 third Fridays: Jan=19, Feb=16, Mar=15, Apr=19, May=17, Jun=21,
                    Jul=19, Aug=16, Sep=20, Oct=18, Nov=15, Dec=20.

Holiday case: verified via pandas_market_calendars CME_TradeDate that
2014-04-18 is a 3rd Friday AND a CME_TradeDate holiday (Good Friday).
The resolver must return 2014-04-17 (prior business day).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.types.options import NextThirdFriday

_r = DefaultMaturityResolver()


# ---------------------------------------------------------------------------
# Known 2024 third Fridays
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref_date, offset, expected", [
    # offset=0: resolve to current month's 3rd Friday if ref_date <= it
    (date(2024, 1, 1),  0, date(2024, 1, 19)),
    (date(2024, 1, 19), 0, date(2024, 1, 19)),   # ref_date == 3rd Friday → same day
    # offset=0: ref_date past 3rd Friday → advance to next month
    (date(2024, 1, 20), 0, date(2024, 2, 16)),
    # offset=1: always at least one full month ahead
    (date(2024, 1, 1),  1, date(2024, 2, 16)),
    (date(2024, 1, 16), 1, date(2024, 2, 16)),
    (date(2024, 1, 17), 1, date(2024, 2, 16)),
    # More months
    (date(2024, 3, 1),  0, date(2024, 3, 15)),
    (date(2024, 4, 1),  0, date(2024, 4, 19)),
    (date(2024, 6, 1),  0, date(2024, 6, 21)),
    (date(2024, 12, 1), 0, date(2024, 12, 20)),
])
def test_known_2024_third_fridays(ref_date: date, offset: int, expected: date) -> None:
    result = _r.resolve(ref_date, NextThirdFriday(offset_months=offset))
    assert result == expected, (
        f"resolve({ref_date}, offset={offset}) → {result}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# Holiday case: 2014-04-18 is the 3rd Friday of April 2014 AND a CME
# holiday (Good Friday).  The resolver must return 2014-04-17.
# ---------------------------------------------------------------------------

def test_holiday_skip_real_case_2014_april() -> None:
    """3rd Friday of April 2014 (2014-04-18) is a CME_TradeDate holiday.

    Expected result: 2014-04-17 (prior business day, per spec §3.4).
    """
    result = _r.resolve(date(2014, 4, 1), NextThirdFriday(offset_months=0))
    assert result == date(2014, 4, 17), (
        f"Expected 2014-04-17 (prior biz day before Good Friday 2014-04-18), got {result}"
    )


def test_holiday_skip_2019_april() -> None:
    """3rd Friday of April 2019 (2019-04-19) is a CME_TradeDate holiday.

    Expected result: 2019-04-18 (prior business day).
    """
    result = _r.resolve(date(2019, 4, 1), NextThirdFriday(offset_months=0))
    assert result == date(2019, 4, 18)


def test_holiday_skip_2022_april() -> None:
    """3rd Friday of April 2022 (2022-04-15) is a CME_TradeDate holiday.

    Expected result: 2022-04-14 (prior business day).
    """
    result = _r.resolve(date(2022, 4, 1), NextThirdFriday(offset_months=0))
    assert result == date(2022, 4, 14)


# ---------------------------------------------------------------------------
# Synthetic holiday via dependency injection (mock the calendar)
# ---------------------------------------------------------------------------

def test_holiday_skip_synthetic_mock() -> None:
    """Inject a synthetic calendar where any arbitrary 3rd Friday is a holiday.

    We pick 2024-03-15 (3rd Friday of March 2024) and declare it a holiday.
    Expected: resolver returns 2024-03-14 (prior business day).
    """
    # Build a mock calendar where valid_days returns empty when queried for
    # 2024-03-15 alone, and returns 2024-03-14 when queried for that date.
    def _mock_valid_days(start_date, end_date):
        """Synthetic: 2024-03-15 is not a valid day."""
        import pandas as pd
        # If the single-day probe is the holiday → return empty
        if start_date == date(2024, 3, 15) and end_date == date(2024, 3, 15):
            return pd.DatetimeIndex([], dtype="datetime64[us, UTC]")
        # 2024-03-14 is valid
        if start_date == date(2024, 3, 14) and end_date == date(2024, 3, 14):
            return pd.DatetimeIndex(
                [pd.Timestamp("2024-03-14", tz="UTC")],
                dtype="datetime64[us, UTC]",
            )
        # Default: return a real calendar response (won't be reached in this test)
        from tcg.engine.options.maturity.resolver import _get_calendar
        real_cal = _get_calendar("CME_TradeDate")
        return real_cal.valid_days(start_date=start_date, end_date=end_date)

    mock_cal = MagicMock()
    mock_cal.valid_days.side_effect = _mock_valid_days

    from tcg.engine.options.maturity import resolver as res_module

    with patch.object(res_module, "_get_calendar", return_value=mock_cal):
        resolver = DefaultMaturityResolver()
        result = resolver.resolve(date(2024, 3, 1), NextThirdFriday(offset_months=0))

    assert result == date(2024, 3, 14)


# ---------------------------------------------------------------------------
# Edge: non-holiday 3rd Friday unchanged
# ---------------------------------------------------------------------------

def test_non_holiday_unchanged() -> None:
    """March 15 2024 is not a holiday; must be returned as-is."""
    result = _r.resolve(date(2024, 3, 1), NextThirdFriday(offset_months=0))
    assert result == date(2024, 3, 15)


# ---------------------------------------------------------------------------
# Cross-year boundary: offset pushes into next year
# ---------------------------------------------------------------------------

def test_cross_year_boundary() -> None:
    """offset_months=3 from Oct 2024 → Jan 2025 (3rd Friday = Jan 17)."""
    result = _r.resolve(date(2024, 10, 1), NextThirdFriday(offset_months=3))
    assert result == date(2025, 1, 17)
