"""Public data-access namespace for the mongoDB-backtester.

This module is a **re-export layer** over ``lib.data_load`` plus a small set of
new helpers (instrument listing, expiry listing, raw-DB escape hatch,
composite-OPT-`_id` round-trip helpers, benchmark-shape normalization). The
loaders themselves live in ``lib.data_load`` and are unchanged — every public
callable here is the *same callable* (identity, not a copy) so existing imports
in tests, ``lib.engine``, and snippets keep working.

Round-trip helpers for composite OPT `_id` are exposed as
``serialize_doc_id`` / ``deserialize_doc_id`` (public aliases of the
underscore-prefixed ``lib.data_load._serialize_id`` /
``lib.data_load._deserialize_id``). Use them when ``raw_db()`` returns OPT_*
docs and you need to stash the dict ``_id`` as a string key.

Usage:

    from lib import data

    bars = data.fetch_index_bars("IND_SP_500", start=20240101, end=20241231)
    chain = data.load_option_chain(data.raw_db(), "SPX", asof_date=20240315)
    expiries = data.list_option_expiries("SPX", dte_band=(20, 60))

The module also exposes **advisory** constants (``KNOWN_ASSET_CLASSES``,
``KNOWN_INDEX_ROOTS``, ``KNOWN_ETF_ROOTS``, ``KNOWN_OPTION_ROOTS``) for tab
completion / SCHEMA.md cross-reference. They are NOT gatekeeping: the dispatch
loaders accept any string and the advisory helpers raise ``LookupError`` (with
a helpful message) only when the agent passes an asset class that the helper
itself does not know how to enumerate.

See ``lib/data/SCHEMA.md`` for the per-collection document layout (read this
once at intake — every collection's _id shape, provider priority, gotchas).
"""
from __future__ import annotations

import functools
from typing import Any, Literal

import numpy as np

from .. import data_load as _data_load
from .. import mongo as _mongo
from ..mongo import MongoWriteForbiddenError

# ---------------------------------------------------------------------------
# Re-exports (identity, not copies). Tests verify
# `lib.data.X is lib.data_load.X` for every name below.
# ---------------------------------------------------------------------------

# --- Dataclasses ---
PriceSeries = _data_load.PriceSeries
OptionDailyRow = _data_load.OptionDailyRow
OptionContractSeries = _data_load.OptionContractSeries
OptionChainSnapshot = _data_load.OptionChainSnapshot
SeriesRecord = _data_load.SeriesRecord

# --- Async loaders (per-asset-class) ---
load_index_bars = _data_load.load_index_bars
load_etf_bars = _data_load.load_etf_bars
load_fund_bars = _data_load.load_fund_bars
load_forex_bars = _data_load.load_forex_bars
load_equity_bars = _data_load.load_equity_bars

# --- Async loaders (futures) ---
list_futures_contracts = _data_load.list_futures_contracts
load_futures_contract = _data_load.load_futures_contract
load_continuous_futures = _data_load.load_continuous_futures

# --- Async loaders (options) ---
load_option_chain = _data_load.load_option_chain
load_option_contract_series = _data_load.load_option_contract_series

# --- Async helpers ---
list_collections_for_root = _data_load.list_collections_for_root

# --- Sync wrappers (used by snippets / scripts that don't manage a loop) ---
load_index_bars_sync = _data_load.load_index_bars_sync
load_etf_bars_sync = _data_load.load_etf_bars_sync
load_fund_bars_sync = _data_load.load_fund_bars_sync
load_forex_bars_sync = _data_load.load_forex_bars_sync
load_equity_bars_sync = _data_load.load_equity_bars_sync
load_futures_contract_sync = _data_load.load_futures_contract_sync
load_continuous_futures_sync = _data_load.load_continuous_futures_sync
load_option_chain_sync = _data_load.load_option_chain_sync
load_option_contract_series_sync = _data_load.load_option_contract_series_sync
list_futures_contracts_sync = _data_load.list_futures_contracts_sync
list_collections_for_root_sync = _data_load.list_collections_for_root_sync

# --- Singular sync alias used in snippets ---
load_continuous_future = _data_load.load_continuous_future

# --- Generic dispatcher ---
load_bars = _data_load.load_bars

# --- Convenience fetch wrappers (open the read-only sync handle internally) ---
fetch_index_bars = _data_load.fetch_index_bars
fetch_etf_bars = _data_load.fetch_etf_bars
fetch_continuous_future = _data_load.fetch_continuous_future

