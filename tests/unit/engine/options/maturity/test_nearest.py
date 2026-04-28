"""Tests for NearestToTarget maturity rule.

Spec §3.4:
- resolve_with_chain picks the expiration closest to ref_date + target_dte_days.
- Tie-break: lower DTE wins.
- Empty chain → None.
- resolve() (without chain) raises ValueError.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.types.options import NearestToTarget

_r = DefaultMaturityResolver()


# ---------------------------------------------------------------------------
# Basic resolution
# ---------------------------------------------------------------------------

def test_nearest_basic() -> None:
    """Spec example: target_dte=30, chain = [Apr 5, Apr 19, May 17].

    ref_date = 2024-03-15
    target_date = 2024-04-14
    DTE: Apr 5=21, Apr 19=35, May 17=63
    |Δ|: Apr 5=|21-30|=9, Apr 19=|35-30|=5, May 17=|63-30|=33
    → Apr 19 wins.
    """
    chain = [date(2024, 4, 5), date(2024, 4, 19), date(2024, 5, 17)]
    result = _r.resolve_with_chain(date(2024, 3, 15), NearestToTarget(30), chain)
    assert result == date(2024, 4, 19)


def test_nearest_single_element() -> None:
    """Single expiration → returned regardless of DTE distance."""
    chain = [date(2024, 6, 21)]
    result = _r.resolve_with_chain(date(2024, 3, 15), NearestToTarget(30), chain)
    assert result == date(2024, 6, 21)


# ---------------------------------------------------------------------------
# Tie-break: lower DTE wins
# ---------------------------------------------------------------------------

def test_tiebreak_lower_dte_wins() -> None:
    """Two expirations equidistant from target → lower DTE (earlier date) wins.

    ref_date = 2024-01-01, target_dte = 20
    target_date = 2024-01-21
    chain: [2024-01-11 (DTE=10), 2024-01-31 (DTE=30)]
    |Δ| for DTE=10: |10-20|=10
    |Δ| for DTE=30: |30-20|=10  → tie
    Lower DTE wins → 2024-01-11 (DTE=10)
    """
    chain = [date(2024, 1, 11), date(2024, 1, 31)]
    result = _r.resolve_with_chain(date(2024, 1, 1), NearestToTarget(20), chain)
    assert result == date(2024, 1, 11)


def test_tiebreak_lower_dte_synthetic_equal() -> None:
    """Exact tie at DTE=10 vs DTE=10 doesn't exist (same date = one element).

    Synthetic: both expirations same distance from target.
    ref_date = 2024-01-01, target_dte = 15
    chain: [2024-01-06 (DTE=5, |Δ|=10), 2024-01-26 (DTE=25, |Δ|=10)]
    Lower DTE → 2024-01-06
    """
    chain = [date(2024, 1, 6), date(2024, 1, 26)]
    result = _r.resolve_with_chain(date(2024, 1, 1), NearestToTarget(15), chain)
    assert result == date(2024, 1, 6)


def test_tiebreak_upper_vs_lower() -> None:
    """Spec example variant: target=20, chain=[2024-3-25 (DTE=10), 2024-4-15 (DTE=31)].

    ref_date = 2024-03-15
    |Δ| for DTE=10: |10-20|=10
    |Δ| for DTE=31: |31-20|=11
    → DTE=10 wins (smaller delta, no tie).
    """
    chain = [date(2024, 3, 25), date(2024, 4, 15)]
    result = _r.resolve_with_chain(date(2024, 3, 15), NearestToTarget(20), chain)
    assert result == date(2024, 3, 25)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_chain_returns_none() -> None:
    result = _r.resolve_with_chain(date(2024, 3, 15), NearestToTarget(30), [])
    assert result is None


def test_resolve_without_chain_raises_valueerror() -> None:
    """resolve() with NearestToTarget must raise ValueError."""
    with pytest.raises(ValueError, match="resolve_with_chain"):
        _r.resolve(date(2024, 3, 15), NearestToTarget(30))


def test_exact_target_match() -> None:
    """Expiration exactly at target DTE → returned directly."""
    ref = date(2024, 3, 15)
    target_dte = 30
    exp = ref + __import__("datetime").timedelta(days=target_dte)
    result = _r.resolve_with_chain(ref, NearestToTarget(target_dte), [exp])
    assert result == exp


# ---------------------------------------------------------------------------
# Ordering of chain should not matter
# ---------------------------------------------------------------------------

def test_chain_order_independent() -> None:
    """Result must be the same regardless of chain list ordering."""
    chain_a = [date(2024, 4, 5), date(2024, 4, 19), date(2024, 5, 17)]
    chain_b = [date(2024, 5, 17), date(2024, 4, 5), date(2024, 4, 19)]
    ref = date(2024, 3, 15)
    rule = NearestToTarget(30)
    assert _r.resolve_with_chain(ref, rule, chain_a) == _r.resolve_with_chain(ref, rule, chain_b)
