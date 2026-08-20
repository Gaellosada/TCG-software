"""Unit tests for the crossed-quote exclusion predicate (Gap 4b).

Pure — no DB. ``_clean_two_sided`` is the single testable guard the intraday v2
reader applies before accepting a bbba two-sided quote as a usable mark. A
crossed / locked book (ask <= bid) must be REJECTED so it degrades to the
trade-bar close and fails the downstream max_spread / min_quote guards, rather
than fabricating a mid from an inverted book.
"""

from __future__ import annotations

from tcg.data._sql.intraday_v2 import _clean_two_sided


def test_clean_two_sided_excludes_crossed():
    # Normal uncrossed 2-sided quote: accepted.
    assert _clean_two_sided(29.9, 30.1) is True
    # Crossed book (ask < bid): rejected.
    assert _clean_two_sided(30.1, 29.9) is False
    # Locked book (ask == bid): rejected (ask must be STRICTLY > bid).
    assert _clean_two_sided(30.0, 30.0) is False
    # Missing side: rejected.
    assert _clean_two_sided(None, 30.0) is False
    assert _clean_two_sided(30.0, None) is False
    # Non-positive bid: rejected.
    assert _clean_two_sided(0.0, 1.0) is False
