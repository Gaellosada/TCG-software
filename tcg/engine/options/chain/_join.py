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
   The FUT_* collection name is derived from the OPT_* collection
   name (``OPT_X`` → ``FUT_X``).  When ``contract.underlying_ref``
   is present (Mongo-era data) look up that FUT_* contract by id;
   otherwise — the dwh SQL reader does NOT preserve ``underlying_ref``
   — fall back to the FUT_* contract whose ``expiration`` equals the
   option's (``get_futures_close_by_expiration``), which is the
   Black-76 forward, the SAME by-expiration resolution Branch 2 uses
   for VIX.  This covers SP500, NASDAQ, GOLD, T_BOND, T_NOTE, EURUSD,
   JPYUSD.  Returns ``None`` on miss (e.g. a weekly option whose
   expiration has no matching listed future).

OPT_ETH (and any crypto root) is NOT an option-on-future — it is
spot/perp-settled (Deribit), so the by-expiration fallback is skipped
(a coincidental ``FUT_ETH`` is the wrong underlying); it returns
``None``.  The chain reports ``K_over_S = None`` and Module 2 (when
invoked) returns ``error_code="missing_deribit_feed"`` per guardrail #6.

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

# Crypto roots are NOT options-on-futures: they settle on a spot / perpetual
# index (Deribit), so the futures-by-expiration fallback (Branch 3) must NOT
# fire for them even though a FUT_* collection coincidentally exists (e.g.
# FUT_ETH).  BTC is already handled by Branch 1 (row-embedded price); ETH has no
# wired underlying feed (see ``_gating`` ``missing_deribit_feed``) and so resolves
# to ``None`` here, as before.  Index/commodity/rate/FX roots (SP500, NASDAQ,
# GOLD, T_BOND, T_NOTE_10_Y, EURUSD, JPYUSD) ARE genuine options-on-futures and
# DO use the fallback — each has a matching FUT_* with a real forward.
_CRYPTO_ROOTS: frozenset[str] = frozenset({"BTC", "OPT_BTC", "ETH", "OPT_ETH"})


def _is_btc(contract: OptionContractDoc) -> bool:
    return contract.collection == "OPT_BTC" or contract.root_underlying in _BTC_ROOTS


def _is_crypto(contract: OptionContractDoc) -> bool:
    """True for crypto roots (BTC/ETH) — spot/perp-settled, NOT option-on-future,
    so the FUT-by-expiration fallback must be skipped for them."""
    return (
        contract.collection in _CRYPTO_ROOTS
        or contract.root_underlying in _CRYPTO_ROOTS
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
    fut_collection = _futures_collection_for(contract.collection)
    if fut_collection is None:
        return None

    if contract.underlying_ref is not None:
        # Per-contract FUT pointer available (Mongo-era data) — direct lookup.
        return await futures_port.get_futures_close_on_date(
            fut_collection,
            contract.underlying_ref,
            target_date,
        )

    # Crypto roots (ETH; BTC is Branch 1) are spot/perp-settled, NOT
    # options-on-futures — a coincidental FUT_ETH is the WRONG underlying, so do
    # not attempt the fallback (preserves the prior ``None`` for OPT_ETH; the
    # pricer also blocks it as ``missing_deribit_feed``).
    if _is_crypto(contract):
        return None

    # No per-contract ``underlying_ref`` (the dwh SQL reader does not preserve
    # the Mongo FUT ``_id`` — see ``tcg.data._sql.options`` ``_meta_to_contract``),
    # which would otherwise make EVERY option-on-future series all-NaN
    # (``missing_underlying_price``).  Fall back to the SAME by-expiration
    # resolution the VIX branch (Branch 2) uses: the FUT_* contract whose
    # ``expiration`` equals the option's IS the Black-76 forward for an
    # option-on-future, so it is the correct underlying (NOT the cash index —
    # they differ by cost-of-carry/dividends).  ``get_futures_close_by_expiration``
    # matches an EXACT expiration; a weekly option whose expiration has no
    # matching (monthly) future resolves to ``None`` (graceful, mirroring VIX
    # weeklies) → ``missing_underlying_price`` on those dates, never a crash.
    return await futures_port.get_futures_close_by_expiration(
        fut_collection,
        contract.expiration,
        target_date,
    )
