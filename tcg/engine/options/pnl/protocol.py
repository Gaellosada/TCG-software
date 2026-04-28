"""Public Protocol for Module 7 — per-contract daily P&L replay.

Module 7 dependency graph
-------------------------
Module 7 depends on Module 1 (``tcg.data.options``) at runtime via the
``OptionsDataPort`` local Port defined here.  The import-linter
``engine-data-isolation`` contract forbids ``tcg.engine.*`` from importing
``tcg.data.*`` at module import time.  We therefore mirror the one method we
need (``get_contract``) as a duck-typed local Protocol, exactly as Module 3
does in ``tcg.engine.options.selection._ports``.

The API router (Wave B4) or any wiring layer in ``tcg.core`` injects a real
``OptionsDataReader`` — which structurally satisfies ``OptionsDataPort``
because it exposes the same ``get_contract`` signature.

Spec reference: OPTIONS_FEATURE_SPEC.md §3.7.
Guardrail: #8 (mirror TCG conventions; engine ⊥ data).

Key invariants (for ``DefaultOptionsPnL``)
-----------------------------------------
- **Mark-to-market only — NO Black-Scholes re-pricing.**  Even if mark is
  ``None`` for an arbitrarily long stretch, we never compute a synthetic price.
- Default ``mark_field="mid"``.  Caller may pass ``"close"`` for backward-compat
  or specific use-cases (guardrail #4: ``close`` is often 0 on iVolatility;
  prefer ``mid``).
- ``pnl_daily`` is zero on missing-mark days; cumulative does not move on those
  days.
- ``qty`` may be negative (short position); algorithm is sign-agnostic.
- ``points`` is chronologically sorted (matches input row order from Module 1).
- The entry row must exist and have a non-``None`` mark; otherwise
  ``ValueError`` is raised — caller's responsibility.
- **Long-gap behavior:** if marks are ``None`` for N consecutive days, each
  of those N ``PnLPoint`` records carries ``mark=None, pnl_daily=0``.  When
  a non-``None`` mark resumes, the *entire* accumulated price move materialises
  as ``pnl_daily`` on that resume day (i.e. the gap is not amortised).
  Callers must be aware that large jumps on resume days are expected and do
  not represent a single-day extreme move — they represent the MTM catch-up
  for the full gap.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    PnLSeries,
)


# ---------------------------------------------------------------------------
# Local port — mirrors Module 1 ``get_contract`` without importing tcg.data
# ---------------------------------------------------------------------------


class OptionsDataPort(Protocol):
    """Minimal duck-typed port for the data dependency.

    Structurally equivalent to the ``get_contract`` method on
    ``tcg.data.options.protocol.OptionsDataReader``.  Any object whose
    ``get_contract`` has the same signature satisfies this protocol.

    The actual ``OptionsDataReader`` instance is injected at construction
    time by the wiring layer in ``tcg.core``; Module 7 itself never imports
    from ``tcg.data``.
    """

    async def get_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        """Return a single contract with its full chronological day series.

        Parameters
        ----------
        collection:
            OPT_* collection name, e.g. ``"OPT_SP_500"``.
        contract_id:
            Composite ``"<internalSymbol>|<expirationCycle>"`` identifier.

        Raises
        ------
        OptionsContractNotFound
            When the document does not exist in *collection*.
        OptionsDataAccessError
            On any underlying Mongo failure.
        """
        ...


# ---------------------------------------------------------------------------
# Public Protocol — the surface Module 7 exposes to its callers
# ---------------------------------------------------------------------------


class OptionsPnL(Protocol):
    """Per-contract historical P&L replay.

    Implementations must perform mark-to-market replay using stored bid/ask
    mids (or ``close`` when the caller explicitly requests it).  Black-Scholes
    re-pricing is NEVER performed here — that is Module 2's responsibility and
    this module does not depend on it.

    Spec reference: OPTIONS_FEATURE_SPEC.md §3.7.
    """

    async def compute(
        self,
        contract: OptionContractDoc,
        entry_date: date,
        qty: float,
        exit_date: date | None = None,
        mark_field: Literal["mid", "close"] = "mid",
    ) -> PnLSeries:
        """Replay daily P&L for a held contract.

        Parameters
        ----------
        contract:
            The option contract metadata (from Module 1 or directly from
            the caller).
        entry_date:
            The date at which the position was entered.  The row for this
            date must exist in the series and have a non-``None`` mark;
            otherwise ``ValueError`` is raised.
        qty:
            Signed quantity: positive = long, negative = short.  The
            algorithm is sign-agnostic; callers pass negative ``qty`` for
            short positions.
        exit_date:
            If provided, the series is truncated at this date.  If
            ``None``, the position is held until ``contract.expiration``
            or until data ends, whichever comes first.
        mark_field:
            ``"mid"`` (default) uses ``OptionDailyRow.mid = (bid+ask)/2``.
            ``"close"`` uses ``OptionDailyRow.close``.  Guardrail #4:
            ``close`` is often 0 on iVolatility data — prefer ``"mid"``.

        Returns
        -------
        PnLSeries
            Full P&L replay.  ``points`` is chronologically sorted.
            ``exit_reason`` indicates why the replay ended.

        Raises
        ------
        ValueError
            If the entry row is missing from the series, or the entry mark
            is ``None``.
        """
        ...
