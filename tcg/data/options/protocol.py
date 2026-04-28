"""Public Protocol for the options data reader (Module 1, Phase 1B).

Module 1 surfaces stored OPT_* documents only. It NEVER calls Module 2
(``tcg.engine.options.pricing``) — guardrail #2. Computation is opt-in
and originates from callers above this layer.

Spec reference: ``OPTIONS_FEATURE_SPEC.md`` §3.1.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)


class OptionsDataReader(Protocol):
    """Read-only Mongo access to OPT_* collections.

    Implementations must:

    - Surface ``mid = (bid + ask) / 2`` only when both quotes are present
      and positive; otherwise ``mid = None``. ``close`` is never used as
      the primary mark.
    - Normalize ``type`` to upper-case ``"C"`` / ``"P"`` (OPT_VIX stores
      mixed case).
    - Never surface stored ``atTheMoney`` / ``moneyness`` / ``daysToExpiry``
      (guardrail #3 — semantics ambiguous; recompute fresh downstream).
    - Pick a single provider per root (see ``_provider.py``) and expose
      the choice on ``OptionContractDoc.provider``.
    - Wrap any ``PyMongoError`` as ``OptionsDataAccessError``.
    """

    async def get_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        """Return a single contract with its full chronological day series.

        ``contract_id`` is the composite ``"<internalSymbol>|<expirationCycle>"``
        produced by :func:`tcg.data._mongo.helpers.serialize_doc_id`.

        Raises
        ------
        OptionsContractNotFound
            When the document does not exist in *collection*.
        OptionsDataAccessError
            On any underlying Mongo failure.
        """
        ...

    async def query_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        """Return one (contract, row) pair per option active on *date*.

        Filters ``type`` and ``strike`` client-side (Mongo lacks the
        compound index — see ``DB_SCHEMA_FINDINGS`` §5). Rows whose
        ``eodDatas[provider]`` does not contain *date* are skipped, so the
        returned length is the count of contracts that traded that day.
        """
        ...

    async def list_roots(self) -> list[OptionRootInfo]:
        """List every OPT_* collection with display metadata.

        ``has_greeks`` reflects the presence of ``eodGreeks`` on the
        chosen provider for the root (None for OPT_VIX / OPT_ETH).
        """
        ...

    async def list_expirations(self, root: str) -> list[date]:
        """Distinct expirations available on *root*, sorted ascending.

        Used by the chain / smile UIs to constrain the user-facing date
        pickers to dates that actually have contracts.
        """
        ...
