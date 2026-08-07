"""Unit tests for DefaultOptionsRoller.should_roll.

Covers:
- AtExpiry: as_of < expiration → False
- AtExpiry: as_of == expiration → True
- AtExpiry: as_of > expiration → True
- NDaysBeforeExpiry: TRADING-day boundary (weekend/holiday-aware), n=0
  boundary == AtExpiry, n larger than the lookback window (clamp), n < 0
  (ValueError)
- DeltaCross → NotImplementedError("phase_2_only: ...")

No Mongo, no Module 1, no Module 3 internals.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from tcg.engine.options.roll.roller import DefaultOptionsRoller
from tcg.types.options import (
    AtExpiry,
    DeltaCross,
    NDaysBeforeExpiry,
    OptionContractDoc,
    OptionDailyRow,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

EXPIRATION = date(2024, 4, 19)


def _make_contract(expiration: date = EXPIRATION) -> OptionContractDoc:
    return OptionContractDoc(
        collection="OPT_SP_500",
        contract_id="OPT_SP_500_K5000_P",
        root_underlying="IND_SP_500",
        underlying_ref="FUT_SP_500_EMINI",
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle="M",
        strike=5000.0,
        type="P",
        contract_size=None,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _make_row(row_date: date = date(2024, 4, 19)) -> OptionDailyRow:
    return OptionDailyRow(
        date=row_date,
        open=None,
        high=None,
        low=None,
        close=None,
        bid=1.0,
        ask=1.1,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=None,
        mid=1.05,
        iv_stored=None,
        delta_stored=-0.10,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=None,
    )


def _make_roller() -> DefaultOptionsRoller:
    """Roller with a stub selector — should_roll doesn't call selector."""
    selector = AsyncMock()
    return DefaultOptionsRoller(selector=selector)


# ---------------------------------------------------------------------------
# AtExpiry tests
# ---------------------------------------------------------------------------


def test_should_roll_at_expiry_before_expiration_returns_false() -> None:
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()
    result = roller.should_roll(held, row, as_of=date(2024, 4, 18), rule=AtExpiry())
    assert result is False


def test_should_roll_at_expiry_on_expiration_date_returns_true() -> None:
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()
    result = roller.should_roll(held, row, as_of=date(2024, 4, 19), rule=AtExpiry())
    assert result is True


def test_should_roll_at_expiry_after_expiration_date_returns_true() -> None:
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()
    result = roller.should_roll(held, row, as_of=date(2024, 4, 20), rule=AtExpiry())
    assert result is True


# ---------------------------------------------------------------------------
# NDaysBeforeExpiry — TRADING days (SPEC §5.5/§5.6, resolved over the
# dataclass's now-fixed docstring: screenshots win per SPEC §0).
#
# EXPIRATION (2024-04-19) is a Friday with no CME_TradeDate holidays in the
# surrounding weeks, so plain n=2 (Fri -> Wed) does not by itself distinguish
# trading-day counting from naive calendar-day counting. The cases below
# specifically pick expirations that cross a weekend and a market holiday
# (Good Friday, 2024-03-29) so a calendar-day implementation would disagree
# with the assertions.
# ---------------------------------------------------------------------------


def test_should_roll_n_days_before_expiry_n2_before_trigger_returns_false() -> None:
    """EXPIRATION=2024-04-19 (Fri), n=2 trading days before = 2024-04-17 (Wed)."""
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()
    result = roller.should_roll(
        held, row, as_of=date(2024, 4, 16), rule=NDaysBeforeExpiry(n=2)
    )
    assert result is False


def test_should_roll_n_days_before_expiry_n2_on_trigger_returns_true() -> None:
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()
    result = roller.should_roll(
        held, row, as_of=date(2024, 4, 17), rule=NDaysBeforeExpiry(n=2)
    )
    assert result is True


def test_should_roll_n_days_before_expiry_n0_matches_at_expiry_boundary() -> None:
    """n=0 must reproduce AtExpiry's exact boundary."""
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()

    before = roller.should_roll(
        held, row, as_of=date(2024, 4, 18), rule=NDaysBeforeExpiry(n=0)
    )
    on = roller.should_roll(
        held, row, as_of=date(2024, 4, 19), rule=NDaysBeforeExpiry(n=0)
    )
    after = roller.should_roll(
        held, row, as_of=date(2024, 4, 20), rule=NDaysBeforeExpiry(n=0)
    )

    assert before is False
    assert on is True
    assert after is True

    # Cross-check directly against AtExpiry for the same dates.
    for as_of in (date(2024, 4, 18), date(2024, 4, 19), date(2024, 4, 20)):
        assert roller.should_roll(held, row, as_of=as_of, rule=NDaysBeforeExpiry(n=0)) == (
            roller.should_roll(held, row, as_of=as_of, rule=AtExpiry())
        )


