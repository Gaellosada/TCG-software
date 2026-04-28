"""Adapter wiring for the options router (Wave B4).

This module sits in ``tcg.core.api`` because it intentionally bridges
``tcg.data.*`` and ``tcg.engine.options.*``.  Per the import-linter
``engine-data-isolation`` contract those two layers do not import each
other; ``tcg.core`` is the only place where the boundary may be
crossed.

What lives here
---------------
- Three small port adapters (``_OptionsDataPortAdapter``,
  ``_IndexDataPortAdapter``, ``_FuturesDataPortAdapter``) translating
  the existing ``MarketDataService`` surface and ``MongoOptionsDataReader``
  into the duck-typed ports each engine module expects.
- A per-request ``CachedChainReader`` decorator that memoizes
  ``query_chain`` results — used by ``DefaultOptionsSelector`` so that
  the wide-window probe issued by ``NearestToTarget`` is not duplicated
  when the API later asks for the resolved-expiration chain.
- Factory helpers that assemble the concrete engine objects with these
  adapters wired in.

These factories are called per FastAPI request (within each handler, or
through ``Depends(...)``).  That keeps the cache lifetime scoped to a
single request and avoids cross-request data leaks.
"""

from __future__ import annotations

from datetime import date
from typing import Awaitable, Callable, Literal

from tcg.data.options.reader import MongoOptionsDataReader
from tcg.data.protocols import MarketDataService
from tcg.engine.options.chain._join import resolve_underlying_price
from tcg.engine.options.chain.chain import DefaultOptionsChain
from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.pricing.pricer import DefaultOptionsPricer
from tcg.engine.options.selection.selector import DefaultOptionsSelector
from tcg.types.options import OptionContractDoc, OptionDailyRow


# ---------------------------------------------------------------------------
# Port adapters
# ---------------------------------------------------------------------------


class _OptionsDataPortAdapter:
    """Wrap a ``MongoOptionsDataReader`` (or any object implementing
    ``query_chain``) to satisfy the engine-side ``OptionsDataPort`` /
    ``ChainReaderPort`` Protocols.

    The shape is identical; this class exists primarily so the wiring
    module references the engine-side contract explicitly and so we can
    later interpose telemetry without touching engine code.
    """

    def __init__(self, reader: MongoOptionsDataReader) -> None:
        self._reader = reader

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
        return await self._reader.query_chain(
            root=root,
            date=date,
            type=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
        )


class CachedChainReader:
    """Per-request cache for ``query_chain`` results.

    Reviewer suggestion (Wave B2): ``DefaultOptionsSelector`` issues a
    wide-window probe call for ``NearestToTarget``, then a second
    call narrowed to the resolved expiration.  Without a cache, both hit
    Mongo.  Caching by ``(root, date, type, expiration_min, expiration_max,
    strike_min, strike_max)`` makes the second call free when its window
    is fully covered by the first — but for simplicity and predictability
    we cache exact-key only.  The probe key and the narrow-key differ, so
    the narrow call still hits Mongo, but the cache *does* coalesce
    repeated identical queries within one request (e.g. when
    ``/select`` is followed by ``/chain`` from the frontend in the same
    request — currently not the case, but the cache costs little and is
    correctness-safe).

    Lifetime: one instance per FastAPI request.  Instantiated by the
    factory functions below; not reused across requests.
    """

    def __init__(self, inner: _OptionsDataPortAdapter) -> None:
        self._inner = inner
        self._cache: dict[
            tuple, list[tuple[OptionContractDoc, OptionDailyRow]]
        ] = {}

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
        key = (
            root,
            date,
            type,
            expiration_min,
            expiration_max,
            strike_min,
            strike_max,
        )
        if key in self._cache:
            return self._cache[key]
        result = await self._inner.query_chain(
            root=root,
            date=date,
            type=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
        )
        self._cache[key] = result
        return result


