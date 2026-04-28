"""Tests for FixedDate maturity rule.

Spec §3.4: returned as-is, no transformation.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.types.options import FixedDate

_r = DefaultMaturityResolver()


@pytest.mark.parametrize("ref_date, fixed, expected", [
    # Spec-mandated case
    (date(2024, 3, 15), date(2025, 1, 1), date(2025, 1, 1)),
    # fixed date in the past → still returned as-is
    (date(2024, 6, 1),  date(2020, 1, 1), date(2020, 1, 1)),
    # fixed date == ref_date
    (date(2024, 3, 15), date(2024, 3, 15), date(2024, 3, 15)),
    # fixed on a weekend → returned as-is (no adjustment)
    (date(2024, 1, 1),  date(2024, 1, 6), date(2024, 1, 6)),   # Saturday
    # fixed on a holiday → returned as-is
    (date(2024, 1, 1),  date(2024, 3, 29), date(2024, 3, 29)), # Good Friday
])
def test_fixed_date(ref_date: date, fixed: date, expected: date) -> None:
    rule = FixedDate(date=fixed)
    result = _r.resolve(ref_date, rule)
    assert result == expected, (
        f"resolve({ref_date}, FixedDate({fixed})) → {result}, expected {expected}"
    )
