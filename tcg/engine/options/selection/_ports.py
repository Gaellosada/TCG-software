"""Local Protocol ports for Module 3 — selection.

Why this exists
---------------
The import-linter ``engine-data-isolation`` independence contract forbids
``tcg.engine.options.selection`` from importing ``tcg.data.*`` at module
import time.  We therefore define here the *minimal* duck-typed shapes
that Module 3 needs at runtime, by Protocol, without referencing
``tcg.data.options.protocol``.

Any object that structurally satisfies these Protocols can be injected
into ``DefaultOptionsSelector``.  The API router (Wave B4) wires the
real ``OptionsDataReader`` and the ``MarketDataService`` underlying-join
helper into Module 3 — those wires live in ``tcg.core``, where crossing
the boundary is allowed.

Spec / guardrail references
---------------------------
- Spec §3.3 (Module 3 Protocol).
- Guardrail #8 (mirror TCG conventions; engine ⊥ data).
- ORDERS.md: "Module 3 imports the Module 1 Protocol by Protocol shape;
  the construct-time injection means ``tcg.engine.options.selection``
  only references protocols defined here."
"""

from __future__ import annotations

from datetime import date
from typing import Awaitable, Callable, Literal, Protocol, Sequence, runtime_checkable

from tcg.types.options import (
    OptionContractDoc,
    OptionDailyRow,
)


@runtime_checkable
class ChainReaderPort(Protocol):
    """Minimal shape Module 3 needs from a chain reader.

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
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        """Return ``(contract, row)`` pairs traded on *date* in the window."""
        ...


@runtime_checkable
class BulkChainReaderPort(Protocol):
    """Minimal shape for bulk chain queries across multiple dates.

    Mirrors ``OptionsDataReader.query_chain_bulk`` by structural typing.
    Unlike ``ChainReaderPort``, this returns rows for ALL requested dates
    in a single cursor pass.  Does NOT include ``expiration_cycle`` —
    cycle injection is handled by ``_CycleInjectingBulkReader`` in the
    stream resolver layer.
    """

    async def query_chain_bulk(
        self,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Return ``(contract, row)`` pairs for ALL *dates* in one pass."""
        ...


# Canonical type alias for the optional underlying-price resolver injected
# at construction time.  Returns ``None`` when the join cannot be made
# (caller surfaces ``error_code="missing_underlying_price"``).
#
# Phase 1B: Module 3 only needs this for ``ByMoneyness`` (and for the
# optional Module-2 compute path on ``ByDelta``).  Module 6 (chain) owns
# the canonical resolver in production wiring; the API layer wires both
# modules to the same callable.
UnderlyingPriceResolver = Callable[[OptionContractDoc, date], Awaitable[float | None]]
