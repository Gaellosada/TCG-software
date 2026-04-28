"""Unit tests for DefaultOptionsRoller.next_contract.

Covers:
- AtExpiry due, selector returns valid contract → RollResult with new_contract,
  reason="rolled_at_expiry", error_code=None.
- AtExpiry not due (as_of < expiration) → RollResult(new_contract=None,
  error_code="not_yet_due").
- AtExpiry due, selector returns no_chain_for_date → RollResult(new_contract=None,
  error_code="no_chain_for_date", reason includes "roll_selection_failed").
- NDaysBeforeExpiry → raises NotImplementedError.
- DeltaCross → raises NotImplementedError.

No Mongo, no Module 1, no Module 3 internals.
Selector mocked via AsyncMock.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from tcg.engine.options.roll.roller import DefaultOptionsRoller
from tcg.types.options import (
    AtExpiry,
    ByDelta,
    DeltaCross,
    NDaysBeforeExpiry,
    NextThirdFriday,
    OptionContractDoc,
    OptionDailyRow,
    SelectionResult,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

EXPIRATION = date(2024, 4, 19)
AS_OF_DUE = date(2024, 4, 19)
AS_OF_NOT_YET = date(2024, 4, 18)

CRITERION = ByDelta(target_delta=-0.10)
MATURITY = NextThirdFriday(offset_months=1)


def _make_held(expiration: date = EXPIRATION) -> OptionContractDoc:
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


def _make_new_contract() -> OptionContractDoc:
    """A synthetic replacement contract (May expiry)."""
    return OptionContractDoc(
        collection="OPT_SP_500",
        contract_id="OPT_SP_500_K5010_P_MAY",
        root_underlying="IND_SP_500",
        underlying_ref="FUT_SP_500_EMINI",
        underlying_symbol=None,
        expiration=date(2024, 5, 17),
        expiration_cycle="M",
        strike=5010.0,
        type="P",
        contract_size=None,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _make_roller(selector: AsyncMock) -> DefaultOptionsRoller:
    return DefaultOptionsRoller(selector=selector)


# ---------------------------------------------------------------------------
# Happy-path: roll is due, selector succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_contract_at_expiry_due_selector_succeeds() -> None:
    new_contract = _make_new_contract()
    selector = AsyncMock()
    selector.select.return_value = SelectionResult(
        contract=new_contract,
        matched_value=-0.09,
        error_code=None,
        diagnostic=None,
    )

    roller = _make_roller(selector)
    result = await roller.next_contract(
        held=_make_held(),
        as_of=AS_OF_DUE,
        rule=AtExpiry(),
        criterion_for_new=CRITERION,
        maturity_for_new=MATURITY,
    )

    assert result.new_contract is new_contract
    assert result.roll_date == AS_OF_DUE
    assert result.reason == "rolled_at_expiry"
    assert result.error_code is None

    # Verify selector was called with the right root (held.collection)
    selector.select.assert_awaited_once_with(
        root="OPT_SP_500",
        date=AS_OF_DUE,
        type="P",
        criterion=CRITERION,
        maturity=MATURITY,
    )


# ---------------------------------------------------------------------------
# Not yet due
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_contract_at_expiry_not_yet_due() -> None:
    selector = AsyncMock()
    roller = _make_roller(selector)

    result = await roller.next_contract(
        held=_make_held(expiration=EXPIRATION),
        as_of=AS_OF_NOT_YET,
        rule=AtExpiry(),
        criterion_for_new=CRITERION,
        maturity_for_new=MATURITY,
    )

    assert result.new_contract is None
    assert result.roll_date is None
    assert result.reason == "not_yet_due"
    assert result.error_code == "not_yet_due"

    # Selector must NOT be called when not yet due
    selector.select.assert_not_called()


# ---------------------------------------------------------------------------
# Roll due but selection fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_contract_at_expiry_due_selector_returns_no_chain_for_date() -> None:
    selector = AsyncMock()
    selector.select.return_value = SelectionResult(
        contract=None,
        matched_value=None,
        error_code="no_chain_for_date",
        diagnostic="No chain rows found for OPT_SP_500 on 2024-04-19",
    )

    roller = _make_roller(selector)
    result = await roller.next_contract(
        held=_make_held(),
        as_of=AS_OF_DUE,
        rule=AtExpiry(),
        criterion_for_new=CRITERION,
        maturity_for_new=MATURITY,
    )

    assert result.new_contract is None
    assert result.roll_date is None
    assert result.error_code == "no_chain_for_date"
    assert "roll_selection_failed" in result.reason
    assert "no_chain_for_date" in result.reason


# ---------------------------------------------------------------------------
# Phase-2-only stubs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_contract_n_days_before_expiry_raises_not_implemented() -> None:
    selector = AsyncMock()
    roller = _make_roller(selector)
    with pytest.raises(NotImplementedError):
        await roller.next_contract(
            held=_make_held(),
            as_of=AS_OF_DUE,
            rule=NDaysBeforeExpiry(n=5),
            criterion_for_new=CRITERION,
            maturity_for_new=MATURITY,
        )


@pytest.mark.asyncio
async def test_next_contract_delta_cross_raises_not_implemented() -> None:
    selector = AsyncMock()
    roller = _make_roller(selector)
    with pytest.raises(NotImplementedError):
        await roller.next_contract(
            held=_make_held(),
            as_of=AS_OF_DUE,
            rule=DeltaCross(threshold=0.30),
            criterion_for_new=CRITERION,
            maturity_for_new=MATURITY,
        )
