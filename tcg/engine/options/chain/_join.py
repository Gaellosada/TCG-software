"""Canonical underlying-price resolver — Module 6 owns this in Phase 1.

Three join strategies, dispatched on contract metadata:

1. **OPT_BTC** (Decision H — collection ``"OPT_BTC"`` or
   ``root_underlying == "BTC"``):
   The underlying price is **inside the INTERNAL provider's
   eodGreeks row itself**.  Module 1 already extracts it and
   surfaces it on ``OptionDailyRow.underlying_price_stored``.
   Module 6 reads it directly off the row — no Mongo query.

2. **OPT_VIX** (``root_underlying == "IND_VIX"``, or collection
   ``"OPT_VIX"``):
   Look up the matching ``FUT_VIX`` contract whose ``expiration``
   field equals the option's ``expiration``, and return its close
   on ``target_date``.  This is the Black-76 forward for the option
   (VIX options are European, AM-settled on the matching VIX future
   on expiration Wednesday — the future is the canonical forward).
   Returns ``None`` when no matching FUT_VIX contract exists (i.e.
   the option is weekly — Phase 3 will add forward-curve
   interpolation) or when the matching future has no bar for the
   trade date.

3. **All other roots** (option-on-future):
   Look up the FUT_* document referenced by
   ``contract.underlying_ref`` (e.g.
   ``"FUT_SP_500_EMINI_20240621"``), find the row matching
   ``target_date``, return ``eodDatas.close``.  The FUT_*
   collection name is derived from the OPT_* collection name
   (``OPT_X`` → ``FUT_X``).  Returns ``None`` on miss.

OPT_ETH is not specially handled — its ``rootUnderlying`` is
``"ETH"`` (not an INDEX) and ``underlying_ref`` is absent, so the
fallthrough returns ``None``.  The chain reports
``K_over_S = None`` and Module 2 (when invoked) returns
``error_code="missing_deribit_feed"`` per guardrail #6.

Returning ``None`` is the contract for "join not possible" — the
caller (``DefaultOptionsChain.snapshot``) decides how to surface it
(typically ``K_over_S = None`` plus a note).
"""

from __future__ import annotations

from datetime import date

from tcg.engine.options.chain._forward import is_vix as _is_vix
from tcg.engine.options.chain._forward import resolve_vix_forward
from tcg.engine.options.chain._ports import FuturesDataPort, IndexDataPort
from tcg.types.options import OptionContractDoc, OptionDailyRow


_BTC_ROOTS: frozenset[str] = frozenset({"BTC", "OPT_BTC"})


def _is_btc(contract: OptionContractDoc) -> bool:
    return (
        contract.collection == "OPT_BTC"
        or contract.root_underlying in _BTC_ROOTS
    )


def _futures_collection_for(opt_collection: str) -> str | None:
    """Derive the FUT_* collection name from an OPT_* collection.

    ``OPT_SP_500`` → ``FUT_SP_500``.  Returns ``None`` if the input is
    not an ``OPT_*`` collection (defensive — should not happen in
    Phase 1 since callers always pass real OPT_* names).
    """
    if not opt_collection.startswith("OPT_"):
        return None
    return "FUT_" + opt_collection[len("OPT_") :]


async def resolve_underlying_price(
    *,
    contract: OptionContractDoc,
    row: OptionDailyRow,
    target_date: date,
    index_port: IndexDataPort,
    futures_port: FuturesDataPort,
) -> float | None:
    """Resolve the underlying price for ``(contract, target_date)``.

    Returns ``None`` when the join cannot be made.  See module docstring
    for the three join strategies.

    The ``row`` argument is required for the OPT_BTC field-level join
    (Decision H) — its ``underlying_price_stored`` field carries the
    price extracted from the INTERNAL provider's eodGreeks entry by
    Module 1.
    """
    # Branch 1: OPT_BTC — read directly from the row.
    if _is_btc(contract):
        return row.underlying_price_stored

    # Branch 2: OPT_VIX — match by expiration against FUT_VIX. Delegates
    # to the shared helper so the API bulk path
    # (``_batch_underlying_prices``) uses the same dispatch logic
    # (see ``tcg.engine.options.chain._forward.resolve_vix_forward``).
    if _is_vix(contract):
        return await resolve_vix_forward(contract, futures_port, target_date)

    # Branch 3: option-on-future — FUT_* lookup.
    if contract.underlying_ref is None:
        # No per-contract pointer to a FUT_*; cannot join.
        return None

    fut_collection = _futures_collection_for(contract.collection)
    if fut_collection is None:
        return None

    return await futures_port.get_futures_close_on_date(
        fut_collection,
        contract.underlying_ref,
        target_date,
    )