# --- npz I/O ---
save_bars_npz = _data_load.save_bars_npz
save_npz = _data_load.save_npz
load_npz = _data_load.load_npz
save_signal_npz = _data_load.save_signal_npz
load_signal_npz = _data_load.load_signal_npz

# --- data_summary writer (canonical schema for pipeline/02-data.md) ---
write_data_summary = _data_load.write_data_summary

# --- Documented constant from data_load (used by lib.options as the canonical projection) ---
_OPTIONS_DOC_PROJECTION = _data_load._OPTIONS_DOC_PROJECTION
CYCLE_LETTERS = _data_load.CYCLE_LETTERS

# --- Round-trip helpers for composite OPT _ids (public aliases of the
#     underscore-prefixed originals; the underscore convention in
#     data_load.py is load-bearing for internal callers, so we re-export
#     rather than rename in place). Identity is verified by tests. ---
serialize_doc_id = _data_load._serialize_id
deserialize_doc_id = _data_load._deserialize_id


# ---------------------------------------------------------------------------
# Advisory constants — NEVER gatekeeping (Sign 1).
#
# These are seeded from `lib.data_load._PROVIDER_PRIORITY` keys + the
# real-Mongo verified ids (see data_load.py:5-12 for the live-verification
# block: IND_SP_500, IND_VIX, IND_NDX_100, IND_RUT_2000, ETF_SPY, BTC_USD).
# They are exposed as tuples (frozen at import time) so an agent can use them
# for tab-completion / SCHEMA cross-reference. The dispatch loaders do NOT
# consult these tuples and accept any string instrument id / asset class.
# ---------------------------------------------------------------------------

#: Known asset-class strings. Note: ``FUTURE`` and ``OPTION`` are
#: collection-prefix shorthands (``FUT_*`` / ``OPT_*``) and are NOT keys for
#: ``load_bars`` — they identify groups of collections fetched via
#: ``load_continuous_futures`` / ``load_option_chain`` instead.
KNOWN_ASSET_CLASSES: tuple[str, ...] = (
    "INDEX", "ETF", "FUND", "FOREX", "EQUITY", "FUTURE", "OPTION",
)

#: Bar-loader-compatible asset classes (subset of ``KNOWN_ASSET_CLASSES``).
#: Pass any of these to ``load_bars(asset_class=...)``.
BAR_LOADER_ASSET_CLASSES: tuple[str, ...] = (
    "INDEX", "ETF", "FUND", "FOREX", "EQUITY",
)

#: Real-Mongo verified INDEX ``_id`` values (advisory). Not exhaustive — the
#: production DB ships variants beyond the canonical 4 (notably the VIX
#: term-structure: VIX9D, VIX3M, VIX6M; plus VVIX). Use ``list_instruments(
#: asset_class="INDEX")`` for the live set when designing a strategy that
#: depends on a specific instrument being present.
KNOWN_INDEX_ROOTS: tuple[str, ...] = (
    "IND_SP_500", "IND_NDX_100", "IND_RUT_2000",
    "IND_VIX", "IND_VIX_9D", "IND_VIX_3M", "IND_VIX_6M", "IND_VVIX",
)

#: Real-Mongo verified ETF ``_id`` values (advisory; not exhaustive).
KNOWN_ETF_ROOTS: tuple[str, ...] = (
    "ETF_SPY",
)

#: Roots known to ship an ``OPT_<ROOT>`` collection (advisory; verified live
#: against ``tcg-instrument`` on 2026-05-05). The collection-name suffix is
#: NOT the same as the underlying-symbol ticker — e.g. the S&P 500 options
#: live in ``OPT_SP_500`` (root = ``"SP_500"``), not ``OPT_SPX``. Use these
#: literal strings when calling ``list_option_expiries`` /
#: ``load_option_chain``. For the live set, query
#: ``raw_db().list_collection_names()`` and filter on ``OPT_*``.
KNOWN_OPTION_ROOTS: tuple[str, ...] = (
    "SP_500", "VIX", "NASDAQ_100",
    "T_BOND", "T_NOTE_10_Y",
    "BTC", "ETH", "EURUSD", "GOLD", "JPYUSD",
)


# ---------------------------------------------------------------------------
# New helpers (live in this module, not in data_load.py)
# ---------------------------------------------------------------------------


