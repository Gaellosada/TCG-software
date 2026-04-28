"""Public Protocol for Module 6 — chain snapshot assembly.

Spec reference: §3.6 (``tcg.engine.options.chain``).

Contract:

- ``snapshot`` returns a ``ChainSnapshot`` for ``(root, date)`` with a
  filter on type, expiration window, and (optionally) strike bounds.
- When ``compute_missing=False`` (default), missing stored Greeks
  surface as ``ComputeResult(source="missing", error_code="not_stored")``
  — Module 2 is NOT invoked.
- When ``compute_missing=True``, Module 2 is invoked once per row to
  fill the missing Greeks; stored values still take precedence.
- Module 6 is the **only** place where stored values are widened to
  ``ComputeResult(source="stored", ...)`` (spec §4.4 + Appendix C.3).

Independence contract: this module does NOT import from ``tcg.data.*``.
The caller injects an ``OptionsDataPort``, ``IndexDataPort``, and
``FuturesDataPort`` (defined in ``_ports.py``).
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from tcg.types.options import ChainSnapshot


class OptionsChain(Protocol):
    """Assemble a ``ChainSnapshot`` for a (root, date) pair."""

    async def snapshot(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        compute_missing: bool = False,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> ChainSnapshot:
        """Return the full chain table for ``(root, date)``.

        Parameters
        ----------
        root:
            Collection name, e.g. ``"OPT_SP_500"``.
        date:
            Trade date for the chain query.
        type:
            ``"C"`` (calls), ``"P"`` (puts), or ``"both"``.
        expiration_min, expiration_max:
            Inclusive expiration window (must satisfy ``min <= max``).
        compute_missing:
            When ``True``, missing stored Greeks are filled by invoking
            Module 2 (pricer).  When ``False``, missing Greeks surface as
            ``source="missing"`` with ``error_code="not_stored"``.
        strike_min, strike_max:
            Optional strike-bounded filtering forwarded to the data port.
        expiration_cycle:
            Optional ``OptionContractDoc.expiration_cycle`` filter. When
            non-None, contracts whose cycle does not match are dropped.
            Used by the smile UI to disambiguate roots (notably
            OPT_SP_500) where multiple cycles share the same expiration
            calendar date.
        """
        ...
