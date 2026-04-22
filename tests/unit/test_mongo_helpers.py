"""Unit tests for tcg/data/_mongo/helpers.py — extract_price_data.

Verifies that bars with bad ``date`` fields (string, None, NaN, etc.)
are silently skipped with a warning rather than raising an exception,
and that valid bars in the same document are still included.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tcg.data._mongo.helpers import extract_price_data


def _make_doc(_id, bars):
    """Build a minimal MongoDB document with ``eodDatas``."""
    return {"_id": _id, "eodDatas": {"provider1": bars}}


def _good_bar(date: int, close: float) -> dict:
    return {"date": date, "open": close, "high": close, "low": close, "close": close, "volume": 100.0}


# ---------------------------------------------------------------------------
# Bad-date bars: must be skipped with a warning, not raise
# ---------------------------------------------------------------------------

class TestBadDateBars:
    def test_none_date_skipped_with_warning(self, caplog):
        import logging
        bars = [
            {"date": None, "close": 100.0},
            _good_bar(20240102, 101.0),
        ]
        doc = _make_doc("inst1", bars)
        with caplog.at_level(logging.WARNING, logger="tcg.data._mongo.helpers"):
            result = extract_price_data(doc)
        assert result is not None
        assert len(result.dates) == 1
        assert result.dates[0] == 20240102

    def test_string_date_skipped_with_warning(self, caplog):
        import logging
        bars = [
            {"date": "not-a-date", "close": 100.0},
            _good_bar(20240102, 101.0),
        ]
        doc = _make_doc("inst1", bars)
        with caplog.at_level(logging.WARNING, logger="tcg.data._mongo.helpers"):
            result = extract_price_data(doc)
        assert result is not None
        assert len(result.dates) == 1
        assert result.dates[0] == 20240102
        # Warning should mention the bad date
        assert any("non-integer date" in r.message for r in caplog.records)

    def test_nan_date_skipped_with_warning(self, caplog):
        """float('nan') as date should be skipped."""
        import logging
        bars = [
            {"date": float("nan"), "close": 100.0},
            _good_bar(20240103, 102.0),
        ]
        doc = _make_doc("inst2", bars)
        with caplog.at_level(logging.WARNING, logger="tcg.data._mongo.helpers"):
            result = extract_price_data(doc)
        assert result is not None
        assert len(result.dates) == 1
        assert result.dates[0] == 20240103

    def test_dict_date_skipped_with_warning(self, caplog):
        """A dict (wrong type) as date should be skipped."""
        import logging
        bars = [
            {"date": {"year": 2024}, "close": 100.0},
            _good_bar(20240104, 103.0),
        ]
        doc = _make_doc("inst3", bars)
        with caplog.at_level(logging.WARNING, logger="tcg.data._mongo.helpers"):
            result = extract_price_data(doc)
        assert result is not None
        assert len(result.dates) == 1
        assert result.dates[0] == 20240104

    def test_all_bad_dates_returns_none(self, caplog):
        """If every bar has a bad date, result is None."""
        import logging
        bars = [
            {"date": None, "close": 100.0},
            {"date": "bad", "close": 101.0},
        ]
        doc = _make_doc("inst4", bars)
        with caplog.at_level(logging.WARNING, logger="tcg.data._mongo.helpers"):
            result = extract_price_data(doc)
        assert result is None

    def test_numeric_string_date_accepted(self):
        """A string that parses as int (e.g. '20240101') is accepted."""
        bars = [{"date": "20240101", "close": 100.0}]
        doc = _make_doc("inst5", bars)
        result = extract_price_data(doc)
        assert result is not None
        assert result.dates[0] == 20240101

    def test_float_date_accepted_truncated(self):
        """A float like 20240101.0 is accepted (int conversion truncates)."""
        bars = [{"date": 20240101.0, "close": 100.0}]
        doc = _make_doc("inst6", bars)
        result = extract_price_data(doc)
        assert result is not None
        assert result.dates[0] == 20240101


# ---------------------------------------------------------------------------
# Standard functionality: good bars still work
# ---------------------------------------------------------------------------

class TestGoodBars:
    def test_basic_extraction(self):
        bars = [_good_bar(20240101, 100.0), _good_bar(20240102, 101.0)]
        doc = _make_doc("inst", bars)
        result = extract_price_data(doc)
        assert result is not None
        np.testing.assert_array_equal(result.dates, [20240101, 20240102])
        np.testing.assert_array_almost_equal(result.close, [100.0, 101.0])

    def test_empty_bars_returns_none(self):
        doc = _make_doc("inst", [])
        assert extract_price_data(doc) is None

    def test_no_eod_datas_returns_none(self):
        doc = {"_id": "inst"}
        assert extract_price_data(doc) is None

    def test_nan_close_bar_dropped(self, caplog):
        """A bar with NaN close must be dropped."""
        import logging
        bars = [
            {"date": 20240101, "close": float("nan")},
            _good_bar(20240102, 101.0),
        ]
        doc = _make_doc("inst", bars)
        with caplog.at_level(logging.WARNING, logger="tcg.data._mongo.helpers"):
            result = extract_price_data(doc)
        assert result is not None
        assert len(result.dates) == 1
        assert result.dates[0] == 20240102