def raw_db() -> Any:
    """Return the read-only proxy database for ad-hoc queries.

    This is the **escape hatch** for queries the lib doesn't cover. The
    returned object is the existing ``mongo.sync_db()`` wrapper; write
    attempts (``insert_one``, ``update_one``, ``aggregate`` with ``$out`` /
    ``$merge`` stages, etc.) raise :class:`MongoWriteForbiddenError` before
    any byte hits the wire.

    If you reach for this often for the same access pattern, the right fix
    is to add a first-class ``lib.data`` helper rather than embedding the
    raw query in strategy code — keep strategies semantically simple and
    let the lib own the schema.
    """
    return _mongo.sync_db()


@functools.lru_cache(maxsize=None)
def _live_collection_names() -> tuple[str, ...]:
    """Process-wide cached snapshot of ``db.list_collection_names()``.

    Cleared by ``_clear_caches`` (test hook). Lazy: only hits Mongo on the
    first call. Cached because list-collection-names is a relatively expensive
    server round-trip and the live set is effectively immutable for the
    lifetime of a backtester run.
    """
    db = _mongo.sync_db()
    # `_SyncDB.__getattr__` falls back to `_dl.list_collection_names_sync` for
    # any name ending in `_sync`, but `list_collection_names` is a real
    # method on `_ReadOnlyDatabase` (proxied to the underlying Motor / pymongo
    # database) — call it directly.
    names = db._db.list_collection_names()
    if hasattr(names, "__await__"):
        # Async path: `_ReadOnlyDatabase` proxies a Motor DB whose
        # `list_collection_names` is async. Drive via sync_run.
        names = _mongo.sync_run(names)  # type: ignore[arg-type]
    return tuple(sorted(names))


def _clear_caches() -> None:
    """Test hook: clear all process-wide caches in this module.

    Covers ``_live_collection_names``, ``_list_instruments_for_class``,
    plus the live-root helpers (``live_index_roots`` / ``live_etf_roots``
    / ``live_option_roots`` / ``live_futures_roots``). Used by tests to
    prevent cross-test cache leakage; production callers should never
    need to invoke this.
    """
    _live_collection_names.cache_clear()
    _list_instruments_for_class.cache_clear()
    # The live_*_roots helpers may not be defined yet at module import
    # time if this function is invoked very early; guard with hasattr.
    for name in ("live_index_roots", "live_etf_roots", "live_option_roots", "live_futures_roots"):
        fn = globals().get(name)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()


_ASSET_CLASS_TO_COLL: dict[str, str] = {
    "INDEX": "INDEX",
    "ETF": "ETF",
    "FUND": "FUND",
    "FOREX": "FOREX",
    "EQUITY": "EQUITY",
}


@functools.lru_cache(maxsize=None)
def _list_instruments_for_class(asset_class: str) -> tuple[dict[str, Any], ...]:
    """Cached per-class instrument listing. Returns a tuple of frozen dicts.

    Hits Mongo via ``raw_db()[<coll>].find({}, {"_id": 1, "eodDatas": 1})``
    once per asset class per process. Subsequent calls hit the cache.
    """
    coll = _ASSET_CLASS_TO_COLL.get(asset_class)
    if coll is None:
        # FUTURE / OPTION groups: enumerate FUT_* / OPT_* collection names
        # rather than docs. Strategies typically don't need a flat list of
        # contract docs (use list_futures_contracts / list_option_expiries).
        prefix = "FUT_" if asset_class == "FUTURE" else "OPT_"
        names = _live_collection_names()
        return tuple(
            {"id": n[len(prefix):], "asset_class": asset_class, "providers": []}
            for n in names if n.startswith(prefix)
        )
    db = _mongo.sync_db()
    if coll not in _live_collection_names():
        return ()
    # Project _id + eodDatas keys (top-level only; we don't need bar rows).
    out: list[dict[str, Any]] = []
    raw_coll = db._db[coll]
    cursor = raw_coll.find({}, {"_id": 1, "eodDatas": 1})
    if hasattr(cursor, "__aiter__"):
        # Async cursor; collect via sync_run.
        async def _collect() -> list[dict]:
            docs: list[dict] = []
            async for d in cursor:
                docs.append(d)
            return docs
        docs = _mongo.sync_run(_collect())
    else:
        docs = list(cursor)
    for d in docs:
        eod = d.get("eodDatas") or {}
        providers = sorted(eod.keys()) if isinstance(eod, dict) else []
        out.append(
            {
                "id": _data_load._serialize_id(d.get("_id")),
                "asset_class": asset_class,
                "providers": providers,
            }
        )
    return tuple(out)


