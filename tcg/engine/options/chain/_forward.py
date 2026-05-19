"""VIX forward-resolution shared between the API bulk path and the
single-date chain join.

Both call sites have the same business rule: for a VIX option contract,
the Black-76 forward is the close of the matching ``FUT_VIX`` monthly
future (the one whose ``expiration`` field equals the option's
``expiration``). For non-VIX contracts the spot or per-contract path
applies; this module's helpers short-circuit to ``None`` so callers
fall through.

Two helpers live here, deliberately shaped for the two call-site needs:

  * :func:`resolve_vix_forward` — single-date close, used by
    ``_join.resolve_underlying_price`` (per-row chain assembly).
  * :func:`resolve_vix_futures_ref` — returns just the FUT_VIX contract
    id so the API bulk path can drive a date-range ``get_prices`` from
    it without making an extra round-trip per date.

Both consult ``_is_vix`` (also defined here) for the dispatch — moved
out of ``_join`` so the API path doesn't reach into private names.

Phase-3 note: the helpers return ``None`` for weekly VIX options (no
matching FUT_VIX expiration). Phase 3 will replace these returns with
a forward-curve interpolator; the call sites stay the same.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from tcg.engine.options.chain._ports import FuturesDataPort
from tcg.types.options import OptionContractDoc


_VIX_ROOTS: frozenset[str] = frozenset({"IND_VIX", "OPT_VIX"})


def is_vix(contract: OptionContractDoc) -> bool:
    """Whether ``contract`` is a VIX option (collection or root match)."""
    return (
        contract.collection == "OPT_VIX"
        or contract.root_underlying in _VIX_ROOTS
    )


async def resolve_vix_forward(
    contract: OptionContractDoc,
    futures_port: FuturesDataPort,
    target_date: date,
) -> float | None:
    """Return the FUT_VIX close that serves as Black-76 forward for
    ``contract`` on ``target_date``.

    Returns ``None`` when:
      * ``contract`` is not a VIX option (caller should use spot / per-contract path),
      * the option is a weekly with no matching monthly FUT_VIX expiration
        (Phase 3 will interpolate from the forward curve), or
      * the matching future has no bar for ``target_date``.

    Caller propagates ``None`` to the pricer which surfaces
    ``error_code="missing_forward_vix_curve"``.
    """
    if not is_vix(contract):
        return None
    return await futures_port.get_futures_close_by_expiration(
        "FUT_VIX",
        contract.expiration,
        target_date,
    )


class _MarketDataServiceForVix(Protocol):
    """Minimal interface needed by :func:`resolve_vix_futures_ref`.

    Mirrors ``tcg.data.protocols.MarketDataService.find_futures_contract_by_expiration``
    without taking a hard dep on the full data protocol from the engine
    layer (engine-data-isolation contract).
    """

    async def find_futures_contract_by_expiration(
        self, collection: str, expiration_int: int
    ) -> str | None: ...


async def resolve_vix_futures_ref(
    contract: OptionContractDoc,
    svc: _MarketDataServiceForVix,
) -> str | None:
    """Return the FUT_VIX contract id matching ``contract.expiration``.

    Used by the API bulk path (``_batch_underlying_prices``) which then
    drives a date-range ``get_prices`` from that contract id — saving the
    per-date round-trips ``resolve_vix_forward`` would otherwise make.

    Returns ``None`` when:
      * ``contract`` is not a VIX option, or
      * no FUT_VIX expiration matches (weekly).

    Any underlying data-access error is swallowed and returned as ``None``
    so the caller falls through to the missing-forward-curve path
    (matches the API's "don't 502 on a single missing underlying" policy).
    """
    if not is_vix(contract):
        return None
    exp = contract.expiration
    exp_int = exp.year * 10000 + exp.month * 100 + exp.day
    try:
        return await svc.find_futures_contract_by_expiration("FUT_VIX", exp_int)
    except Exception:  # noqa: BLE001 — see docstring
        return None
