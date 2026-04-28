"""Pure-helper tests for tcg.engine.options.selection._match.

These tests target the pure matchers without going through the
DefaultOptionsSelector orchestration.  Behavior under test:

- Tie-break convention: lower strike wins (documented in _match.py).
- ByStrike: 1e-9 absolute tolerance for float fuzz.
- ByDelta: stored-only path; tolerance/strict semantics.
"""

from __future__ import annotations

from tcg.engine.options.selection._match import (
    match_by_delta,
    match_by_moneyness,
    match_by_strike,
)
from ._fixtures import make_chain


# ---------------------------------------------------------------------------
# match_by_strike
# ---------------------------------------------------------------------------


def test_match_by_strike_exact() -> None:
    chain = make_chain([(95, None), (100, None), (105, None)])
    out = match_by_strike(chain, 100.0)
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.strike == 100.0
    assert out.matched_value == 100.0


def test_match_by_strike_no_match() -> None:
    chain = make_chain([(95, None), (100, None), (105, None)])
    out = match_by_strike(chain, 110.0)
    assert out.contract is None
    assert out.error_code == "strike_not_in_chain"
    assert out.matched_value is None


def test_match_by_strike_float_fuzz_tolerance() -> None:
    """A 1e-12 difference (storage fuzz) still matches."""
    chain = make_chain([(100.0, None)])
    out = match_by_strike(chain, 100.0 + 1e-12)
    assert out.contract is not None
    assert out.contract.strike == 100.0


# ---------------------------------------------------------------------------
# match_by_delta
# ---------------------------------------------------------------------------


def test_match_by_delta_closest_wins() -> None:
    chain = make_chain([(95, 0.70), (100, 0.50), (105, 0.30), (110, 0.10)])
    deltas = [r.delta_stored for _c, r in chain]
    out = match_by_delta(
        chain, deltas, target=0.30, tolerance=0.05, strict=False, chain_size=4
    )
    assert out.contract is not None
    assert out.contract.strike == 105
    assert out.matched_value == 0.30


def test_match_by_delta_tie_break_lower_strike_wins() -> None:
    """Two rows equidistant from target → lower strike wins."""
    # Target 0.40 is equidistant from 0.50 (K=100) and 0.30 (K=105).
    chain = make_chain([(100, 0.50), (105, 0.30)])
    deltas = [r.delta_stored for _c, r in chain]
    out = match_by_delta(
        chain, deltas, target=0.40, tolerance=0.5, strict=False, chain_size=2
    )
    assert out.contract is not None
    assert out.contract.strike == 100  # lower strike wins on tie
    assert out.matched_value == 0.50


def test_match_by_delta_all_none_returns_missing_no_compute() -> None:
    chain = make_chain([(95, None), (100, None), (105, None)])
    deltas = [r.delta_stored for _c, r in chain]
    out = match_by_delta(
        chain, deltas, target=0.30, tolerance=0.05, strict=False, chain_size=3
    )
    assert out.contract is None
    assert out.error_code == "missing_delta_no_compute"
    assert out.diagnostic is not None
    assert "3" in out.diagnostic  # mentions chain size


def test_match_by_delta_strict_tolerance_miss() -> None:
    """strict=True + closest exceeds tolerance → no_match_within_tolerance."""
    chain = make_chain([(95, 0.70), (100, 0.50)])
    deltas = [r.delta_stored for _c, r in chain]
    out = match_by_delta(
        chain, deltas, target=0.10, tolerance=0.05, strict=True, chain_size=2
    )
    assert out.contract is None
    assert out.error_code == "no_match_within_tolerance"


def test_match_by_delta_lax_tolerance_miss_returns_closest() -> None:
    """strict=False + tolerance miss → closest returned, no error."""
    chain = make_chain([(95, 0.70), (100, 0.50)])
    deltas = [r.delta_stored for _c, r in chain]
    out = match_by_delta(
        chain, deltas, target=0.10, tolerance=0.05, strict=False, chain_size=2
    )
    assert out.error_code is None
    assert out.contract is not None
    assert out.contract.strike == 100  # 0.50 closer to 0.10 than 0.70
    assert out.matched_value == 0.50


def test_match_by_delta_skips_none_in_filtered_chain() -> None:
    """Mix of stored/None — only the non-None rows are considered."""
    chain = make_chain([(95, None), (100, 0.50), (105, None), (110, 0.10)])
    deltas = [r.delta_stored for _c, r in chain]
    out = match_by_delta(
        chain, deltas, target=0.10, tolerance=0.05, strict=False, chain_size=4
    )
    assert out.contract is not None
    assert out.contract.strike == 110  # 0.10 exact
    assert out.matched_value == 0.10


# ---------------------------------------------------------------------------
# match_by_moneyness
# ---------------------------------------------------------------------------


def test_match_by_moneyness_picks_closest_ks() -> None:
    chain = make_chain([(95, None), (100, None), (105, None), (110, None)])
    # Underlying = 100 → K/S = {0.95, 1.00, 1.05, 1.10}
    out = match_by_moneyness(
        rows=chain,
        target_K_over_S=1.02,
        tolerance=0.01,
        underlying_price=100.0,
    )
    assert out.contract is not None
    assert out.contract.strike == 100  # |1.00-1.02|=0.02; |1.05-1.02|=0.03 → 100 wins
    assert out.matched_value == 1.0


def test_match_by_moneyness_tie_break_lower_strike() -> None:
    """K/S 0.95 and 1.05 equidistant from 1.00 → lower strike (K=95) wins."""
    chain = make_chain([(95, None), (105, None)])
    out = match_by_moneyness(
        rows=chain,
        target_K_over_S=1.00,
        tolerance=0.01,
        underlying_price=100.0,
    )
    assert out.contract is not None
    assert out.contract.strike == 95


def test_match_by_moneyness_empty_chain() -> None:
    out = match_by_moneyness(
        rows=[],
        target_K_over_S=1.0,
        tolerance=0.01,
        underlying_price=100.0,
    )
    assert out.contract is None
    assert out.error_code == "no_chain_for_date"