def list_instruments(*, asset_class: str | None = None) -> list[dict[str, Any]]:
    """List known instruments, optionally filtered by asset class.

    Returns a list of dicts of the form::

        {"id": "IND_SP_500", "asset_class": "INDEX", "providers": ["YAHOO"]}

    Live data: instrument ids and per-doc provider keys are queried through
    ``raw_db()`` and **cached process-wide** (``functools.lru_cache``). Static
    metadata (asset-class names, known root prefixes) lives in this module's
    ``KNOWN_*`` constants.

    The ``asset_class`` filter is **advisory** — passing an unknown class
    raises :class:`LookupError` with the known classes listed, but the
    underlying loaders themselves take any string (Sign 1: no closed enums).

    Args:
        asset_class: One of ``KNOWN_ASSET_CLASSES`` (case-insensitive). When
            ``None``, returns instruments across all known asset classes.

    Returns:
        List of instrument metadata dicts. Empty list when the asset class
        is known but no documents match (e.g., ``EQUITY`` collection absent).

    Raises:
        LookupError: When ``asset_class`` is provided but not in
            ``KNOWN_ASSET_CLASSES``.
    """
    if asset_class is not None:
        ac = str(asset_class).upper()
        if ac not in KNOWN_ASSET_CLASSES:
            raise LookupError(
                f"unknown asset_class={asset_class!r}; advisory list of known "
                f"classes: {KNOWN_ASSET_CLASSES}. (The dispatch loaders accept "
                f"any string — this helper only enumerates classes it knows "
                f"how to query.)"
            )
        return list(_list_instruments_for_class(ac))
    out: list[dict[str, Any]] = []
    for ac in KNOWN_ASSET_CLASSES:
        out.extend(_list_instruments_for_class(ac))
    return out


def list_option_expiries(
    underlying: str,
    *,
    dte_band: tuple[int, int] | None = None,
    as_of: int | None = None,
) -> list[int]:
    """List sorted distinct YYYYMMDD expirations for the OPT_<underlying> collection.

    Reads via ``raw_db()[coll].distinct("expiration")`` — no caching: a
    backtester can run across years of data and "today" changes each call,
    so we stay live.

    Args:
        underlying: Option root (e.g., ``"SPX"``). Maps to collection
            ``OPT_<UNDERLYING>``.
        dte_band: ``(min_dte, max_dte)`` to filter by days-to-expiry from
            ``as_of``. Inclusive on both ends. ``None`` returns all
            expirations.
        as_of: YYYYMMDD reference date for the DTE computation. Defaults to
            today (UTC) when ``None``.

    Returns:
        Sorted ascending list of distinct YYYYMMDD ints. Returns ``[]`` when
        the OPT_<underlying> collection is not present in the live DB
        (mirrors ``list_instruments``: helpers don't gatekeep — an empty
        list is the same shape an agent gets when filtering yields no
        match, and a typo in ``underlying`` shows up as the same empty
        result the agent can investigate via ``list_instruments``).
    """
    coll = f"OPT_{underlying.upper()}"
    if coll not in _live_collection_names():
        return []
    db = _mongo.sync_db()
    raw_coll = db._db[coll]
    distinct = raw_coll.distinct("expiration")
    if hasattr(distinct, "__await__"):
        distinct = _mongo.sync_run(distinct)
    expiries: list[int] = []
    for v in distinct:
        try:
            expiries.append(_data_load._parse_expiration(v))
        except ValueError:
            continue
    expiries.sort()
    if dte_band is None:
        return expiries
    if as_of is None:
        from datetime import date as _date
        today = _date.today()
        as_of_int = today.year * 10000 + today.month * 100 + today.day
    else:
        as_of_int = int(as_of)
    min_dte, max_dte = int(dte_band[0]), int(dte_band[1])
    return [e for e in expiries if min_dte <= _dte_calendar(as_of_int, e) <= max_dte]


