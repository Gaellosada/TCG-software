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
from typing import Awaitable, Callable, Literal, Mapping, Sequence

from tcg.data._utils import date_to_int
from tcg.data.options.protocol import OptionsDataReader
from tcg.data.protocols import MarketDataService
from tcg.engine.options.chain._join import resolve_underlying_price
from tcg.engine.options.chain.chain import DefaultOptionsChain
from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.pricing.pricer import DefaultOptionsPricer
from tcg.engine.options.selection.selector import DefaultOptionsSelector
from tcg.types.market import FuturesContractMeta
from tcg.types.options import OptionContractDoc, OptionDailyRow


# ---------------------------------------------------------------------------
# Port adapters
# ---------------------------------------------------------------------------


class _OptionsDataPortAdapter:
    """Wrap an ``OptionsDataReader`` to satisfy the engine-side
    ``OptionsDataPort`` / ``ChainReaderPort`` Protocols.

    The shape is identical; this class exists primarily so the wiring
    module references the engine-side contract explicitly and so we can
    later interpose telemetry without touching engine code.
    """

    def __init__(self, reader: OptionsDataReader) -> None:
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
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        return await self._reader.query_chain(
            root=root,
            date=date,
            type=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
            expiration_cycle=expiration_cycle,
        )


class _BulkOptionsDataPortAdapter:
    """Wrap an ``OptionsDataReader`` to satisfy the engine-side
    ``_CycleAwareBulkReader`` Protocol (cycle-aware variant).

    The engine's ``_CycleInjectingBulkReader`` strips ``expiration_cycle``
    from its public signature and injects it here.  This adapter sits
    between the engine wrapper and the real data reader.
    """

    def __init__(self, reader: OptionsDataReader) -> None:
        self._reader = reader
        # Optional year-chunk fast path: advertise the multi-expiration bulk
        # capability only when the concrete reader implements it, so the engine
        # feature-detects and falls back to the per-expiration path otherwise.
        # ``callable`` (not ``hasattr``): a reader that DISABLES the capability
        # by setting ``query_chain_bulk_multi = None`` must NOT be advertised as
        # capable — ``hasattr`` returns True for a None-valued attribute, which
        # would drive the fast path into a TypeError + silent slow fallback.
        self.supports_bulk_multi = callable(
            getattr(reader, "query_chain_bulk_multi", None)
        )
        # Optional hold-leg two-phase Phase-2 capability (same feature-detect
        # discipline as ``supports_bulk_multi``): a reader that disables it via
        # ``query_held_rows = None`` reports False, so the engine cleanly stays on
        # the full-chain hold path instead of entering it → TypeError.
        self.supports_held_rows = callable(getattr(reader, "query_held_rows", None))

    async def query_chain_bulk(
        self,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        return await self._reader.query_chain_bulk(
            root=root,
            dates=dates,
            type=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
            expiration_cycle=expiration_cycle,
        )

    async def query_chain_bulk_multi(
        self,
        root: str,
        type: Literal["C", "P", "both"],
        groups: Sequence[tuple[date, Sequence[date]]],
        strike_windows: "Mapping[date, tuple[float | None, float | None]] | None" = None,
        expiration_cycle: str | Sequence[str] | None = None,
        delta_pushdown: "tuple[float, int] | None" = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        return await self._reader.query_chain_bulk_multi(
            root=root,
            type=type,
            groups=groups,
            strike_windows=strike_windows,
            expiration_cycle=expiration_cycle,
            delta_pushdown=delta_pushdown,
        )

    async def query_held_rows(
        self,
        root: str,
        type: Literal["C", "P", "both"],
        held_windows: "Sequence[tuple[str, date, date]]",
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        return await self._reader.query_held_rows(
            root=root,
            type=type,
            held_windows=held_windows,
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
        self._cache: dict[tuple, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}

    async def query_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        key = (
            root,
            date,
            type,
            expiration_min,
            expiration_max,
            strike_min,
            strike_max,
            expiration_cycle,
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
            expiration_cycle=expiration_cycle,
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
        target_int = date_to_int(target_date)
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

    Per-resolve memoization (perf)
    ------------------------------
    The option-stream resolver's ByMoneyness/ByDelta Phase C resolves the
    underlying future PER TRADE DATE — but all dates of an expiration share ONE
    front-quarterly future, so a naive per-date single-date ``get_prices`` is
    ~97% redundant (the OPT_SP_500 portfolio-leg N+1: ~1500 fetches for ~36
    distinct futures).  When ``prefetch_window`` is supplied, the FIRST close
    lookup for a given ``contract_ref`` fetches that future's closes over the
    WHOLE window in one ranged ``get_prices`` (mirroring
    ``_batch_underlying_prices``) and caches ``{trade_date: close}``; every later
    date is served from the cache.  The front-FUT id resolution
    (``find_front_...``) is memoized too, per ``(collection, expiration_int)``.

    RESULT-INVARIANT: the cached close for a date is exactly the value a
    single-date fetch would return (same stored data) — only the number of
    round-trips changes.  The cache is scoped to ONE adapter instance = ONE
    resolve (a fresh adapter is built per ``build_stream_resolver_wiring`` call),
    so there is no cross-request staleness.  ``prefetch_window=None`` preserves
    the exact prior per-date behaviour.
    """

    def __init__(
        self,
        market_data: MarketDataService,
        prefetch_window: "tuple[date, date] | None" = None,
    ) -> None:
        self._md = market_data
        self._prefetch_window = prefetch_window
        # contract_ref -> {trade_date_int: close}; None marks a fetch that failed
        # or returned nothing (so we don't re-hit the dwh for that future).
        self._close_cache: dict[str, dict[int, float] | None] = {}
        # (kind, collection, expiration_int) -> resolved FUT id (or None); ``kind``
        # ("exact" | "front") separates the VIX exact-match namespace from the
        # front-quarterly (>=) namespace so they never collide on the same key.
        self._front_id_cache: dict[tuple[str, str, int], str | None] = {}

    async def _window_closes(
        self, collection: str, contract_ref: str
    ) -> dict[int, float] | None:
        """Return (and cache) ``{trade_date_int: close}`` for ``contract_ref`` over
        the prefetch window, fetched in ONE ranged ``get_prices``.  Cached per
        adapter (per resolve)."""
        if contract_ref in self._close_cache:
            return self._close_cache[contract_ref]
        assert self._prefetch_window is not None
        start, end = self._prefetch_window
        try:
            series = await self._md.get_prices(
                collection, contract_ref, start=start, end=end
            )
        except Exception:  # noqa: BLE001
            self._close_cache[contract_ref] = None
            return None
        if series is None or len(series) == 0:
            self._close_cache[contract_ref] = None
            return None
        closes = {
            int(d): float(series.close[i]) for i, d in enumerate(series.dates.tolist())
        }
        self._close_cache[contract_ref] = closes
        return closes

    async def get_futures_close_on_date(
        self,
        collection: str,
        contract_ref: str,
        target_date: date,
    ) -> float | None:
        target_int = date_to_int(target_date)
        # Memoized path: serve from the one ranged fetch per future.
        if self._prefetch_window is not None:
            closes = await self._window_closes(collection, contract_ref)
            if closes is None:
                return None
            return closes.get(target_int)
        # Legacy per-date path (unchanged) when no window was supplied.
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
        for idx, d in enumerate(series.dates.tolist()):
            if int(d) == target_int:
                return float(series.close[idx])
        return None

    async def get_futures_close_by_expiration(
        self,
        collection: str,
        expiration: date,
        target_date: date,
    ) -> float | None:
        """Find the FUT_* contract whose ``expiration`` field equals
        ``expiration`` (YYYYMMDD int in Mongo), and return its
        ``eodDatas.close`` on ``target_date``. Returns ``None`` when no
        FUT_* contract matches (e.g. weekly VIX option) or the matching
        contract has no bar for the target date.

        Used by the OPT_VIX branch of the underlying-price resolver in
        Phase 2 of the VIX greeks rollout. The legacy schema stores
        ``expiration`` as ``YYYYMMDD`` int per ``_parse_expiration`` in
        ``tcg.data._mongo.instruments``. Delegates to the public
        ``MarketDataService.find_futures_contract_by_expiration`` method
        rather than reaching into private attributes.
        """
        expiration_int = date_to_int(expiration)
        key = ("exact", collection, expiration_int)
        if key in self._front_id_cache:
            contract_ref = self._front_id_cache[key]
        else:
            try:
                contract_ref = await self._md.find_futures_contract_by_expiration(
                    collection, expiration_int
                )
            except Exception:  # noqa: BLE001
                contract_ref = None
            self._front_id_cache[key] = contract_ref
        if contract_ref is None:
            return None
        return await self.get_futures_close_on_date(
            collection, contract_ref, target_date
        )

    async def get_futures_close_on_or_after_expiration(
        self,
        collection: str,
        expiration: date,
        target_date: date,
    ) -> float | None:
        """Find the FRONT-QUARTERLY future — the nearest FUT_* contract in
        ``collection`` whose ``expiration`` is >= ``expiration`` — and return its
        ``close`` on ``target_date``.

        Used by the option-on-future underlying resolver (``_join`` Branch 3) for
        roots without a per-contract ``underlying_ref`` (the dwh SQL reader does
        not preserve it).  ``>=`` (not exact) because index/commodity futures are
        quarterly while options list serial months + weeklies, which settle
        against the front quarterly future.  Delegates to the public
        ``MarketDataService.find_front_futures_contract_on_or_after`` (no private
        attribute access).  Returns ``None`` when no future expires on/after the
        option or the resolved contract has no bar for ``target_date``.  The
        resolved front-FUT id is memoized per ``(collection, expiration_int)`` so
        the ~N-per-window per-date lookups collapse to one id resolution per
        distinct expiration (see the class docstring).
        """
        expiration_int = date_to_int(expiration)
        key = ("front", collection, expiration_int)
        if key in self._front_id_cache:
            contract_ref = self._front_id_cache[key]
        else:
            try:
                contract_ref = await self._md.find_front_futures_contract_on_or_after(
                    collection, expiration_int
                )
            except Exception:  # noqa: BLE001
                contract_ref = None
            self._front_id_cache[key] = contract_ref
        if contract_ref is None:
            return None
        return await self.get_futures_close_on_date(
            collection, contract_ref, target_date
        )


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


def get_options_reader(market_data: MarketDataService) -> OptionsDataReader:
    """Return the ``OptionsDataReader`` from the service.

    Accesses the public ``options_reader`` property defined on the
    ``MarketDataService`` protocol, avoiding any private attribute access.
    """
    return market_data.options_reader


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


def build_stream_resolver_wiring(
    market_data: MarketDataService,
    underlying_prefetch_window: "tuple[date, date] | None" = None,
) -> tuple[
    CachedChainReader,
    DefaultMaturityResolver,
    Callable[[OptionContractDoc, date], Awaitable[float | None]],
    _BulkOptionsDataPortAdapter,
]:
    """Return the components a per-date stream materialiser needs.

    The Wave 2 ``OptionStreamRef`` resolver wraps the chain reader in a
    cycle-injecting proxy and constructs its own selector inside the
    engine layer (so the engine never imports from ``tcg.data`` or
    ``tcg.core``).  This factory hands the engine the four live
    components without imposing the selector class on it.

    ``underlying_prefetch_window`` (``(start, end)``): passed to the futures
    adapter so the per-date underlying lookups (ByMoneyness/ByDelta Phase C)
    collapse to ONE ranged fetch per distinct future over that window instead of
    a single-date fetch per trade date (the OPT_SP_500 portfolio-leg N+1).
    Result-invariant memoization scoped to THIS wiring (one resolve); ``None``
    keeps the per-date behaviour.  Callers pass their resolve window.

    Returns
    -------
    chain_reader:
        Cycle-aware ``CachedChainReader`` (per-request cache) wrapping
        the live ``MongoOptionsDataReader``.
    maturity_resolver:
        ``DefaultMaturityResolver`` (stateless).
    underlying_resolver:
        Async callable ``(contract, date) -> float | None`` reusing the
        canonical ``resolve_underlying_price`` join.
    bulk_chain_reader:
        Cycle-aware ``_BulkOptionsDataPortAdapter`` wrapping the live
        ``MongoOptionsDataReader``.  Passed to
        ``resolve_option_stream(bulk_chain_reader=...)`` to enable the
        three-phase bulk pre-fetch path.
    """
    reader = get_options_reader(market_data)
    inner = _OptionsDataPortAdapter(reader)
    cached = CachedChainReader(inner)
    bulk = _BulkOptionsDataPortAdapter(reader)
    maturity_resolver = DefaultMaturityResolver()
    index_port = _IndexDataPortAdapter(market_data)
    futures_port = _FuturesDataPortAdapter(
        market_data, prefetch_window=underlying_prefetch_window
    )
    underlying_resolver = _build_underlying_resolver(index_port, futures_port)
    return cached, maturity_resolver, underlying_resolver, bulk


def _pick_reference_contract(
    metas: "Sequence[FuturesContractMeta]",
    option_expiry: date,
    futures_reference: str,
) -> "FuturesContractMeta | None":
    """Select the reference futures contract for an option expiry.

    ``metas`` is ascending by ``(expiration, symbol)`` (as
    ``list_futures_contract_meta`` returns).
      * ``nearest_on_or_after`` — the FIRST contract expiring >= the option expiry
        (root's real listed cycle); None if the option outlives the curve.
      * ``nearest_abs`` — the contract whose expiration is closest in |time| to the
        option expiry (before OR after).  Ties (equidistant before/after) break
        toward the on/after contract (the more conservative reference), then toward
        the earlier expiration — both deterministic.

    WEEKLY contracts (``expiration_cycle == 'W'``) are never a sizing reference on
    a multi-cycle root: the docstrings promise the root's REAL cycle (monthly for
    VIX, quarterly for SP/NDX), and a weekly VX future that happens to expire close
    to the option would mis-anchor the notional.  Weeklies are dropped whenever any
    non-weekly candidate remains; if the root is ALL weekly (degenerate) the full
    set is kept rather than refusing to size.  Single-cycle roots carry an empty
    ``expiration_cycle`` (never 'W') so their selection is unchanged.
    """
    if not metas:
        return None
    regular = [c for c in metas if c.expiration_cycle != "W"]
    if regular:
        metas = regular
    if futures_reference == "nearest_on_or_after":
        for c in metas:  # ascending → first >= is the nearest on/after
            if c.expiration >= option_expiry:
                return c
        return None

    # nearest_abs
    def _key(c: "FuturesContractMeta") -> tuple:
        delta = abs((c.expiration - option_expiry).days)
        after = 0 if c.expiration >= option_expiry else 1  # prefer on/after on tie
        return (delta, after, c.expiration)

    return min(metas, key=_key)


def build_futures_reference_resolver(
    market_data: MarketDataService,
    *,
    option_collection: str,
    futures_reference: str,
    prefetch_window: "tuple[date, date] | None" = None,
) -> Callable[[date, date], Awaitable["tuple[float, float | None] | None"]]:
    """Build the per-roll reference-future resolver for futures-notional sizing.

    Maps ``OPT_<root>`` → ``FUT_<root>`` BY NAME (Guardrail Sign 3 — never
    ``underlying_id``) and returns an async ``(roll_date, option_expiry) ->
    (close_price, contract_size)`` closure the option-stream resolver calls at each
    roll.  ``contract_size`` is the LIVE ``M_fut`` (None where the dwh row is NULL →
    signed-off config fallback).  Reuses the window-memoized ``_FuturesDataPortAdapter``
    for the close read (same partition-pruning / N+1 protections as the
    underlying-price path); the contract listing is fetched once and cached.

    Modes:
      * ``nearest_on_or_after`` (DEFAULT) — nearest LISTED future expiring >= the
        option expiry (monthly VIX / quarterly SP/NDX).
      * ``nearest_abs`` — future whose expiration is closest in |time|.
      * ``continuous_front`` — NOT yet wired (no continuous-front hookup here); the
        closure raises so the caller surfaces a clear not-implemented error rather
        than mis-sizing.  The field still validates upstream.
    """
    from tcg.types.multipliers import futures_collection_for_option

    fut_collection = futures_collection_for_option(option_collection)
    futures_port = _FuturesDataPortAdapter(market_data, prefetch_window=prefetch_window)

    if futures_reference not in ("nearest_on_or_after", "nearest_abs"):

        async def _not_implemented(
            roll_date: date, option_expiry: date
        ) -> "tuple[float, float | None] | None":
            raise NotImplementedError(
                f"futures_reference={futures_reference!r} is not yet implemented; "
                f"use 'nearest_on_or_after' or 'nearest_abs'"
            )

        return _not_implemented

    # One cached contract listing per closure (per resolve); result-invariant.
    _meta_cache: dict[str, "list[FuturesContractMeta]"] = {}

    async def _metas() -> "list[FuturesContractMeta]":
        cached = _meta_cache.get(fut_collection)
        if cached is None:
            # A real DB fault (pool timeout / dropped socket) surfaces as
            # DataAccessError and MUST propagate so the request fails loudly —
            # swallowing it to [] would be indistinguishable from a genuine
            # "no covering future" and silently carry the whole leg forward.
            # A genuinely EMPTY result ([]) is a real answer and is cached; the
            # exception path is NOT cached so a retry after a transient fault can
            # succeed.
            cached = list(await market_data.list_futures_contract_meta(fut_collection))
            _meta_cache[fut_collection] = cached
        return cached

    async def _resolve(
        roll_date: date, option_expiry: date
    ) -> "tuple[float, float | None] | None":
        target = _pick_reference_contract(
            await _metas(), option_expiry, futures_reference
        )
        if target is None:
            return None
        price = await futures_port.get_futures_close_on_date(
            fut_collection, target.symbol, roll_date
        )
        if price is None:
            return None
        return (float(price), target.contract_size)

    return _resolve


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
    "build_stream_resolver_wiring",
    "get_options_reader",
]