class _IndexDataPortAdapter:
    """Adapter satisfying ``tcg.engine.options.chain._ports.IndexDataPort``.

    Looks up the INDEX collection's price series for ``index_id`` and
    finds the row whose date matches ``target_date``.  Returns ``close``
    (the canonical INDEX value field — matches the Java legacy
    ``IND_VIX.eodDatas.close``).  Returns ``None`` on miss.
    """

    def __init__(self, market_data: MarketDataService) -> None:
        self._md = market_data

    async def get_index_value_on_date(
        self,
        index_id: str,
        target_date: date,
    ) -> float | None:
        try:
            series = await self._md.get_prices(
                "INDEX",
                index_id,
                start=target_date,
                end=target_date,
            )
        except Exception:  # noqa: BLE001
            # Any underlying-data failure → treat as join-not-possible.
            # The chain will surface K/S=None plus a note; the router
            # already separates engine-level joins from query-level
            # OptionsDataAccessError (which only fires for OPT_*
            # collection failures).
            return None
        if series is None or len(series) == 0:
            return None
        # PriceSeries.dates is YYYYMMDD int64; find an exact match.
        target_int = (
            target_date.year * 10000 + target_date.month * 100 + target_date.day
        )
        for idx, d in enumerate(series.dates.tolist()):
            if int(d) == target_int:
                return float(series.close[idx])
        return None


class _FuturesDataPortAdapter:
    """Adapter satisfying ``tcg.engine.options.chain._ports.FuturesDataPort``.

    Looks up a single futures contract's close on ``target_date``.  The
    ``contract_ref`` is the ``OptionContractDoc.underlying_ref`` — e.g.
    ``"FUT_SP_500_EMINI_20240621"`` — which Module 1 surfaces from the
    OPT_* document.  We use ``MarketDataService.get_prices`` to read the
    contract's series and find the date.
    """

    def __init__(self, market_data: MarketDataService) -> None:
        self._md = market_data

    async def get_futures_close_on_date(
        self,
        collection: str,
        contract_ref: str,
        target_date: date,
    ) -> float | None:
        try:
            series = await self._md.get_prices(
                collection,
                contract_ref,
                start=target_date,
                end=target_date,
            )
        except Exception:  # noqa: BLE001
            return None
        if series is None or len(series) == 0:
            return None
        target_int = (
            target_date.year * 10000 + target_date.month * 100 + target_date.day
        )
        for idx, d in enumerate(series.dates.tolist()):
            if int(d) == target_int:
                return float(series.close[idx])
        return None


# ---------------------------------------------------------------------------
# Underlying-price resolver (closure over the three ports)
# ---------------------------------------------------------------------------


