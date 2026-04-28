"""Public Protocol for Module 4 — maturity rule → target expiration date.

Spec reference: §3.4 (tcg.engine.options.maturity).

Contract:
- ``resolve`` handles the four non-chain rules: NextThirdFriday, EndOfMonth,
  PlusNDays, FixedDate.  Raises ``ValueError`` when called with NearestToTarget.
- ``resolve_with_chain`` handles NearestToTarget by selecting from a caller-
  supplied list of available expirations.  Returns ``None`` when the list is
  empty.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from tcg.types.options import MaturityRule, NearestToTarget


@runtime_checkable
class MaturityResolver(Protocol):
    """Pure resolver: ref_date + rule → target expiration date.

    Implementations must be stateless and deterministic.  Calendar instances
    may be cached internally but must not carry mutable state between calls.
    """

    def resolve(
        self,
        ref_date: date,
        rule: MaturityRule,
        calendar: str = "CME",
    ) -> date:
        """Resolve a maturity rule to a concrete expiration date.

        Parameters
        ----------
        ref_date:
            Reference date (today or signal date).
        rule:
            One of NextThirdFriday | EndOfMonth | PlusNDays | FixedDate.
            Passing NearestToTarget raises ValueError — use resolve_with_chain.
        calendar:
            pandas_market_calendars calendar name.  Defaults to ``"CME"``
            (mapped internally to ``"CME_TradeDate"`` because the library
            does not register a bare ``"CME"`` alias at version 5.x+).

        Returns
        -------
        date
            The resolved target expiration date.

        Raises
        ------
        ValueError
            If ``rule`` is NearestToTarget (requires available_expirations).
        """
        ...

    def resolve_with_chain(
        self,
        ref_date: date,
        rule: NearestToTarget,
        available_expirations: list[date],
    ) -> date | None:
        """Select the expiration nearest to ``ref_date + rule.target_dte_days``.

        Parameters
        ----------
        ref_date:
            Reference date.
        rule:
            NearestToTarget with the desired DTE in calendar days.
        available_expirations:
            Caller-supplied list of available contract expirations.

        Returns
        -------
        date | None
            The nearest expiration, or ``None`` if ``available_expirations``
            is empty.  Tie-break: lower DTE wins (per spec §3.4).
        """
        ...
