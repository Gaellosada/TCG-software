"""DefaultOptionsSelector — Module 3 default implementation.

Spec reference: §3.3.

Selection flow
--------------
1. Resolve target expiration via Module 4 (``MaturityResolver``).
   - For ``NearestToTarget``: probe a wide-window chain to enumerate
     available expirations, then ``resolve_with_chain``.
   - Otherwise: ``resolve(ref_date, rule)``.
2. Query the chain via the injected ``ChainReaderPort`` with
   ``expiration_min == expiration_max == target_expiration``.
3. Apply criterion → ``SelectionResult``.

Independence contract
---------------------
This module imports **only** from:
- ``tcg.types.options`` (DTOs)
- ``tcg.engine.options.maturity.protocol`` (Module 4 Protocol)
- ``tcg.engine.options.pricing.protocol`` (Module 2 Protocol)
- ``tcg.engine.options.selection._ports`` (local Port for chain reader +
  underlying-price resolver)
- ``tcg.engine.options.selection._match`` (pure helpers)

It does **not** import from ``tcg.data.*``.  The API router (Wave B4)
wires a ``tcg.data.options.OptionsDataReader`` instance to the
``ChainReaderPort`` slot — that wiring crosses the boundary in
``tcg.core``, where it is allowed.

Guardrails honored
------------------
- #2 (stored vs computed separation): Module 3 only invokes Module 2
  when ``compute_missing_for_delta=True`` AND criterion is ``ByDelta``
  AND the row has ``delta_stored is None``.  Otherwise stored-only.
- #6 (VIX/ETH gating cascades): when Module 2 returns
  ``source="missing"`` (e.g. for OPT_VIX), the row stays unmatched and
  the selector reflects this in the ``missing_delta_no_compute`` path
  with a diagnostic that surfaces the underlying ``error_code``.

Underlying-price resolver (judgment call)
-----------------------------------------
Per ORDERS.md guidance, Module 3 takes an optional
``underlying_price_resolver: Callable | None`` at construction time.
- If ``None`` and ``ByMoneyness`` is requested → returns
  ``error_code="missing_underlying_price"``.
- If ``None`` and ``ByDelta`` with ``compute_missing_for_delta=True`` is
  requested for a row missing stored delta → that row is treated as
  ``delta=None`` (no compute possible without underlying), and the
  ``missing_delta_no_compute`` path may fire.
Module 6 will own the canonical resolver in production wiring.
"""

from __future__ import annotations

from datetime import date
from typing import Awaitable, Callable, Literal

from tcg.engine.options.maturity.protocol import MaturityResolver
from tcg.engine.options.pricing.protocol import OptionsPricer
from tcg.engine.options.selection._match import (
    match_by_delta,
    match_by_moneyness,
    match_by_strike,
)
from tcg.engine.options.selection._ports import ChainReaderPort
from tcg.engine.options.selection.protocol import OptionsSelector
from tcg.types.options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    GreekKind,
    MaturitySpec,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    SelectionCriterion,
    SelectionResult,
)

UnderlyingPriceResolver = Callable[
    [OptionContractDoc, date], Awaitable[float | None]
]


def _no_chain(root: str, on_date: date) -> SelectionResult:
    return SelectionResult(
        contract=None,
        matched_value=None,
        error_code="no_chain_for_date",
        diagnostic=f"{root} chain on {on_date.isoformat()} has 0 rows",
    )


def _missing_underlying(detail: str) -> SelectionResult:
    return SelectionResult(
        contract=None,
        matched_value=None,
        error_code="missing_underlying_price",
        diagnostic=detail,
    )


