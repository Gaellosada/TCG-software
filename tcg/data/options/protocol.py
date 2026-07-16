"""Public Protocol for the options data reader (Module 1, Phase 1B).

Module 1 surfaces stored OPT_* documents only. It NEVER calls Module 2
(``tcg.engine.options.pricing``) — guardrail #2. Computation is opt-in
and originates from callers above this layer.

Spec reference: ``OPTIONS_FEATURE_SPEC.md`` §3.1.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, Sequence

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
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        """Return one (contract, row) pair per option active on *date*.

        Filters ``type`` and ``strike`` client-side (Mongo lacks the
        compound index — see ``DB_SCHEMA_FINDINGS`` §5). Rows whose
        ``eodDatas[provider]`` does not contain *date* are skipped, so the
        returned length is the count of contracts that traded that day.

        ``expiration_cycle`` (when non-None) filters out contracts whose
        ``expiration_cycle`` (e.g. "M" / "W" / "D" / "Q") does not match.
        Used by the smile UI to collapse multi-cycle overlap on roots
        such as OPT_SP_500 to a single trace per strike.
        """
        ...

    async def query_chain_bulk(
        self,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Return ``(contract, row)`` pairs for ALL *dates* in one cursor pass.

        Same server-side filters as ``query_chain`` (expiration range, type,
        cycle), but materialises rows for every date in *dates* rather than
        a single target date.  Avoids N separate cursor iterations when the
        caller needs the same chain across many dates.
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

    async def get_option_root_symbol(self, root: str) -> str | None:
        """The ``dim_instrument.root_symbol`` of *root* (a ``source_collection``).

        A single indexed, fact-free ``LIMIT 1`` dim lookup returning the value
        that :meth:`query_chain`/:meth:`query_chain_bulk` would place on every
        contract's ``OptionContractDoc.root_underlying`` for this collection
        (they are constant across the collection). Used by the stream resolver to
        synthesise the underlying-price-resolver's routing contract WITHOUT a
        full-chain probe fetch. ``None`` when the collection has no option
        contract or the column is NULL.
        """
        ...

    async def trade_date_coverage(self, root: str) -> tuple[date | None, date | None]:
        """``(first_trade_date, last_trade_date)`` bar coverage for *root*.

        Backs the portfolio date-slider floor so an option-only portfolio
        defaults to the option collection's TRUE history. Either bound is
        ``None`` when the root has no usable contract.
        """
        ...

    async def list_expirations_filtered(
        self,
        root: str,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
    ) -> list[date]:
        """Distinct expirations filtered by type and/or cycle."""
        ...

    async def list_expirations_by_date(
        self,
        root: str,
        start: date,
        end: date,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
        expiration_max: date | None = None,
    ) -> dict[date, list[date]]:
        """Per-trade-date map of expirations actually LISTED (price-quoted).

        ``{trade_date: [expirations listed that day]}`` — a price-row join, not
        the dim-only global set.  Consumed by the stream resolver so
        ``NearestToTarget`` snaps to an expiration listed on each date (fixes the
        daily-expiration ``no_chain_for_date`` global-snap bug).

        ``expiration_max`` (optional) caps the expirations considered, bounding
        the LEAPS scan; ``None`` = no upper bound.
        """
        ...
