"""Unit tests for tcg/core/api/_dates.py::parse_iso_range."""

from __future__ import annotations

from datetime import date

import pytest

from tcg.core.api._dates import parse_iso_range


class TestParseIsoRangeHappyPath:
    def test_both_none_returns_none_none(self):
        start, end = parse_iso_range(None, None)
        assert start is None
        assert end is None

    def test_both_empty_string_returns_none_none(self):
        start, end = parse_iso_range("", "")
        assert start is None
        assert end is None

    def test_start_only(self):
        start, end = parse_iso_range("2024-01-15", None)
        assert start == date(2024, 1, 15)
        assert end is None

    def test_end_only(self):
        start, end = parse_iso_range(None, "2024-12-31")
        assert start is None
        assert end == date(2024, 12, 31)

    def test_both_dates_valid(self):
        start, end = parse_iso_range("2024-01-01", "2024-12-31")
        assert start == date(2024, 1, 1)
        assert end == date(2024, 12, 31)

    def test_start_equals_end(self):
        start, end = parse_iso_range("2024-06-15", "2024-06-15")
        assert start == end == date(2024, 6, 15)


class TestParseIsoRangeErrorWording:
    def test_invalid_start_raises_value_error_with_canonical_prefix(self):
        with pytest.raises(ValueError, match=r"Invalid date format"):
            parse_iso_range("not-a-date", None)

    def test_invalid_end_raises_value_error_with_canonical_prefix(self):
        with pytest.raises(ValueError, match=r"Invalid date format"):
            parse_iso_range(None, "2024-13-01")

    def test_invalid_start_error_message_contains_original_exc(self):
        with pytest.raises(ValueError, match=r"Invalid date format"):
            parse_iso_range("2024/01/01", None)  # slash-separated: not ISO

    def test_both_invalid_raises_on_start_first(self):
        # Both are bad — start is parsed first, so its error fires.
        with pytest.raises(ValueError, match=r"Invalid date format"):
            parse_iso_range("bad-start", "bad-end")


class TestParseIsoRangeEdgeCases:
    def test_leap_day_valid(self):
        start, end = parse_iso_range("2024-02-29", None)
        assert start == date(2024, 2, 29)

    def test_non_leap_day_invalid(self):
        with pytest.raises(ValueError, match=r"Invalid date format"):
            parse_iso_range("2023-02-29", None)

    def test_end_before_start_does_not_raise(self):
        # parse_iso_range does NOT validate order — callers are responsible.
        start, end = parse_iso_range("2024-12-31", "2024-01-01")
        assert start == date(2024, 12, 31)
        assert end == date(2024, 1, 1)

    def test_empty_start_empty_end(self):
        start, end = parse_iso_range("", "")
        assert start is None
        assert end is None

    def test_start_none_end_empty(self):
        start, end = parse_iso_range(None, "")
        assert start is None
        assert end is None

    def test_missing_day_component_invalid(self):
        with pytest.raises(ValueError, match=r"Invalid date format"):
            parse_iso_range("2024-01", None)