def normalize_benchmark(
    meta_value: Any,
    *,
    default_asset_class: str = "INDEX",
) -> dict[str, str]:
    """Normalize a ``META['benchmark']`` value to a canonical ``{symbol, asset_class}`` dict.

    ``META.benchmark`` accepts both a bare string id AND a dict (see CLAUDE.md
    "META keys / benchmark"). Strategies that need to read benchmark data each
    re-implement the type-switching; this helper centralizes the logic so the
    shape stays consistent across strategy code, ``_build_benchmark_bars``,
    and ad-hoc analysis snippets.

    Accepted shapes:

    - ``str`` -> ``{"symbol": <str>, "asset_class": default_asset_class}``
    - ``{"symbol": ..., "asset_class": ...}`` -> returned (canonical form;
      ``asset_class`` upper-cased; missing ``asset_class`` defaults).
    - ``{"symbol": ...}`` (no asset_class) -> fills ``default_asset_class``.
    - ``{"instrument_id": ..., "asset_class"?: ...}`` -> back-compat. The
      ``instrument_id`` key was used by some legacy fixtures; treat it as
      ``symbol``.

    Raises ``ValueError`` with a helpful message on any other shape (None,
    empty string, dict missing both ``symbol`` and ``instrument_id``, etc.).

    .. note::
        ``asset_class`` is **upper-cased on output** to match the dispatch
        convention used by ``load_bars`` (the existing collections —
        ``INDEX``, ``ETF``, ``FUND``, ``FOREX``, ``EQUITY`` — are upper-case
        in production). If you pass mixed-case, the output is canonicalized.
        Strategies should not depend on the case being preserved through
        this helper.
    """
    if meta_value is None:
        raise ValueError(
            "normalize_benchmark: meta_value is None; pass a string id or a "
            "{symbol, asset_class} dict (see CLAUDE.md META keys / benchmark)."
        )
    ac_default = str(default_asset_class).upper()
    if isinstance(meta_value, str):
        sym = meta_value.strip()
        if not sym:
            raise ValueError(
                "normalize_benchmark: empty string is not a valid benchmark id"
            )
        return {"symbol": sym, "asset_class": ac_default}
    if isinstance(meta_value, dict):
        sym_raw = meta_value.get("symbol")
        if sym_raw is None:
            sym_raw = meta_value.get("instrument_id")
        if sym_raw is None or not str(sym_raw).strip():
            raise ValueError(
                f"normalize_benchmark: dict {meta_value!r} is missing a "
                f"'symbol' (or back-compat 'instrument_id') key with a "
                f"non-empty value"
            )
        ac_raw = meta_value.get("asset_class") or ac_default
        return {"symbol": str(sym_raw).strip(), "asset_class": str(ac_raw).upper()}
    raise ValueError(
        f"normalize_benchmark: unsupported benchmark shape "
        f"{type(meta_value).__name__}={meta_value!r}; expected str or dict"
    )


def _dte_calendar(asof_yyyymmdd: int, exp_yyyymmdd: int) -> int:
    """Days-to-expiry, calendar days, between two YYYYMMDD ints.

    Uses ``datetime.date`` for the difference so leap years / month
    boundaries are handled exactly. Returns negative DTE when ``exp`` is
    before ``asof`` (the caller decides whether to filter).
    """
    from datetime import date as _date
    y1, m1, d1 = asof_yyyymmdd // 10000, (asof_yyyymmdd // 100) % 100, asof_yyyymmdd % 100
    y2, m2, d2 = exp_yyyymmdd // 10000, (exp_yyyymmdd // 100) % 100, exp_yyyymmdd % 100
    return (_date(y2, m2, d2) - _date(y1, m1, d1)).days


# ---------------------------------------------------------------------------
# Live root discovery — supersedes the hand-curated KNOWN_* tuples.
# The KNOWN_* constants stayed advisory (offline tab-completion, SCHEMA.md
# cross-references) but they drift the moment the DB ships a new
# instrument. These helpers query the DB once per process and cache.
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def live_index_roots() -> tuple[str, ...]:
    """Live INDEX `_id`s from the DB; cached process-wide.

    Returns a sorted tuple of every `_id` in the INDEX collection. Use
    this instead of ``KNOWN_INDEX_ROOTS`` when you need the authoritative
    set — additions ship without a code change. The first call hits Mongo
    via ``list_instruments``; subsequent calls reuse the cache.
    """
    return tuple(sorted(i["id"] for i in list_instruments(asset_class="INDEX")))


@functools.lru_cache(maxsize=1)
def live_etf_roots() -> tuple[str, ...]:
    """Live ETF `_id`s from the DB; cached process-wide.

    Same shape as :func:`live_index_roots`. Returns full `_id`s
    (e.g. ``"ETF_SPY"``), not bare tickers.
    """
    return tuple(sorted(i["id"] for i in list_instruments(asset_class="ETF")))