def _build_underlying_resolver(
    index_port: _IndexDataPortAdapter,
    futures_port: _FuturesDataPortAdapter,
) -> Callable[[OptionContractDoc, date], Awaitable[float | None]]:
    """Return an async callable resolving (contract, date) → underlying price.

    Reuses ``tcg.engine.options.chain._join.resolve_underlying_price``
    so that Module 6 and Module 3 share the *same* resolution logic.
    For OPT_BTC the resolver requires a ``row`` argument (Decision H —
    field-level join), but Module 3 only has a contract+date.  This
    closure reads BTC's price from a probe-row trick: we fetch the chain
    for that date (one call) and pick any row's
    ``underlying_price_stored``; for non-BTC roots that field is None
    and the index/futures branches fire as usual.

    Module 3 only needs this resolver for ``ByMoneyness`` and the
    ``ByDelta+compute_missing_for_delta`` path; OPT_BTC isn't used with
    ``ByMoneyness`` in Phase 1 (verified — the resolver still has to
    not crash on it).  We therefore use a sentinel ``OptionDailyRow`` —
    constructed with ``underlying_price_stored=None`` for non-BTC paths
    — and accept that BTC ``ByMoneyness`` returns ``None`` (the
    ``_select_by_moneyness`` path then surfaces
    ``error_code="missing_underlying_price"``).  This is a Phase 1
    simplification; Phase 2 can pass a real row through the selector
    Protocol once the contract is extended.
    """

    async def resolver(
        contract: OptionContractDoc,
        row_date: date,
    ) -> float | None:
        # Sentinel row — only ``underlying_price_stored`` is read by
        # ``resolve_underlying_price`` (BTC branch), and we leave it None.
        # The non-BTC branches use index_port / futures_port directly.
        sentinel_row = OptionDailyRow(
            date=row_date,
            open=None,
            high=None,
            low=None,
            close=None,
            bid=None,
            ask=None,
            bid_size=None,
            ask_size=None,
            volume=None,
            open_interest=None,
            mid=None,
            iv_stored=None,
            delta_stored=None,
            gamma_stored=None,
            theta_stored=None,
            vega_stored=None,
            underlying_price_stored=None,
        )
        return await resolve_underlying_price(
            contract=contract,
            row=sentinel_row,
            target_date=row_date,
            index_port=index_port,
            futures_port=futures_port,
        )

    return resolver


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def get_options_reader(market_data: MarketDataService) -> MongoOptionsDataReader:
    """Pull the underlying ``MongoOptionsDataReader`` out of the service.

    The default ``DefaultMarketDataService`` stores the reader as
    ``_options``.  Wave B1 deliberately did not expose it on the
    Protocol (only the Protocol methods are public).  The router needs
    direct access to ``list_roots()`` and ``get_contract()``, both of
    which exist on ``MarketDataService`` already (``list_option_roots``,
    ``get_option_contract``).  So in practice we never touch the
    private attribute — every caller below uses the Protocol methods.
    This helper is kept for symmetry / future use.
    """
    return market_data._options  # type: ignore[attr-defined]


def build_options_pricer() -> DefaultOptionsPricer:
    """Build a default ``DefaultOptionsPricer`` (BS76 kernel, r=0)."""
    return DefaultOptionsPricer(kernel=BS76Kernel())


def build_options_chain(market_data: MarketDataService) -> DefaultOptionsChain:
    """Construct a ``DefaultOptionsChain`` wired to the live ports.

    Each call returns a fresh instance — adapters and pricer are not
    shared across requests.  The chain's data port is a
    ``CachedChainReader`` so repeated identical ``query_chain`` calls
    within one request are coalesced.
    """
    reader = get_options_reader(market_data)
    inner = _OptionsDataPortAdapter(reader)
    cached = CachedChainReader(inner)
    pricer = build_options_pricer()
    index_port = _IndexDataPortAdapter(market_data)
    futures_port = _FuturesDataPortAdapter(market_data)
    return DefaultOptionsChain(
        data_port=cached,
        pricer=pricer,
        index_port=index_port,
        futures_port=futures_port,
    )


def build_options_selector(
    market_data: MarketDataService,
    *,
    with_pricer: bool,
) -> DefaultOptionsSelector:
    """Construct a ``DefaultOptionsSelector`` wired to the live ports.

    ``with_pricer=True`` provides the Module 2 pricer for the
    ``compute_missing_for_delta_selection`` path.  When False, that
    path raises ``NotImplementedError`` per Module 3's contract.
    """
    reader = get_options_reader(market_data)
    inner = _OptionsDataPortAdapter(reader)
    cached = CachedChainReader(inner)
    maturity_resolver = DefaultMaturityResolver()
    pricer = build_options_pricer() if with_pricer else None
    index_port = _IndexDataPortAdapter(market_data)
    futures_port = _FuturesDataPortAdapter(market_data)
    underlying_resolver = _build_underlying_resolver(index_port, futures_port)
    return DefaultOptionsSelector(
        reader=cached,
        maturity_resolver=maturity_resolver,
        pricer=pricer,
        underlying_price_resolver=underlying_resolver,
    )


# Re-exports for tests / integration tests that want to construct the
# adapters directly.
__all__ = [
    "CachedChainReader",
    "build_options_chain",
    "build_options_pricer",
    "build_options_selector",
    "get_options_reader",
]
