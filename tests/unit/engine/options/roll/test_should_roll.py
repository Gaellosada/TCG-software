"""Unit tests for DefaultOptionsRoller.should_roll.

Covers:
- AtExpiry: as_of < expiration → False
- AtExpiry: as_of == expiration → True
- AtExpiry: as_of > expiration → True
- NDaysBeforeExpiry → NotImplementedError("phase_2_only: ...")
- DeltaCross → NotImplementedError("phase_2_only: ...")

No Mongo, no Module 1, no Module 3 internals.
"""

from __future__ import annotations

from datetime import date
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
# Phase-2-only stubs
# ---------------------------------------------------------------------------


def test_should_roll_n_days_before_expiry_raises_not_implemented() -> None:
    roller = _make_roller()
    held = _make_contract()
    row = _make_row()
    with pytest.raises(NotImplementedError, match="phase_2_only"):
        roller.should_roll(held, row, as_of=date(2024, 4, 15), rule=NDaysBeforeExpiry(n=5))


def test_should_roll_delta_cross_raises_not_implemented() -> None:
    roller = _make_roller()
    held = _make_contract()
    row = _make_row()
    with pytest.raises(NotImplementedError, match="phase_2_only"):
        roller.should_roll(held, row, as_of=date(2024, 4, 15), rule=DeltaCross(threshold=0.30))