@functools.lru_cache(maxsize=1)
def live_option_roots() -> tuple[str, ...]:
    """Live OPT_<ROOT> root suffixes from the DB; cached process-wide.

    Returns the suffix only (e.g. ``"SP_500"``, ``"VIX"``), matching the
    string ``list_option_expiries`` and ``load_option_chain`` accept as
    their ``underlying`` / ``root`` argument. Use this instead of
    ``KNOWN_OPTION_ROOTS`` when you need the authoritative live set.
    """
    names = _live_collection_names()
    return tuple(sorted(n[len("OPT_"):] for n in names if n.startswith("OPT_")))


@functools.lru_cache(maxsize=1)
def live_futures_roots() -> tuple[str, ...]:
    """Live FUT_<ROOT> root suffixes from the DB; cached process-wide.

    Companion to :func:`live_option_roots`. Returns just the suffix
    (e.g. ``"SP_500"``) so the value is plug-compatible with the
    ``root`` argument of ``load_continuous_future`` /
    ``list_futures_contracts``.
    """
    names = _live_collection_names()
    return tuple(sorted(n[len("FUT_"):] for n in names if n.startswith("FUT_")))


# ---------------------------------------------------------------------------
# Cross-asset date alignment — kills the recurring "X has 8 extra holidays
# vs Y" boilerplate that every multi-instrument strategy reinvents.
# ---------------------------------------------------------------------------


def align_close_to_grid(
    grid_dates: Any,
    series: Any,
    *,
    method: Literal["forward_fill", "drop"] = "forward_fill",
) -> Any:
    """Project ``series.close`` onto ``grid_dates`` for cross-asset alignment.

    Returns a numpy float64 array of length ``len(grid_dates)`` carrying the
    close value from ``series`` at each grid date. Handles the common
    cross-asset case where two instruments live on different exchange
    calendars (e.g. CBOE VIX has ~8 extra trading days/year vs NYSE SPY).

    Args:
        grid_dates: Target date grid (numpy int64 YYYYMMDD or any iterable
            of ints). Typically the primary instrument's
            ``PriceSeries.dates``.
        series: Source :class:`PriceSeries` whose close values will be
            projected onto ``grid_dates``. Date arrays do NOT need to be
            sorted — the function uses dict lookup, not interpolation.
        method: Behaviour for grid dates absent from ``series.dates``:

            - ``"forward_fill"`` (default): use the most recent prior
              close from ``series``. Until ``series`` has its first row,
              the output is ``NaN``. This is the right default for
              regime / signal alignment where you want "latest known
              value" semantics.
            - ``"drop"``: emit ``NaN`` for missing dates. Caller decides
              whether to mask or interpolate.

    Returns:
        ``NDArray[np.float64]`` of length ``len(grid_dates)``.

    Example::

        from lib import data
        spy = data.fetch_etf_bars("ETF_SPY", start=20200101, end=20241231)
        vix = data.fetch_index_bars("IND_VIX", start=20200101, end=20241231)
        # SPY has 1258 bars, VIX has ~1266 (CBOE-only days). Project VIX
        # onto SPY's grid so signal[t] uses VIX value as of SPY day t.
        vix_on_spy = data.align_close_to_grid(spy.dates, vix)
        signal = (vix_on_spy < threshold).astype(np.float64)
    """
    grid = np.asarray(grid_dates, dtype=np.int64)
    src_dates = np.asarray(series.dates, dtype=np.int64)
    src_close = np.asarray(series.close, dtype=np.float64)
    by_date = {int(d): float(c) for d, c in zip(src_dates, src_close)}
    out = np.full(grid.shape, np.nan, dtype=np.float64)
    if method == "drop":
        for i, d in enumerate(grid):
            v = by_date.get(int(d))
            if v is not None:
                out[i] = v
        return out
    if method != "forward_fill":
        raise ValueError(
            f"align_close_to_grid: unsupported method={method!r}; "
            f"expected 'forward_fill' or 'drop'"
        )
    # Forward-fill: walk grid_dates in chronological order so every grid
    # point picks up the latest available close from the source. NaN
    # stays only for grid dates that precede the first source row.
    sorted_idx = np.argsort(grid)
    sorted_grid = grid[sorted_idx]
    src_sorted_idx = np.argsort(src_dates)
    src_sorted_dates = src_dates[src_sorted_idx]
    src_sorted_close = src_close[src_sorted_idx]
    last = np.nan
    j = 0
    n_src = len(src_sorted_dates)
    sorted_out = np.full(sorted_grid.shape, np.nan, dtype=np.float64)
    for i, d in enumerate(sorted_grid):
        # Advance src pointer through any source rows on/before d.
        while j < n_src and int(src_sorted_dates[j]) <= int(d):
            last = float(src_sorted_close[j])
            j += 1
        sorted_out[i] = last
    # Restore original grid order.
    out[sorted_idx] = sorted_out
    return out


