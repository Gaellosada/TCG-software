"""Public Protocol for Module 3 — options selection.

Spec reference: §3.3 (tcg.engine.options.selection).

Contract:
- ``select`` resolves a (root, date, type, criterion, maturity) request to a
  single ``SelectionResult``.  Three criteria: ``ByDelta``, ``ByMoneyness``,
  ``ByStrike``.  Five maturity rules forwarded to Module 4.
- ``compute_missing_for_delta`` is the API-level opt-in.  When ``True`` AND
  the criterion is ``ByDelta`` AND a candidate row has ``delta_stored is None``,
  the selector calls Module 2 (pricing) to fill delta — otherwise stored-only.

Independence contract: this module must NOT import from ``tcg.data.*``.
The caller injects a ``ChainReaderPort`` (defined in ``_ports.py``).
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from tcg.types.options import (
    MaturitySpec,
    SelectionCriterion,
    SelectionResult,
)


class OptionsSelector(Protocol):
    """Resolve a selection criterion + maturity rule into a single contract."""

    async def select(
        self,
        root: str,
        date: date,
        type: Literal["C", "P"],
        criterion: SelectionCriterion,
        maturity: MaturitySpec,
        compute_missing_for_delta: bool = False,
    ) -> SelectionResult:
        """Resolve the request into a single ``SelectionResult``.

        Parameters
        ----------
        root:
            Collection name, e.g. ``"OPT_SP_500"``.
        date:
            Trade date for the selection.
        type:
            ``"C"`` (call) or ``"P"`` (put).
        criterion:
            One of ``ByDelta``, ``ByMoneyness``, ``ByStrike``.
        maturity:
            One of the five ``MaturityRule`` variants.  ``NearestToTarget``
            is resolved against the chain's available expirations on *date*.
        compute_missing_for_delta:
            When ``True`` AND criterion is ``ByDelta``, fill missing
            ``delta_stored`` rows via the injected pricer (Module 2).
            Otherwise stored-only delta.

        Returns
        -------
        SelectionResult
            ``contract=None`` and ``error_code`` set on failure paths.
        """
        ...
