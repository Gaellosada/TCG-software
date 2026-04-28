"""DefaultOptionsChain — assemble a ``ChainSnapshot`` from raw rows.

Module 6 (spec §3.6) is the sole place where the
``ComputeResult.source="stored"`` widening happens (Appendix C.3).
Module 1 supplies stored values; Module 2 supplies computed/missing.
This module merges them and produces a single ``ChainSnapshot``.

Cardinal invariants
-------------------
- Module 6 calls Module 2 **only** when ``compute_missing=True``
  (guardrail #2).  Stored takes precedence regardless.
- Module 6 NEVER fabricates a ``source="computed"`` ComputeResult —
  pass-through only.  Module 2 is the sole emitter of ``"computed"``.
- ``source="stored"`` is exclusive to Module 6 (via ``_widen.py``).
- ``underlying_price`` on ``ChainSnapshot`` stays ``float | None``
  (Decision B) — the API router (Wave B4) wraps to ``ComputeResult``
  for the API response.
- ``K_over_S = strike / underlying_price`` computed fresh; never reads
  the stored ``moneyness`` (guardrail #3).
- OPT_VIX / OPT_ETH chains return rows where every Greek is
  ``source="missing"`` even with ``compute_missing=True`` — the gate
  cascades from Module 2 (guardrail #6).
- Empty chain query → ``ChainSnapshot`` with ``rows=()`` and a note.

Independence
------------
This module does NOT import from ``tcg.data.*`` (lint-imports
``engine-data-isolation`` contract).  Dependencies are typed against
local ``OptionsDataPort`` / ``IndexDataPort`` / ``FuturesDataPort``
Protocols (see ``_ports.py``).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from tcg.engine.options.chain._join import resolve_underlying_price
from tcg.engine.options.chain._ports import (
    FuturesDataPort,
    IndexDataPort,
    OptionsDataPort,
)
from tcg.engine.options.chain._widen import (
    merge_stored_with_computed,
    widen_stored,
)
from tcg.engine.options.chain.protocol import OptionsChain
from tcg.engine.options.pricing.protocol import OptionsPricer
from tcg.types.errors import OptionsValidationError
from tcg.types.options import (
    ChainRow,
    ChainSnapshot,
    ComputedGreeks,
    ComputeResult,
    OptionContractDoc,
    OptionDailyRow,
)

_VALID_TYPES: frozenset[str] = frozenset({"C", "P", "both"})


class DefaultOptionsChain(OptionsChain):
    """Default implementation of the ``OptionsChain`` Protocol.

    Constructor takes the same Protocol-typed dependencies as Module 3,
    via local ports.  See ``_ports.py`` for the duck-typed shapes.
    """

    def __init__(
        self,
        *,
        data_port: OptionsDataPort,
        pricer: OptionsPricer,
        index_port: IndexDataPort,
        futures_port: FuturesDataPort,
    ) -> None:
        self._data_port = data_port
        self._pricer = pricer
        self._index_port = index_port
        self._futures_port = futures_port

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
    ) -> ChainSnapshot:
        # 1. Validate parameters.
        if type not in _VALID_TYPES:
            raise OptionsValidationError(
                f"Invalid type {type!r}; expected one of {sorted(_VALID_TYPES)}."
            )
        if expiration_min > expiration_max:
            raise OptionsValidationError(
                f"expiration_min={expiration_min.isoformat()} > "
                f"expiration_max={expiration_max.isoformat()}."
            )

        # 2. Query the chain via the data port.
        pairs: list[tuple[OptionContractDoc, OptionDailyRow]] = (
            await self._data_port.query_chain(
                root=root,
                date=date,
                type=type,
                expiration_min=expiration_min,
                expiration_max=expiration_max,
                strike_min=strike_min,
                strike_max=strike_max,
            )
        )

        # 3. Empty chain → early return with note.
        if not pairs:
            note = (
                f"{root} has 0 rows for date={date.isoformat()} "
                f"type={type} expiration in "
                f"[{expiration_min.isoformat()}, {expiration_max.isoformat()}]"
            )
            return ChainSnapshot(
                root=root,
                date=date,
                underlying_price=None,
                rows=(),
                notes=(note,),
            )

        # 4. Resolve the underlying price.  We use the first contract's
        #    metadata to pick a join strategy; the row supplies OPT_BTC's
        #    field-level price.  Within a single root all contracts share
        #    the same root_underlying, so any contract works.  We pick the
        #    contract whose row carries the OPT_BTC underlying_price_stored
        #    when present (defensive — Module 1 fills it on every row for
        #    the INTERNAL provider).
        first_contract, first_row = pairs[0]
        underlying_price = await resolve_underlying_price(
            contract=first_contract,
            row=first_row,
            target_date=date,
            index_port=self._index_port,
            futures_port=self._futures_port,
        )

        notes_list: list[str] = []
        if underlying_price is None:
            notes_list.append(
                f"Underlying join failed for {root} on {date.isoformat()}; "
                "K/S unavailable for all rows in this snapshot."
            )

        # 5. Build chain rows.
        chain_rows: list[ChainRow] = []
        for contract, row in pairs:
            chain_rows.append(
                self._build_chain_row(
                    contract=contract,
                    row=row,
                    underlying_price=underlying_price,
                    compute_missing=compute_missing,
                )
            )

        return ChainSnapshot(
            root=root,
            date=date,
            underlying_price=underlying_price,
            rows=tuple(chain_rows),
            notes=tuple(notes_list),
        )

    # ---- internals --------------------------------------------------------

    def _build_chain_row(
        self,
        *,
        contract: OptionContractDoc,
        row: OptionDailyRow,
        underlying_price: float | None,
        compute_missing: bool,
    ) -> ChainRow:
        """Assemble a single ``ChainRow`` from one (contract, row) pair."""
        # K/S — fresh from strike / underlying_price (guardrail #3).
        k_over_s: float | None
        if underlying_price is not None and underlying_price != 0:
            k_over_s = float(contract.strike) / float(underlying_price)
        else:
            k_over_s = None

        # Greek fields — stored takes precedence; pricer fills gaps when
        # compute_missing=True.  We invoke the pricer at most once per row
        # (its compute() call returns all five Greeks at once).
        computed: ComputedGreeks | None = None
        if compute_missing and self._row_has_any_missing_stored(row):
            computed = self._pricer.compute(contract, row, underlying_price)

        iv = merge_stored_with_computed(
            stored_value=row.iv_stored,
            greek_name="iv",
            computed=computed.iv if computed is not None else None,
        )
        delta = merge_stored_with_computed(
            stored_value=row.delta_stored,
            greek_name="delta",
            computed=computed.delta if computed is not None else None,
        )
        gamma = merge_stored_with_computed(
            stored_value=row.gamma_stored,
            greek_name="gamma",
            computed=computed.gamma if computed is not None else None,
        )
        theta = merge_stored_with_computed(
            stored_value=row.theta_stored,
            greek_name="theta",
            computed=computed.theta if computed is not None else None,
        )
        vega = merge_stored_with_computed(
            stored_value=row.vega_stored,
            greek_name="vega",
            computed=computed.vega if computed is not None else None,
        )

        return ChainRow(
            contract_id=contract.contract_id,
            expiration=contract.expiration,
            type=contract.type,
            strike=contract.strike,
            K_over_S=k_over_s,
            bid=row.bid,
            ask=row.ask,
            mid=row.mid,
            open_interest=row.open_interest,
            iv=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
        )

    @staticmethod
    def _row_has_any_missing_stored(row: OptionDailyRow) -> bool:
        """True iff any of the five stored Greeks is None.

        Used to skip the pricer call when every Greek is stored (avoids
        wasted work and an unnecessary side-effect on the mock pricer in
        tests).
        """
        return (
            row.iv_stored is None
            or row.delta_stored is None
            or row.gamma_stored is None
            or row.theta_stored is None
            or row.vega_stored is None
        )


# ``ComputeResult`` is re-exported here only because some downstream
# typing-context code may want to pin to it via this module path; not
# strictly necessary, but mirrors Module 3's conventions.
__all__ = ["DefaultOptionsChain", "ComputeResult"]
