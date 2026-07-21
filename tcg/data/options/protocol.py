"""Public Protocol for the options data reader (Module 1, Phase 1B).

Module 1 surfaces stored OPT_* documents only. It NEVER calls Module 2
(``tcg.engine.options.pricing``) — guardrail #2. Computation is opt-in
and originates from callers above this layer.

Spec reference: ``OPTIONS_FEATURE_SPEC.md`` §3.1.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Mapping, Protocol, Sequence

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

    async def query_chain_bulk_multi(
        self,
        root: str,
        type: Literal["C", "P", "both"],
        groups: Sequence[tuple[date, Sequence[date]]],
        strike_windows: "Mapping[date, tuple[float | None, float | None]] | None" = None,
        expiration_cycle: str | Sequence[str] | None = None,
        delta_pushdown: "tuple[float, int] | None" = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Multi-EXPIRATION bulk chain fetch in ONE query (year-chunk fast path).

        OPTIONAL capability: collapses the per-expiration ``query_chain_bulk``
        fan-out into a single round-trip covering several expirations, each
        restricted to its OWN trade-date window (``groups`` =
        ``[(expiration, [trade_dates...]), ...]``).  Same ``(contract, row)``
        semantics as ``query_chain_bulk`` per (expiration, trade_date), MINUS the
        strike window when ``strike_windows`` is None (a strict superset).
        Callers must feature-detect (``hasattr``) and fall back to
        ``query_chain_bulk`` when a reader does not implement it.

        ``delta_pushdown`` (a ``(target_delta, k)`` tuple, optional) engages the
        single-read DELTA PUSHDOWN: the greeks fact is ranked per (expiration,
        trade_date) by ``|delta - target|`` (tie-break lower strike) and only the
        top-``k`` candidates per group are returned — the ROW SHAPE is unchanged,
        so ``match_by_delta`` picks the same contract (``rn=1`` IS its winner;
        NULL deltas sort LAST so they never displace it, and an all-NULL chain
        preserves the ``missing_delta_no_compute`` classification).  Correct only
        for STORED-delta ``ByDelta`` selection; mutually exclusive with
        ``strike_windows``.
        """
        ...

    async def query_held_rows(
        self,
        root: str,
        type: Literal["C", "P", "both"],
        held_windows: Sequence[tuple[str, date, date]],
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Identity keyset fetch of specific HELD option SYMBOLS (Phase 2).

        OPTIONAL capability (hold-leg two-phase pushdown).  ``held_windows`` is
        ``[(symbol, lo, hi), ...]`` — each ALREADY-SELECTED frozen contract's
        ``symbol`` (== ``OptionContractDoc.contract_id``) and its held date-range;
        ``hi`` must include the next roll date (the resolver reads the OLD
        contract's mid on the roll seam).  Returns every physical row of each
        symbol over its window, keyed by fact ``trade_date``.  SQL never ranks or
        picks — selection stays in Python.  Callers must feature-detect
        (``callable(getattr(reader, "query_held_rows", None))``) and fall back to
        the full-chain hold path when a reader does not implement it.

        ``expiration_cycle`` MUST be the SAME cycle the full-chain path filters on
        (the wiring layer injects the caller's cycle): a symbol is NOT unique
        across cycles — the ~2.68% duplicate-``instrument_id`` quirk is one symbol
        double-tagged (e.g. ``"M"`` + ``"W3 Friday"``) with different quotes, and
        only the matching-cycle sibling must survive so ``_row_for_contract``'s
        first-by-``instrument_id`` pick is byte-identical to the full-chain path.
        ``None`` = all cycles.
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