class DefaultOptionsSelector(OptionsSelector):
    """Default Module 3 implementation.

    Constructor parameters
    ----------------------
    reader:
        Anything satisfying ``ChainReaderPort`` (the real
        ``OptionsDataReader`` from Module 1, or a test double).
    maturity_resolver:
        ``MaturityResolver`` from Module 4.
    pricer:
        Optional ``OptionsPricer`` from Module 2.  Required when callers
        pass ``compute_missing_for_delta=True``; ``NotImplementedError``
        otherwise.
    underlying_price_resolver:
        Optional async callable to join the underlying price for a
        contract on a given date.  Required for ``ByMoneyness`` and for
        the Module-2 compute path on ``ByDelta``.  When omitted, those
        paths surface ``error_code="missing_underlying_price"``.
    """

    def __init__(
        self,
        reader: ChainReaderPort,
        maturity_resolver: MaturityResolver,
        pricer: OptionsPricer | None = None,
        underlying_price_resolver: UnderlyingPriceResolver | None = None,
    ) -> None:
        self._reader = reader
        self._maturity = maturity_resolver
        self._pricer = pricer
        self._resolve_underlying = underlying_price_resolver

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def select(
        self,
        root: str,
        date: date,
        type: Literal["C", "P"],
        criterion: SelectionCriterion,
        maturity: MaturitySpec,
        compute_missing_for_delta: bool = False,
    ) -> SelectionResult:
        # Validate opt-in vs availability of pricer.
        if compute_missing_for_delta and not isinstance(criterion, ByDelta):
            # Opt-in is meaningful only for ByDelta — silently ignore for
            # ByMoneyness / ByStrike (matches API ergonomics).
            compute_missing_for_delta = False
        if compute_missing_for_delta and self._pricer is None:
            raise NotImplementedError(
                "compute_missing_for_delta=True requires a pricer; "
                "DefaultOptionsSelector was constructed with pricer=None"
            )

        # 1) Resolve target expiration.
        target_expiration = await self._resolve_expiration(
            root=root,
            ref_date=date,
            type=type,
            maturity=maturity,
        )
        if target_expiration is None:
            return _no_chain(root, date)

        # 2) Query the chain on the resolved expiration.
        rows = await self._reader.query_chain(
            root=root,
            date=date,
            type=type,
            expiration_min=target_expiration,
            expiration_max=target_expiration,
        )
        if not rows:
            return _no_chain(root, date)

        # 3) Dispatch on criterion.
        if isinstance(criterion, ByStrike):
            return match_by_strike(rows, criterion.strike)

        if isinstance(criterion, ByMoneyness):
            return await self._select_by_moneyness(rows, criterion, on_date=date)

        if isinstance(criterion, ByDelta):
            return await self._select_by_delta(
                rows,
                criterion,
                compute_missing=compute_missing_for_delta,
                on_date=date,
            )

        # Defensive — SelectionCriterion is closed at the type level.
        raise TypeError(  # pragma: no cover
            f"Unsupported SelectionCriterion: {type(criterion).__name__}"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _resolve_expiration(
        self,
        root: str,
        ref_date: date,
        type: Literal["C", "P"],
        maturity: MaturitySpec,
    ) -> date | None:
        """Resolve the rule, querying a probe chain only for NearestToTarget."""
        if isinstance(maturity, NearestToTarget):
            # Probe with a wide window to enumerate available expirations
            # on this date.  Module 4's resolve_with_chain picks the
            # nearest DTE.
            #
            # Window choice: ref_date .. ref_date + 5y covers any plausible
            # listed maturity; bonds peak near 10y but those are not used
            # with NearestToTarget in Phase 1.  Bound chosen on the high
            # side rather than guessing exactly.
            from datetime import timedelta

            far_future = ref_date + timedelta(days=365 * 5)
            probe_rows = await self._reader.query_chain(
                root=root,
                date=ref_date,
                type=type,
                expiration_min=ref_date,
                expiration_max=far_future,
            )
            available = sorted({c.expiration for c, _r in probe_rows})
            if not available:
                return None
            return self._maturity.resolve_with_chain(
                ref_date=ref_date,
                rule=maturity,
                available_expirations=available,
            )

        # Non-chain rules: pure date arithmetic.
        return self._maturity.resolve(ref_date=ref_date, rule=maturity)

    async def _select_by_moneyness(
        self,
        rows: list[tuple[OptionContractDoc, OptionDailyRow]],
        criterion: ByMoneyness,
        on_date: date,
    ) -> SelectionResult:
        if self._resolve_underlying is None:
            return _missing_underlying(
                "no underlying_price_resolver injected — ByMoneyness requires "
                "an underlying-price join"
            )
        # Use the first row's contract to resolve underlying — all rows in
        # the chain share the same underlying_ref / root, so the join is
        # the same for any of them.
        first_contract = rows[0][0]
        S = await self._resolve_underlying(first_contract, on_date)
        if S is None or S <= 0:
            return _missing_underlying(
                f"underlying_price_resolver returned {S!r} for {first_contract.collection}"
            )
        return match_by_moneyness(
            rows=rows,
            target_K_over_S=criterion.target_K_over_S,
            tolerance=criterion.tolerance,
            underlying_price=float(S),
        )

    async def _select_by_delta(
        self,
        rows: list[tuple[OptionContractDoc, OptionDailyRow]],
        criterion: ByDelta,
        compute_missing: bool,
        on_date: date,
    ) -> SelectionResult:
        # Build per-row delta list.  Fast path: stored-only.
        chain_size = len(rows)
        deltas: list[float | None] = [r.delta_stored for _c, r in rows]

        if compute_missing:
            assert self._pricer is not None  # narrowed by select()
            # Resolve underlying once (all rows share root/ref).  If the
            # resolver is missing or returns None, computed deltas are
            # not available — leave those rows as None.
            first_contract = rows[0][0]
            underlying: float | None = None
            underlying_error: str | None = None
            if self._resolve_underlying is None:
                underlying_error = "no underlying_price_resolver injected"
            else:
                S = await self._resolve_underlying(first_contract, on_date)
                if S is None or S <= 0:
                    underlying_error = f"underlying_price_resolver returned {S!r}"
                else:
                    underlying = float(S)

            # Track Module-2 missing reasons for diagnostic enrichment.
            compute_failure_reasons: set[str] = set()

            for idx, (contract, row) in enumerate(rows):
                if deltas[idx] is not None:
                    continue
                # No underlying → cannot compute; leave as None.
                if underlying is None:
                    if underlying_error is not None:
                        compute_failure_reasons.add("missing_underlying_price")
                    continue
                computed = self._pricer.compute(
                    contract=contract,
                    row=row,
                    underlying_price=underlying,
                    which=(GreekKind.DELTA,),
                )
                if (
                    computed.delta.source == "computed"
                    and computed.delta.value is not None
                ):
                    deltas[idx] = float(computed.delta.value)
                else:
                    if computed.delta.error_code is not None:
                        compute_failure_reasons.add(computed.delta.error_code)

            result = match_by_delta(
                rows=rows,
                deltas=deltas,
                target=criterion.target_delta,
                tolerance=criterion.tolerance,
                strict=criterion.strict,
                chain_size=chain_size,
            )
            # Enrich the diagnostic when no usable delta exists, surfacing
            # Module-2 / underlying failure reasons (guardrail #6 cascade).
            if (
                result.error_code == "missing_delta_no_compute"
                and compute_failure_reasons
            ):
                reasons = ", ".join(sorted(compute_failure_reasons))
                enriched = (
                    f"{result.diagnostic}; computed delta unavailable due to "
                    f"{reasons}"
                )
                return SelectionResult(
                    contract=None,
                    matched_value=None,
                    error_code=result.error_code,
                    diagnostic=enriched,
                )
            return result

        # Stored-only path.
        return match_by_delta(
            rows=rows,
            deltas=deltas,
            target=criterion.target_delta,
            tolerance=criterion.tolerance,
            strict=criterion.strict,
            chain_size=chain_size,
        )


# Re-export the type alias so callers / wiring code can import it from
# the same place as the class.
__all__ = ["DefaultOptionsSelector", "UnderlyingPriceResolver"]
