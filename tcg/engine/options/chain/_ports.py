"""Local Protocol ports for Module 6 — chain.

Why this exists
---------------
The import-linter ``engine-data-isolation`` independence contract forbids
``tcg.engine.options.chain`` from importing ``tcg.data.*`` at module
import time.  We therefore define here the *minimal* duck-typed shapes
that Module 6 needs at runtime, by Protocol, without referencing
``tcg.data.options.protocol`` or ``tcg.data._mongo.instruments``.

Any object that structurally satisfies these Protocols can be injected
into ``DefaultOptionsChain``.  The API router (Wave B4) constructs
concrete adapters wiring ``MongoOptionsDataReader`` and
``MongoInstrumentReader`` to these ports.  Those wires live in
``tcg.core``, where crossing the boundary is allowed.

Spec / guardrail references
---------------------------
- Spec §3.6 (Module 6 Protocol).
- Guardrail #8 (mirror TCG conventions; engine ⊥ data).
- ORDERS.md: "No ``from tcg.data.*`` imports in
  ``tcg/engine/options/chain/*``."
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, runtime_checkable

from tcg.types.options import OptionContractDoc, OptionDailyRow


@runtime_checkable
class OptionsDataPort(Protocol):
    """Minimal shape Module 6 needs from a chain reader.

    Mirrors ``tcg.data.options.protocol.OptionsDataReader.query_chain``
    by structural typing, without importing it.  Any object whose
    ``query_chain`` is shape-compatible satisfies this Protocol.
    """

    async def query_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        """Return ``(contract, row)`` pairs traded on *date* in the window.

        ``expiration_cycle`` (optional) drops rows whose cycle does not
        match — used by the smile UI to disambiguate same-date overlap.
        """
        ...


@runtime_checkable
class IndexDataPort(Protocol):
    """Minimal shape needed to read a single value from an INDEX document.

    Used by the OPT_VIX branch of the underlying-price resolver: query
    the INDEX collection's ``IND_VIX`` doc, find the row matching the
    target date, return its value.  Returns ``None`` when the doc or row
    is missing — the caller surfaces that as ``K_over_S = None``.
    """

    async def get_index_value_on_date(
        self,
        index_id: str,
        target_date: date,
    ) -> float | None:
        """Return the index value on *target_date* or ``None`` on miss."""
        ...


@runtime_checkable
class FuturesDataPort(Protocol):
    """Minimal shape needed to read a single futures close.

    Used by the option-on-future branch of the underlying-price resolver:
    query the FUT_* collection per ``OptionContractDoc.underlying_ref``,
    find the row matching the target date, return ``eodDatas.close``.
    Returns ``None`` when the doc or row is missing.

    Note on the ``collection`` parameter: the resolver derives the
    correct FUT_* collection name from ``contract.collection`` (e.g.
    ``OPT_SP_500`` → ``FUT_SP_500``).  The adapter wired in
    ``tcg.core`` performs the actual Mongo query.
    """

    async def get_futures_close_on_date(
        self,
        collection: str,
        contract_ref: str,
        target_date: date,
    ) -> float | None:
        """Return the futures close on *target_date* or ``None`` on miss."""
        ...

    async def get_futures_close_by_expiration(
        self,
        collection: str,
        expiration: date,
        target_date: date,
    ) -> float | None:
        """Return the close on ``target_date`` of the FUT_* contract whose
        ``expiration`` field matches the option's expiration date.

        Used by the OPT_VIX branch of the underlying-price resolver to find
        the matching monthly VIX future (the legacy Mongo schema stores
        ``expiration`` as a YYYYMMDD int on each FUT_VIX document; the
        adapter is responsible for translating ``expiration: date`` to the
        right query). Returns ``None`` when:

        - no FUT_* contract has a matching expiration (i.e. the option is
          weekly — Phase 3 will introduce a forward-curve interpolator), or
        - the matching contract has no row for ``target_date``.

        The caller (``_join.resolve_underlying_price``) propagates the
        ``None`` up to ``DefaultOptionsPricer.compute`` which surfaces
        ``error_code="missing_forward_vix_curve"``.
        """
        ...
