"""Module 5 implementation — DefaultOptionsRoller.

Spec reference: §3.5 (tcg.engine.options.roll).

Phase 1 scope
-------------
Only ``AtExpiry`` is fully implemented.  ``NDaysBeforeExpiry`` and
``DeltaCross`` raise ``NotImplementedError("phase_2_only: ...")`` — they are
reserved for Phase 2 without prejudice to the Protocol shape.

Dependency pattern
------------------
The constructor accepts an ``OptionsSelector`` (Protocol from Module 3).
Module 3 already abstracts the ``tcg.data`` boundary through its own ports,
so Module 5 does NOT need its own ``_ports.py``.  The coupling is to the
*shape* of Module 3, not to any concrete implementation — do NOT import
``DefaultOptionsSelector``.

Independence contract verified by::

    grep -rE "^from tcg\\.data" tcg/engine/options/roll/    # must be empty
    grep -r "from tcg.engine.options.selection.selector"    # must be empty
"""

from __future__ import annotations

from datetime import date

from tcg.engine.options.selection.protocol import OptionsSelector
from tcg.types.options import (
    AtExpiry,
    DeltaCross,
    MaturityRule,
    NDaysBeforeExpiry,
    OptionContractDoc,
    OptionDailyRow,
    RollResult,
    RollRule,
    SelectionCriterion,
)


class DefaultOptionsRoller:
    """Concrete implementation of the ``OptionsRoller`` Protocol.

    Parameters
    ----------
    selector:
        An object satisfying ``OptionsSelector`` (Module 3 Protocol).
        Injected at construction; never imported as a concrete class here.
    """

    def __init__(self, selector: OptionsSelector) -> None:
        self._selector = selector

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_roll(
        self,
        held: OptionContractDoc,
        held_row: OptionDailyRow,
        as_of: date,
        rule: RollRule,
    ) -> bool:
        """Return True when the roll condition is satisfied.

        AtExpiry semantics: roll on or after ``held.expiration``.
        """
        if isinstance(rule, AtExpiry):
            return as_of >= held.expiration
        if isinstance(rule, NDaysBeforeExpiry):
            raise NotImplementedError(
                "phase_2_only: NDaysBeforeExpiry roll rule reserved for Phase 2"
            )
        if isinstance(rule, DeltaCross):
            raise NotImplementedError(
                "phase_2_only: DeltaCross roll rule reserved for Phase 2"
            )
        raise ValueError(f"Unknown roll rule type: {type(rule).__name__}")

    async def next_contract(
        self,
        held: OptionContractDoc,
        as_of: date,
        rule: RollRule,
        criterion_for_new: SelectionCriterion,
        maturity_for_new: MaturityRule,
    ) -> RollResult:
        """Check roll condition and, if due, select the replacement contract.

        Root extraction: ``held.collection`` is used as the root passed to
        Module 3 (e.g. ``"OPT_SP_500"``).  This matches what Module 1's
        ``query_chain`` expects as the *root* parameter.

        Phase 1 dispatch on rule type:

        - ``AtExpiry``: check ``as_of >= held.expiration``; if not due return
          ``not_yet_due``; if due call ``selector.select`` and wrap result.
        - ``NDaysBeforeExpiry`` / ``DeltaCross``: raise ``NotImplementedError``.
        """
        if isinstance(rule, NDaysBeforeExpiry) or isinstance(rule, DeltaCross):
            raise NotImplementedError("phase_2_only")

        if isinstance(rule, AtExpiry):
            return await self._roll_at_expiry(
                held=held,
                as_of=as_of,
                criterion_for_new=criterion_for_new,
                maturity_for_new=maturity_for_new,
            )

        raise ValueError(f"Unknown roll rule type: {type(rule).__name__}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _roll_at_expiry(
        self,
        held: OptionContractDoc,
        as_of: date,
        criterion_for_new: SelectionCriterion,
        maturity_for_new: MaturityRule,
    ) -> RollResult:
        """Inner handler for AtExpiry — extracted for readability."""
        if as_of < held.expiration:
            return RollResult(
                new_contract=None,
                roll_date=None,
                reason="not_yet_due",
                error_code="not_yet_due",
            )

        # Roll is due — invoke Module 3 to select the replacement.
        result = await self._selector.select(
            root=held.collection,
            date=as_of,
            type=held.type,
            criterion=criterion_for_new,
            maturity=maturity_for_new,
        )

        if result.contract is not None:
            return RollResult(
                new_contract=result.contract,
                roll_date=as_of,
                reason="rolled_at_expiry",
                error_code=None,
            )

        # Selection failed — propagate error_code from Module 3.
        return RollResult(
            new_contract=None,
            roll_date=None,
            reason=f"roll_selection_failed: {result.error_code}",
            error_code=result.error_code,
        )