# ---------------------------------------------------------------------------
# BacktestSpec field describer — keeps the doc surface in sync with the
# dataclass via introspection (no drift). Use from a REPL when designing
# a run-shape strategy.
# ---------------------------------------------------------------------------


def describe_backtest_spec() -> str:
    """Return a human-readable summary of ``BacktestSpec`` fields.

    Generated on demand from the dataclass via ``dataclasses.fields``, so
    the description never drifts from the actual schema. Useful for a
    cold-start agent writing a run-shape strategy who needs to know which
    arguments ``BacktestSpec(...)`` accepts. Run::

        from lib import data
        print(data.describe_backtest_spec())

    Returns the listing as a single string (one field per line, with
    type + default).
    """
    import dataclasses
    from .. import engine as _engine
    spec_cls = _engine.BacktestSpec
    lines = [f"BacktestSpec fields ({spec_cls.__module__}.BacktestSpec):"]
    for f in dataclasses.fields(spec_cls):
        ann = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", repr(f.type))
        if f.default is not dataclasses.MISSING:
            default = f"= {f.default!r}"
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            default = f"= <factory:{f.default_factory.__name__}>"  # type: ignore[union-attr]
        else:
            default = "(required)"
        lines.append(f"  {f.name}: {ann} {default}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# __all__ — mirrors the re-export surface above.
# ---------------------------------------------------------------------------

__all__ = [
    # Dataclasses
    "PriceSeries",
    "OptionDailyRow",
    "OptionContractSeries",
    "OptionChainSnapshot",
    "SeriesRecord",
    # Async loaders (bars)
    "load_index_bars",
    "load_etf_bars",
    "load_fund_bars",
    "load_forex_bars",
    "load_equity_bars",
    # Async loaders (futures)
    "list_futures_contracts",
    "load_futures_contract",
    "load_continuous_futures",
    # Async loaders (options)
    "load_option_chain",
    "load_option_contract_series",
    # Async helpers
    "list_collections_for_root",
    # Sync wrappers
    "load_index_bars_sync",
    "load_etf_bars_sync",
    "load_fund_bars_sync",
    "load_forex_bars_sync",
    "load_equity_bars_sync",
    "load_futures_contract_sync",
    "load_continuous_futures_sync",
    "load_option_chain_sync",
    "load_option_contract_series_sync",
    "list_futures_contracts_sync",
    "list_collections_for_root_sync",
    "load_continuous_future",
    # Generic dispatcher
    "load_bars",
    # Fetch wrappers
    "fetch_index_bars",
    "fetch_etf_bars",
    "fetch_continuous_future",
    # npz I/O
    "save_bars_npz",
    "save_npz",
    "load_npz",
    "save_signal_npz",
    "load_signal_npz",
    # data_summary
    "write_data_summary",
    # Documented constants
    "_OPTIONS_DOC_PROJECTION",
    "CYCLE_LETTERS",
    # Round-trip helpers for composite OPT _ids
    "serialize_doc_id",
    "deserialize_doc_id",
    # Advisory constants
    "KNOWN_ASSET_CLASSES",
    "BAR_LOADER_ASSET_CLASSES",
    "KNOWN_INDEX_ROOTS",
    "KNOWN_ETF_ROOTS",
    "KNOWN_OPTION_ROOTS",
    # New helpers
    "raw_db",
    "list_instruments",
    "list_option_expiries",
    "normalize_benchmark",
    # Live root discovery (supersedes hand-curated KNOWN_* tuples)
    "live_index_roots",
    "live_etf_roots",
    "live_option_roots",
    "live_futures_roots",
    # Cross-asset utilities
    "align_close_to_grid",
    # Introspection
    "describe_backtest_spec",
    # Re-exported from mongo
    "MongoWriteForbiddenError",
]