def test_should_roll_n_days_before_expiry_crosses_weekend_trading_days_not_calendar() -> None:
    """expiration=2024-04-22 (Mon), n=1 trading day before = 2024-04-19 (Fri).

    A naive calendar-day subtraction would give 2024-04-21 (Sunday) — this
    test fails under that (wrong) implementation and passes under the
    trading-day-aware one.
    """
    roller = _make_roller()
    held = _make_contract(expiration=date(2024, 4, 22))
    row = _make_row()

    # Thursday 4/18: before the trading-day trigger (Friday 4/19) → False.
    assert roller.should_roll(
        held, row, as_of=date(2024, 4, 18), rule=NDaysBeforeExpiry(n=1)
    ) is False
    # Friday 4/19: exactly the trigger → True.
    assert roller.should_roll(
        held, row, as_of=date(2024, 4, 19), rule=NDaysBeforeExpiry(n=1)
    ) is True
    # Saturday/Sunday (non-trading, but as_of is a plain date comparison):
    # already on/after the trigger → True. A calendar-day implementation
    # would put the trigger on Sunday 4/21, disagreeing on 4/19 and 4/20.
    assert roller.should_roll(
        held, row, as_of=date(2024, 4, 20), rule=NDaysBeforeExpiry(n=1)
    ) is True


def test_should_roll_n_days_before_expiry_crosses_good_friday_holiday() -> None:
    """expiration=2024-04-01 (Mon), n=1 trading day before = 2024-03-28 (Thu).

    2024-03-29 (Fri) is Good Friday, a CME_TradeDate holiday, and 3/30-3/31
    is a weekend — so the previous TRADING day before 4/1 is Thursday 3/28,
    not Sunday 3/31 (naive calendar-day count) and not Friday 3/29 (a
    calendar business day count that ignores the market holiday).
    """
    roller = _make_roller()
    held = _make_contract(expiration=date(2024, 4, 1))
    row = _make_row()

    assert roller.should_roll(
        held, row, as_of=date(2024, 3, 27), rule=NDaysBeforeExpiry(n=1)
    ) is False
    assert roller.should_roll(
        held, row, as_of=date(2024, 3, 28), rule=NDaysBeforeExpiry(n=1)
    ) is True


def test_should_roll_n_days_before_expiry_huge_n_clamps_within_bounded_window() -> None:
    """n far exceeding the bounded lookback window (1000 calendar days)
    clamps to the earliest trading day found in that window, rather than
    raising or scanning an unbounded calendar (documented in
    ``_n_trading_days_before``).
    """
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()
    huge_rule = NDaysBeforeExpiry(n=10_000)

    # 500 calendar days before expiry is comfortably inside the 1000-day
    # window → past the clamped trigger → due.
    assert roller.should_roll(
        held, row, as_of=EXPIRATION - timedelta(days=500), rule=huge_rule
    ) is True
    # 1500 calendar days before expiry is outside the window entirely →
    # not yet due (proves the clamp is bounded, not "always True").
    assert roller.should_roll(
        held, row, as_of=EXPIRATION - timedelta(days=1500), rule=huge_rule
    ) is False


def test_should_roll_n_days_before_expiry_negative_n_raises_value_error() -> None:
    roller = _make_roller()
    held = _make_contract(expiration=EXPIRATION)
    row = _make_row()
    with pytest.raises(ValueError, match="NDaysBeforeExpiry.n must be >= 0"):
        roller.should_roll(held, row, as_of=EXPIRATION, rule=NDaysBeforeExpiry(n=-1))


# ---------------------------------------------------------------------------
# Phase-2-only stubs
# ---------------------------------------------------------------------------


def test_should_roll_delta_cross_raises_not_implemented() -> None:
    roller = _make_roller()
    held = _make_contract()
    row = _make_row()
    with pytest.raises(NotImplementedError, match="phase_2_only"):
        roller.should_roll(held, row, as_of=date(2024, 4, 15), rule=DeltaCross(threshold=0.30))


def test_n_trading_days_before_is_memoized_identical_decisions() -> None:
    """A6a: ``_n_trading_days_before`` is memoized so repeated ``should_roll``
    calls during a backtest do not recompute ``cal.valid_days``. Caching must be
    transparent — identical inputs give identical outputs, and the memoized value
    matches a fresh (uncached) recomputation.
    """
    from tcg.engine.options.roll.roller import _n_trading_days_before

    _n_trading_days_before.cache_clear()
    exp = date(2024, 4, 22)  # Monday; n=1 trading day before = Fri 2024-04-19
    first = _n_trading_days_before(exp, 1)
    assert first == date(2024, 4, 19)
    # Recompute uncached (bypass the wrapper) and confirm agreement.
    fresh = _n_trading_days_before.__wrapped__(exp, 1)
    assert fresh == first
    # Repeat calls hit the cache rather than recompute.
    before = _n_trading_days_before.cache_info()
    for _ in range(5):
        assert _n_trading_days_before(exp, 1) == first
    after = _n_trading_days_before.cache_info()
    assert after.hits - before.hits == 5
