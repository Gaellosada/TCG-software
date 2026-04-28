"""Public Protocol for Module 5 — roll logic.

Spec reference: §3.5 (tcg.engine.options.roll).

Contract
--------
- ``should_roll`` evaluates whether the held contract should be rolled on
  *as_of* given *rule*.  Returns a plain ``bool``.  For ``AtExpiry``, rolls
  on or after ``held.expiration``.  For Phase-2 rules raises
  ``NotImplementedError("phase_2_only: ...")``.
- ``next_contract`` is the full round-trip: check roll condition → invoke
  Module 3 (selection) → return ``RollResult``.

Independence contract
---------------------
Module 5 depends only on Module 3's ``OptionsSelector`` Protocol (defined in
``tcg.engine.options.selection.protocol``).  It does NOT import from
``tcg.data.*``; the data boundary is already abstracted by Module 3's own
ports.  No ``_ports.py`` file is needed here — Module 3 is the engine-level
abstraction.

Do NOT type the constructor against ``DefaultOptionsSelector`` (the concrete
implementation).  Type it against ``OptionsSelector`` (this Protocol's peer).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from tcg.types.options import (
    MaturityRule,
    OptionContractDoc,
    OptionDailyRow,
    RollResult,
    RollRule,
    SelectionCriterion,
)


class OptionsRoller(Protocol):
    """Evaluate roll conditions and produce the next contract.

    Phase 1 implements ``AtExpiry`` only.  ``NDaysBeforeExpiry`` and
    ``DeltaCross`` raise ``NotImplementedError("phase_2_only: ...")``.
    """

    async def next_contract(
        self,
        held: OptionContractDoc,
        as_of: date,
        rule: RollRule,
        criterion_for_new: SelectionCriterion,
        maturity_for_new: MaturityRule,
    ) -> RollResult:
        """Determine whether to roll and, if so, select the replacement.

        Parameters
        ----------
        held:
            The currently-held option contract.
        as_of:
            The evaluation date (today or signal date).
        rule:
            Roll rule to apply.  ``AtExpiry`` is the only Phase-1 rule.
        criterion_for_new:
            Selection criterion forwarded to Module 3 for the replacement.
        maturity_for_new:
            Maturity rule forwarded to Module 3 for the replacement.

        Returns
        -------
        RollResult
            ``new_contract=None`` and ``error_code="not_yet_due"`` when roll
            is not triggered.  ``new_contract`` set and ``error_code=None``
            on success.  ``new_contract=None`` and ``error_code`` from
            Module 3 on selection failure.

        Raises
        ------
        NotImplementedError
            For ``NDaysBeforeExpiry`` and ``DeltaCross`` (Phase 2 only).
        """
        ...

    def should_roll(
        self,
        held: OptionContractDoc,
        held_row: OptionDailyRow,
        as_of: date,
        rule: RollRule,
    ) -> bool:
        """Return True when the roll condition is satisfied.

        Parameters
        ----------
        held:
            The currently-held option contract.
        held_row:
            The daily row for *held* on *as_of*.  Required for ``DeltaCross``
            (Phase 2).  Not used by ``AtExpiry``.
        as_of:
            Evaluation date.
        rule:
            Roll rule to apply.

        Returns
        -------
        bool
            ``True`` iff the contract should be rolled today.

        Raises
        ------
        NotImplementedError
            For ``NDaysBeforeExpiry`` and ``DeltaCross`` (Phase 2 only).
        ValueError
            For unrecognised rule types.
        """
        ...
