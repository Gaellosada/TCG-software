"""Typed async accessors over the production MongoDB schema (uppercase prefixes).

Schema encoded here only - no imports from `tcg.*`. Date convention: YYYYMMDD int64.

Real-shape contract (verified live 2026-05-02 against `mongodb://localhost:27017/tcg-instrument`):
- INDEX `_id="IND_SP_500"`, ETF `_id="ETF_SPY"`  -- providers=[YAHOO]
- FUND `_id="FUND_..."`                          -- providers=[BLOOMBERG]
- FOREX `_id="BTC_USD"`                          -- providers=[BITSTAMP, COINGECKO]
- FUT_*/OPT_* docs                               -- providers=[IVOLATILITY] (OPT_VIX uses CBOE)
- OPT docs use composite `_id={"internalSymbol": ..., "expirationCycle": ...}`,
  field `type` ("CALL"/"PUT"/"C"/"P"/"c"), `eodGreeks: {<provider>: [{date, impliedVolatility, ...}]}`.
- OPT eod rows are mostly `close=0` (untraded); `bid`/`ask` carry the real premium.

All loaders fail loud on missing `_id`/provider rather than returning empty.
"""
from __future__ import annotations

import ast
import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import numpy as np
from numpy.typing import NDArray

from .mongo import sync_run

logger = logging.getLogger(__name__)

CYCLE_LETTERS = "FGHJKMNQUVXZ"
_CYCLE_MONTH = {c: i + 1 for i, c in enumerate(CYCLE_LETTERS)}

# Per-collection canonical providers, ordered by preference (real-Mongo verified).
# When user does not pass `provider`, the loader picks the first present in `eodDatas`.
# When user passes an explicit provider absent from the doc, we raise with the
# available list. This eliminates the silent-empty-on-wrong-provider bug.
_PROVIDER_PRIORITY: dict[str, tuple[str, ...]] = {
    "INDEX":  ("YAHOO",),
    "ETF":    ("YAHOO",),
    "FUND":   ("BLOOMBERG",),
    "FOREX":  ("BITSTAMP", "COINGECKO"),
    "EQUITY": ("YAHOO",),
}
# Prefix-based lookup for FUT_* / OPT_* collections (composite names).
_PROVIDER_PRIORITY_PREFIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("OPT_VIX", ("CBOE", "IVOLATILITY")),
    ("OPT_",    ("IVOLATILITY",)),
    ("FUT_",    ("IVOLATILITY",)),
)


def _provider_priority_for(collection: str) -> tuple[str, ...]:
    """Return the ordered provider priority list for `collection`, or empty tuple."""
    if collection in _PROVIDER_PRIORITY:
        return _PROVIDER_PRIORITY[collection]
    for prefix, prio in _PROVIDER_PRIORITY_PREFIX:
        if collection.startswith(prefix):
            return prio
    return ()


# Sentinel for "use the first canonical provider for this collection".
_AUTO_PROVIDER = "__AUTO__"


# ----------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class PriceSeries:
    """Daily OHLCV strip for one instrument; arrays parallel and date-sorted ASC."""

    instrument_id: str
    provider: str
    dates: NDArray[np.int64]
    open: NDArray[np.float64]
    high: NDArray[np.float64]
    low: NDArray[np.float64]
    close: NDArray[np.float64]
    volume: NDArray[np.float64]
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.dates.shape[0])

    def slice(self, start: int | None, end: int | None) -> "PriceSeries":
        """Return inclusive [start,end] slice by YYYYMMDD bounds."""
        mask = np.ones(self.dates.shape, dtype=bool)
        if start is not None:
            mask &= self.dates >= int(start)
        if end is not None:
            mask &= self.dates <= int(end)
        return PriceSeries(
            instrument_id=self.instrument_id,
            provider=self.provider,
            dates=self.dates[mask],
            open=self.open[mask],
            high=self.high[mask],
            low=self.low[mask],
            close=self.close[mask],
            volume=self.volume[mask],
            meta=dict(self.meta),
        )


@dataclass(frozen=True)
class OptionDailyRow:
    """One contract-day row: OHLCV + bid/ask + IV + greeks (each may be None).

    `bid`/`ask` reflect the real Mongo OPT_* schema (eodDatas.<provider> rows carry
    bid/ask alongside close). On untraded days `close==0` while bid/ask are valid;
    use `.mark` for the canonical fill price (close-if-traded else mid else 0).
    """

    date: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    iv: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    rho: float | None
    bid: float | None = None
    ask: float | None = None

    @property
    def mid(self) -> float | None:
        """Bid-ask midpoint when both >0, else None."""
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return 0.5 * (float(self.bid) + float(self.ask))
        return None

    @property
    def mark(self) -> float:
        """Canonical mark price: close when traded (>0), else mid, else 0.0."""
        if self.close is not None and self.close > 0:
            return float(self.close)
        m = self.mid
        if m is not None:
            return float(m)
        return 0.0

    @property
    def mark_source(self) -> str:
        """Provenance tag for `.mark`: 'close' | 'mid' | 'none'."""
        if self.close is not None and self.close > 0:
            return "close"
        if self.mid is not None:
            return "mid"
        return "none"


@dataclass(frozen=True)
class OptionContractSeries:
    """Per-contract daily series; rows ordered by date ASC."""

    root: str
    contract_id: str
    strike: float
    expiration: int
    option_type: Literal["C", "P"]
    rows: tuple[OptionDailyRow, ...] = ()

    @property
    def dates(self) -> NDArray[np.int64]:
        """Per-row YYYYMMDD dates as a NumPy int64 array."""
        return np.array([r.date for r in self.rows], dtype=np.int64)

    @property
    def close(self) -> NDArray[np.float64]:
        """Per-row close prices as a NumPy float64 array."""
        return np.array([r.close for r in self.rows], dtype=np.float64)

    @property
    def iv(self) -> NDArray[np.float64]:
        """Per-row implied volatility (NaN where missing)."""
        return np.array([np.nan if r.iv is None else r.iv for r in self.rows], dtype=np.float64)

    @property
    def greeks_per_day(self) -> dict[str, NDArray[np.float64]]:
        """Per-day greeks as a dict of float64 arrays (delta/gamma/theta/vega)."""
        def _arr(name: str) -> NDArray[np.float64]:
            return np.array(
                [np.nan if getattr(r, name) is None else getattr(r, name) for r in self.rows],
                dtype=np.float64,
            )

        return {"delta": _arr("delta"), "gamma": _arr("gamma"), "theta": _arr("theta"), "vega": _arr("vega")}


@dataclass(frozen=True)
class OptionChainSnapshot:
    """One-day chain view: contracts each carrying a single OptionDailyRow at as_of."""

    root: str
    asof_date: int
    spot: float | None
    contracts: tuple[OptionContractSeries, ...]

    @property
    def as_of(self) -> int:
        """Alias for asof_date (matches spec naming)."""
        return self.asof_date


# ----------------------------------------------------------------------------- helpers


def _parse_expiration(value: Any) -> int:
    """Normalize expiration values (int / ISO string / datetime) to YYYYMMDD int."""
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, datetime):
        return value.year * 10000 + value.month * 100 + value.day
    if isinstance(value, str):
        s = value.strip()
        if "-" in s:
            try:
                d = datetime.fromisoformat(s.split("T")[0])
                return d.year * 10000 + d.month * 100 + d.day
            except ValueError as e:
                raise ValueError(f"unparseable expiration: {value!r}") from e
        try:
            return int(s)
        except ValueError as e:
            raise ValueError(f"unparseable expiration: {value!r}") from e
    raise ValueError(f"unsupported expiration type: {type(value).__name__}")


def _doc_to_price_series(
    doc: dict | None,
    provider: str,
    instrument_id: str,
    start: int | None,
    end: int | None,
    *,
    extra_meta: dict[str, Any] | None = None,
) -> PriceSeries:
    """Pull eodDatas[provider] from the doc, drop close-NaN, fill OHLV NaN with 0.0."""
    meta: dict[str, Any] = {"instrument_id": instrument_id, "provider": provider}
    if extra_meta:
        meta.update(extra_meta)
    if not doc:
        return PriceSeries(
            instrument_id=instrument_id,
            provider=provider,
            dates=np.zeros(0, dtype=np.int64),
            open=np.zeros(0, dtype=np.float64),
            high=np.zeros(0, dtype=np.float64),
            low=np.zeros(0, dtype=np.float64),
            close=np.zeros(0, dtype=np.float64),
            volume=np.zeros(0, dtype=np.float64),
            meta=meta,
        )
    eod = (doc.get("eodDatas") or {}).get(provider) or []
    if not eod:
        return PriceSeries(
            instrument_id=instrument_id,
            provider=provider,
            dates=np.zeros(0, dtype=np.int64),
            open=np.zeros(0, dtype=np.float64),
            high=np.zeros(0, dtype=np.float64),
            low=np.zeros(0, dtype=np.float64),
            close=np.zeros(0, dtype=np.float64),
            volume=np.zeros(0, dtype=np.float64),
            meta=meta,
        )
    rows = sorted(eod, key=lambda r: int(r.get("date", 0)))
    dates = np.array([int(r.get("date", 0)) for r in rows], dtype=np.int64)
    close = np.array([float(r.get("close", np.nan)) for r in rows], dtype=np.float64)
    keep = ~np.isnan(close)
    rows = [rows[i] for i in range(len(rows)) if keep[i]]
    dates = dates[keep]
    close = close[keep]

    def _col(name: str) -> NDArray[np.float64]:
        a = np.array([float(r.get(name, 0.0)) if r.get(name) is not None else 0.0 for r in rows], dtype=np.float64)
        a = np.where(np.isnan(a), 0.0, a)
        return a

    series = PriceSeries(
        instrument_id=instrument_id,
        provider=provider,
        dates=dates,
        open=_col("open"),
        high=_col("high"),
        low=_col("low"),
        close=close,
        volume=_col("volume"),
        meta=meta,
    )
    if start is not None or end is not None:
        return series.slice(start, end)
    return series


def _serialize_id(value: Any) -> str:
    """Stringify a Mongo `_id` into a stable key. Composite dict -> 'k1=v1|k2=v2' (sorted).

    Matches data-model.md `serialize_doc_id` so emitted ids round-trip with
    `_deserialize_id` and interop with the rest of the TCG ecosystem.
    """
    if isinstance(value, dict):
        return "|".join(f"{k}={value[k]}" for k in sorted(value.keys()))
    return str(value)


def _deserialize_id(s: str) -> Any:
    """Inverse of `_serialize_id`: rebuild a candidate `_id` (str | dict | ObjectId).

    Tries `key=val|key=val` parse first, then ObjectId-shaped hex, else raw string.
    Used for indexed `_id` lookups against composite-keyed OPT collections.
    """
    if not isinstance(s, str):
        return s
    # Composite-dict form: key1=val1|key2=val2
    if "=" in s and "|" in s and " " not in s.split("|", 1)[0].split("=", 1)[0]:
        try:
            parts = [p.split("=", 1) for p in s.split("|")]
            if all(len(p) == 2 and p[0] for p in parts):
                return {k: v for k, v in parts}
        except ValueError as e:
            # Composite-dict parse failed (malformed split); fall through to other shapes.
            logger.debug("_deserialize_id composite-dict parse failed for %r: %s", s, e)
    # Legacy stringified-tuple form (lib's pre-fix output, e.g. "[('a','b'), ...]"). Decode safely.
    if s.startswith("[") and s.endswith("]"):
        try:
            decoded = ast.literal_eval(s)
            if isinstance(decoded, list) and all(
                isinstance(t, tuple) and len(t) == 2 for t in decoded
            ):
                return dict(decoded)
        except (ValueError, SyntaxError) as e:
            logger.debug("_deserialize_id literal_eval failed for %r: %s", s, e)
    # ObjectId shape: 24 hex chars
    if len(s) == 24 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            from bson import ObjectId  # type: ignore

            if ObjectId.is_valid(s):
                return ObjectId(s)
        except (ImportError, ValueError) as e:
            # bson is optional (not installed) or ObjectId construction rejected the shape.
            logger.debug("_deserialize_id ObjectId construction failed for %r: %s", s, e)
    return s


async def _sample_ids(db: Any, collection: str, n: int = 10) -> list[Any]:
    """Return up to `n` sample `_id`s from `collection` for diagnostic error messages."""
    out: list[Any] = []
    # Narrow to pymongo cursor / driver errors (the documented failure mode);
    # let unexpected exceptions propagate. `pymongo.errors.PyMongoError` is the
    # base class for all driver exceptions.
    try:
        from pymongo.errors import PyMongoError
    except ImportError:
        PyMongoError = Exception  # type: ignore[assignment,misc]
    try:
        async for d in db[collection].find({}, {"_id": 1}):
            out.append(d.get("_id"))
            if len(out) >= n:
                break
    except PyMongoError as e:
        # Cursor / find failed — return whatever we have so the caller can still
        # build a partial diagnostic. Loud-debug the failure.
        logger.debug("_sample_ids find failed on collection=%r: %s", collection, e)
        return out
    return out


def _available_providers(doc: dict) -> list[str]:
    """List provider keys present in `doc.eodDatas`, empty list if absent or malformed."""
    eod = doc.get("eodDatas")
    if isinstance(eod, dict):
        return list(eod.keys())
    return []


def _pick_provider(doc: dict, requested: str | None, *, collection: str | None = None) -> str:
    """Resolve which provider key to use from `doc.eodDatas`.

    - explicit `requested` not in doc -> LookupError listing what is available
    - requested is None / _AUTO_PROVIDER -> first canonical priority match for the collection
    - if still ambiguous -> first key (deterministic since dicts are insertion-ordered in 3.7+)
      but emits a debug log so the test can capture it
    Raises LookupError when `eodDatas` is absent or empty.
    """
    available = _available_providers(doc)
    if not available:
        raise LookupError(
            f"doc has no eodDatas providers (id={doc.get('_id')!r}, collection={collection!r})"
        )
    if requested is not None and requested != _AUTO_PROVIDER:
        if requested in available:
            return requested
        raise LookupError(
            f"provider {requested!r} not present for id={doc.get('_id')!r} "
            f"in collection={collection!r}; available providers: {available[:10]}"
        )
    # Auto-select by canonical priority for this collection.
    if collection is not None:
        for cand in _provider_priority_for(collection):
            if cand in available:
                return cand
    # No priority match.
    # If the doc has a single provider, it's unambiguous — use it (deterministic).
    # If multiple providers and none match the canonical priority for this
    # collection, it's a real configuration ambiguity: surface it loudly so the
    # caller picks explicitly. This is the P1-F fix per the simplicity review.
    if len(available) == 1:
        return available[0]
    raise LookupError(
        f"provider is ambiguous for id={doc.get('_id')!r} coll={collection!r}: "
        f"available={available} but none match canonical priority for this collection. "
        f"Pass `provider=...` explicitly."
    )


def _make_sync(async_fn: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
    """Return a sync wrapper that calls sync_run on the coroutine."""

    @functools.wraps(async_fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Sync proxy that runs the wrapped coroutine on the current loop."""
        return sync_run(async_fn(*args, **kwargs))

    return wrapper


# ----------------------------------------------------------------------------- bars


async def _resolve_doc(
    db: Any,
    collection: str,
    instrument_id: str,
) -> dict:
    """Find a document by `_id` with conventional-variant fallback. Raises on miss.

    Tries (in order): the raw string, ObjectId variant, and well-known prefix
    variants for INDEX (e.g. SPX -> IND_SPX -> IND_SP_500). Raises LookupError
    listing up to 10 sample ids from the collection so the caller knows what to
    type instead.
    """
    candidates: list[Any] = [instrument_id]
    # ObjectId fallback for hex-shaped ids.
    if isinstance(instrument_id, str) and len(instrument_id) == 24 and all(
        c in "0123456789abcdefABCDEF" for c in instrument_id
    ):
        try:
            from bson import ObjectId  # type: ignore

            candidates.append(ObjectId(instrument_id))
        except Exception:
            pass
    # Composite-dict deserialization (key=val|key=val).
    deser = _deserialize_id(instrument_id)
    if deser is not None and deser != instrument_id:
        candidates.append(deser)
    # Convention-based variants by collection.
    upper = str(instrument_id).upper()
    if collection == "INDEX":
        if not upper.startswith("IND_"):
            candidates.append(f"IND_{upper}")
        # Common synonyms (SPX -> IND_SP_500). One round-trip cheap.
        synonyms = {
            "SPX": "IND_SP_500", "SP500": "IND_SP_500", "SP_500": "IND_SP_500",
            "VIX": "IND_VIX", "NDX": "IND_NDX_100", "RUT": "IND_RUT_2000",
        }
        if upper in synonyms:
            candidates.append(synonyms[upper])
    elif collection == "ETF" and not upper.startswith("ETF_"):
        candidates.append(f"ETF_{upper}")
    elif collection == "FUND" and not upper.startswith("FUND_"):
        candidates.append(f"FUND_{upper}")

    seen: set[str] = set()
    for cand in candidates:
        key = repr(cand)
        if key in seen:
            continue
        seen.add(key)
        doc = await db[collection].find_one({"_id": cand})
        if doc is not None:
            return doc
    sample = await _sample_ids(db, collection, n=10)
    raise LookupError(
        f"no document with _id={instrument_id!r} in collection={collection!r}; "
        f"tried variants {[repr(c) for c in candidates]}; "
        f"sample of available _id values (up to 10): {sample}"
    )


async def _load_simple(
    db: Any,
    collection: str,
    instrument_id: str,
    provider: str | None,
    start: int | None,
    end: int | None,
) -> PriceSeries:
    """Resolve a doc, validate provider, return the bar series. Raises on missing pieces."""
    if collection not in await _coll_names(db):
        raise ValueError(f"collection {collection!r} not present in DB")
    # Backward-compat: if the collection is empty (test_empty_collection_returns_empty_series
    # passes `collections={"INDEX": []}`), preserve the historical behavior of returning an
    # empty PriceSeries rather than raising. Real-Mongo INDEX is never empty.
    try:
        empty = await db[collection].count_documents({}) == 0
    except Exception:
        empty = False
    if empty:
        return _doc_to_price_series(
            None, provider or "YAHOO", instrument_id, start, end,
            extra_meta={"collection": collection},
        )
    doc = await _resolve_doc(db, collection, instrument_id)
    actual_provider = _pick_provider(doc, provider, collection=collection)
    return _doc_to_price_series(
        doc, actual_provider, instrument_id, start, end,
        extra_meta={"collection": collection},
    )


async def _coll_names(db: Any) -> list[str]:
    """Return the list of collection names in `db` (no caching; callers cache when needed)."""
    return list(await db.list_collection_names())


async def load_index_bars(
    db: Any, instrument_id: str, *, provider: str | None = None, start: int | None = None, end: int | None = None
) -> PriceSeries:
    """Async load of an INDEX bar series. Provider defaults to YAHOO; raises on missing id/provider."""
    return await _load_simple(db, "INDEX", instrument_id, provider, start, end)


async def load_etf_bars(
    db: Any, instrument_id: str, *, provider: str | None = None, start: int | None = None, end: int | None = None
) -> PriceSeries:
    """Async load of an ETF bar series. Provider defaults to YAHOO; raises on missing id/provider."""
    return await _load_simple(db, "ETF", instrument_id, provider, start, end)


async def load_fund_bars(
    db: Any, instrument_id: str, *, provider: str | None = None, start: int | None = None, end: int | None = None
) -> PriceSeries:
    """Async load of a FUND bar series. Provider defaults to BLOOMBERG (real-Mongo)."""
    return await _load_simple(db, "FUND", instrument_id, provider, start, end)


async def load_forex_bars(
    db: Any, instrument_id: str, *, provider: str | None = None, start: int | None = None, end: int | None = None
) -> PriceSeries:
    """Async load of a FOREX bar series. Provider defaults to BITSTAMP/COINGECKO (real-Mongo)."""
    return await _load_simple(db, "FOREX", instrument_id, provider, start, end)


async def load_equity_bars(
    db: Any, instrument_id: str, *, provider: str | None = None, start: int | None = None, end: int | None = None
) -> PriceSeries:
    """Async load of a single-stock EQUITY bar series.

    Future-compatible loader: when the production MongoDB does not yet ship an
    ``EQUITY`` collection, ``_load_simple`` raises a clear
    ``ValueError("collection 'EQUITY' not present in DB")``. The dispatcher
    surfaces that to the caller rather than silently routing to the wrong
    collection (e.g. ETF), which would yield an empty bar series.
    """
    return await _load_simple(db, "EQUITY", instrument_id, provider, start, end)


# ----------------------------------------------------------------------------- futures


async def list_futures_contracts(db: Any, root: str) -> list[dict]:
    """List metadata rows for every contract in FUT_<ROOT>."""
    coll = f"FUT_{root.upper()}"
    if coll not in await _coll_names(db):
        raise ValueError(f"unknown futures root: {root!r}")
    out: list[dict] = []
    async for doc in db[coll].find({}, {"eodDatas": 0, "intradayDatas": 0}):
        out.append(
            {
                "contract_id": _serialize_id(doc.get("_id")),
                "expiration": _parse_expiration(doc.get("expiration", 0)),
                "cycle": doc.get("expirationCycle"),
                "raw": {k: v for k, v in doc.items() if k != "_id"},
            }
        )
    out.sort(key=lambda r: r["expiration"])
    return out


async def load_futures_contract(
    db: Any,
    root: str,
    expiration: int,
    *,
    provider: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> PriceSeries:
    """Async load of one futures contract by expiration (YYYYMMDD)."""
    coll = f"FUT_{root.upper()}"
    if coll not in await _coll_names(db):
        raise ValueError(f"unknown futures root: {root!r}")
    target = int(expiration)
    if target <= 0:
        raise ValueError(f"futures expiration must be a positive YYYYMMDD int, got {expiration!r}")
    doc: dict | None = None
    async for cand in db[coll].find({}):
        try:
            if _parse_expiration(cand.get("expiration", 0)) == target:
                doc = cand
                break
        except ValueError:
            continue
    if doc is None:
        raise LookupError(
            f"no contract for root={root!r} expiration={expiration} in {coll!r}"
        )
    actual_provider = _pick_provider(doc, provider, collection=coll)
    return _doc_to_price_series(
        doc,
        actual_provider,
        _serialize_id(doc.get("_id")),
        start,
        end,
        extra_meta={"collection": coll, "root": root.upper(), "expiration": target},
    )


async def load_continuous_futures(
    db: Any,
    root: str,
    *,
    cycle: str = "HMUZ",
    roll_offset_days: int = 0,
    adjustment: Literal["none", "ratio", "difference"] = "none",
    provider: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> PriceSeries:
    """Stitch front-month contracts into a continuous series; numpy-only roll logic."""
    if not cycle or any(c not in CYCLE_LETTERS for c in cycle):
        raise ValueError(f"bad cycle string: {cycle!r}")
    if adjustment not in {"none", "ratio", "difference"}:
        raise ValueError(f"bad adjustment: {adjustment!r}")

    listings = await list_futures_contracts(db, root)
    months = {_CYCLE_MONTH[c] for c in cycle}
    eligible: list[dict] = []
    for r in listings:
        exp = int(r["expiration"])
        if exp <= 0:
            continue
        if (exp // 100) % 100 in months:
            eligible.append(r)
    eligible.sort(key=lambda r: r["expiration"])
    if not eligible:
        return PriceSeries(
            instrument_id=f"{root.upper()}!continuous",
            provider=str(provider) if provider is not None else "AUTO",
            dates=np.zeros(0, dtype=np.int64),
            open=np.zeros(0, dtype=np.float64),
            high=np.zeros(0, dtype=np.float64),
            low=np.zeros(0, dtype=np.float64),
            close=np.zeros(0, dtype=np.float64),
            volume=np.zeros(0, dtype=np.float64),
            meta={"root": root.upper(), "cycle": cycle, "adjustment": adjustment, "rolls": []},
        )

    # Load each contract series.
    series_by_id: dict[str, PriceSeries] = {}
    for r in eligible:
        cid = r["contract_id"]
        ps = await load_futures_contract(db, root, int(r["expiration"]), provider=provider)
        series_by_id[cid] = ps

    # Pick active contract per date (front month satisfying expiration - offset >= d).
    all_dates: list[int] = []
    for ps in series_by_id.values():
        all_dates.extend(int(d) for d in ps.dates)
    if not all_dates:
        return PriceSeries(
            instrument_id=f"{root.upper()}!continuous",
            provider=str(provider) if provider is not None else "AUTO",
            dates=np.zeros(0, dtype=np.int64),
            open=np.zeros(0, dtype=np.float64),
            high=np.zeros(0, dtype=np.float64),
            low=np.zeros(0, dtype=np.float64),
            close=np.zeros(0, dtype=np.float64),
            volume=np.zeros(0, dtype=np.float64),
            meta={"root": root.upper(), "cycle": cycle, "adjustment": adjustment, "rolls": []},
        )
    dates_sorted = np.array(sorted(set(all_dates)), dtype=np.int64)
    if start is not None:
        dates_sorted = dates_sorted[dates_sorted >= int(start)]
    if end is not None:
        dates_sorted = dates_sorted[dates_sorted <= int(end)]

    def _shift_offset(exp: int, offset_days: int) -> int:
        # Calendar-day shift via datetime; fine for daily-res rolling.
        if offset_days == 0:
            return exp
        from datetime import date, timedelta

        y, m, d = exp // 10000, (exp // 100) % 100, exp % 100
        dt = date(y, m, d) - timedelta(days=offset_days)
        return dt.year * 10000 + dt.month * 100 + dt.day

    # Pre-index per-contract dates for fast lookup.
    idx_by_id: dict[str, dict[int, int]] = {
        cid: {int(d): i for i, d in enumerate(ps.dates)} for cid, ps in series_by_id.items()
    }

    rolls: list[tuple[int, str, str]] = []
    out_dates: list[int] = []
    out_open: list[float] = []
    out_high: list[float] = []
    out_low: list[float] = []
    out_close: list[float] = []
    out_volume: list[float] = []

    # Adjustment factor maintained going *backwards*, but we build forward and apply
    # to the prior history at each roll boundary.
    cum_factor = 1.0
    cum_offset = 0.0

    prev_active: str | None = None
    for d in dates_sorted:
        d_int = int(d)
        # Pick first eligible contract whose effective-roll-date >= d_int and
        # whose series contains this date.
        active: str | None = None
        for r in eligible:
            cid = r["contract_id"]
            eff = _shift_offset(int(r["expiration"]), int(roll_offset_days))
            if eff < d_int:
                continue
            if d_int in idx_by_id[cid]:
                active = cid
                break
        if active is None:
            continue
        ps = series_by_id[active]
        i = idx_by_id[active][d_int]

        if prev_active is not None and active != prev_active:
            # Roll boundary: align prior history to new contract.
            old_close = float(series_by_id[prev_active].close[idx_by_id[prev_active][d_int]]) if d_int in idx_by_id[prev_active] else float(out_close[-1])
            new_close = float(ps.close[i])
            if adjustment == "ratio" and old_close != 0.0:
                factor = new_close / old_close
                out_open = [v * factor for v in out_open]
                out_high = [v * factor for v in out_high]
                out_low = [v * factor for v in out_low]
                out_close = [v * factor for v in out_close]
                cum_factor *= factor
            elif adjustment == "difference":
                delta = new_close - old_close
                out_open = [v + delta for v in out_open]
                out_high = [v + delta for v in out_high]
                out_low = [v + delta for v in out_low]
                out_close = [v + delta for v in out_close]
                cum_offset += delta
            rolls.append((d_int, prev_active, active))

        out_dates.append(d_int)
        out_open.append(float(ps.open[i]))
        out_high.append(float(ps.high[i]))
        out_low.append(float(ps.low[i]))
        out_close.append(float(ps.close[i]))
        out_volume.append(float(ps.volume[i]))
        prev_active = active

    # Provider on output reflects what was actually used by the per-contract loaders.
    actual_prov = next(
        (ps.provider for ps in series_by_id.values() if ps.provider), str(provider) if provider else "AUTO"
    )
    return PriceSeries(
        instrument_id=f"{root.upper()}!continuous",
        provider=actual_prov,
        dates=np.array(out_dates, dtype=np.int64),
        open=np.array(out_open, dtype=np.float64),
        high=np.array(out_high, dtype=np.float64),
        low=np.array(out_low, dtype=np.float64),
        close=np.array(out_close, dtype=np.float64),
        volume=np.array(out_volume, dtype=np.float64),
        meta={
            "root": root.upper(),
            "cycle": cycle,
            "adjustment": adjustment,
            "roll_offset_days": int(roll_offset_days),
            "rolls": rolls,
            "final_factor": cum_factor,
            "final_offset": cum_offset,
        },
    )


# ----------------------------------------------------------------------------- options

# A4: inclusion projection drops intradayDatas + unused fields.  Defined at
# module level so both load_option_chain and load_chain (in lib/options.py)
# share a single source of truth.
_OPTIONS_DOC_PROJECTION: dict[str, int] = {
    "_id": 1,
    "contractId": 1,
    "expiration": 1,
    "strike": 1,
    "type": 1,
    "optionType": 1,
    "rootUnderlying": 1,
    "underlying": 1,
    "underlyingSymbol": 1,
    "contractSize": 1,
    "currency": 1,
    "eodDatas": 1,
    "eodGreeks": 1,
    "eodDatasStart": 1,
    "eodDatasEnd": 1,
}


def _option_type_from_doc(doc: dict) -> Literal["C", "P"]:
    """Resolve the option type from a Mongo doc tolerantly.

    Real production Mongo (IVOLATILITY) uses field `type` ("CALL"/"PUT" or "C"/"P").
    Legacy/test fixtures used `optionType`. Either is accepted; missing → ValueError
    rather than a silent default (which historically misclassified every put as a call).
    """
    raw = doc.get("type")
    if raw is None:
        raw = doc.get("optionType")
    if raw is None:
        raise ValueError(
            f"option doc missing both 'type' and 'optionType' fields: id={doc.get('_id')!r}"
        )
    s = str(raw).upper().strip()
    if s.startswith("P"):
        return "P"
    if s.startswith("C"):
        return "C"
    raise ValueError(f"unparseable option type {raw!r} in doc id={doc.get('_id')!r}")


def _greeks_for_date(
    doc: dict, date: int, *, provider: str | None = None, collection: str | None = None
) -> dict | None:
    """Resolve greeks for an as-of date with deterministic provider priority.

    Tolerated shapes:
    - production `eodGreeks: {<provider>: [{date, impliedVolatility, delta, ...}, ...]}`
    - legacy `eodGreeks: {<date_int_or_str>: {iv, delta, ...}}`
    - production-edge `eodGreeks: []` (empty list) -> returns None with a debug log

    Provider selection: explicit `provider` if present in doc; else first match from
    the canonical priority list for `collection`; else first key (logged).
    Returns a normalized dict (iv/delta/gamma/vega/theta/rho) or None.
    """
    g = doc.get("eodGreeks")
    if isinstance(g, list):
        # Real-shape edge: some OPT_VIX docs ship `eodGreeks: []`.
        logger.debug("eodGreeks is empty list for id=%r; treating as no greeks", doc.get("_id"))
        return None
    if not isinstance(g, dict) or not g:
        return None
    # Detect production shape: any value is a list of date-keyed rows.
    if any(isinstance(v, list) for v in g.values()):
        # Deterministic provider pick.
        list_keys = [k for k, v in g.items() if isinstance(v, list)]
        chosen_key: str | None = None
        if provider is not None and provider in list_keys:
            chosen_key = provider
        elif collection is not None:
            for cand in _provider_priority_for(collection):
                if cand in list_keys:
                    chosen_key = cand
                    break
        if chosen_key is None and list_keys:
            chosen_key = list_keys[0]
            if len(list_keys) > 1:
                logger.debug(
                    "greeks provider auto-picked by insertion order for id=%r coll=%r: %s "
                    "(available=%s)", doc.get("_id"), collection, chosen_key, list_keys,
                )
        rows = g.get(chosen_key) if chosen_key else None
        if not rows:
            return None
        match = next((r for r in rows if int(r.get("date", 0)) == int(date)), None)
        if match is None:
            return None
        return {
            "iv": match.get("impliedVolatility", match.get("iv")),
            "delta": match.get("delta"),
            "gamma": match.get("gamma"),
            "vega": match.get("vega"),
            "theta": match.get("theta"),
            "rho": match.get("rho"),
            "spot": match.get("spot"),
        }
    # Legacy shape: flat dict keyed by date int or str.
    return g.get(str(int(date))) or g.get(int(date))


def _row_from_doc(date: int, raw: dict, greeks: dict | None) -> OptionDailyRow:
    """Build an OptionDailyRow from a raw eodDatas row + matched greeks dict.

    Captures real-Mongo bid/ask alongside close so `OptionDailyRow.mark` can
    fall back to the bid-ask mid on untraded days (close=0).
    """
    def _f(v: Any) -> float | None:
        if v is None:
            return None
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return None if np.isnan(x) else x

    return OptionDailyRow(
        date=int(date),
        open=float(raw.get("open", 0.0) or 0.0),
        high=float(raw.get("high", 0.0) or 0.0),
        low=float(raw.get("low", 0.0) or 0.0),
        close=float(raw.get("close", 0.0) or 0.0),
        volume=float(raw.get("volume", 0.0) or 0.0),
        iv=_f((greeks or {}).get("iv")),
        delta=_f((greeks or {}).get("delta")),
        gamma=_f((greeks or {}).get("gamma")),
        vega=_f((greeks or {}).get("vega")),
        theta=_f((greeks or {}).get("theta")),
        rho=_f((greeks or {}).get("rho")),
        bid=_f(raw.get("bid")),
        ask=_f(raw.get("ask")),
    )


async def load_option_contract_series(
    db: Any,
    root: str,
    contract_id: str,
    *,
    provider: str | None = None,
    start: int | None = None,
    end: int | None = None,
    drop_untraded: bool = False,
) -> OptionContractSeries:
    """Async load of one option contract's daily series + greeks.

    Uses indexed `_id` lookup via `_deserialize_id(contract_id)` (composite-dict
    or raw string) instead of a full collection scan. Raises LookupError when
    no doc matches. Provider is auto-resolved to IVOLATILITY (or CBOE for
    OPT_VIX) when not specified. When `drop_untraded=True`, rows where both
    close==0 and (no usable bid/ask) are dropped; default keeps them and
    surfaces them via `.mark`.
    """
    coll = f"OPT_{root.upper()}"
    if coll not in await _coll_names(db):
        raise ValueError(f"unknown option root: {root!r}")
    # Try indexed lookup with both raw-string and deserialized-composite candidates.
    candidates: list[Any] = []
    deser = _deserialize_id(contract_id)
    if isinstance(deser, dict):
        candidates.append(deser)
    candidates.append(contract_id)
    doc: dict | None = None
    for cand in candidates:
        d = await db[coll].find_one({"_id": cand})
        if d is not None:
            doc = d
            break
    # Legacy fallback: also try `contractId` field (test fixtures use this).
    if doc is None:
        doc = await db[coll].find_one({"contractId": contract_id})
    if doc is None:
        sample = await _sample_ids(db, coll, n=10)
        raise LookupError(
            f"no option contract with id={contract_id!r} in {coll!r}; "
            f"sample ids (up to 10): {sample}"
        )
    eod = (doc.get("eodDatas") or {})
    if isinstance(eod, dict):
        if not eod:
            raise LookupError(
                f"option doc id={doc.get('_id')!r} has empty eodDatas"
            )
        actual_provider = _pick_provider(doc, provider, collection=coll)
        eod_rows = eod.get(actual_provider) or []
    else:
        actual_provider = "UNKNOWN"
        eod_rows = list(eod)
    eod_rows = sorted(eod_rows, key=lambda r: int(r.get("date", 0)))
    rows: list[OptionDailyRow] = []
    for r in eod_rows:
        d = int(r.get("date", 0))
        if start is not None and d < int(start):
            continue
        if end is not None and d > int(end):
            continue
        g = _greeks_for_date(doc, d, provider=actual_provider, collection=coll)
        row = _row_from_doc(d, r, g)
        if drop_untraded and row.mark <= 0:
            continue
        rows.append(row)

    return OptionContractSeries(
        root=root.upper(),
        contract_id=contract_id,
        strike=float(doc.get("strike", 0.0) or 0.0),
        expiration=_parse_expiration(doc.get("expiration", 0)),
        option_type=_option_type_from_doc(doc),
        rows=tuple(rows),
    )


async def load_option_chain(
    db: Any,
    root: str,
    *,
    asof_date: int,
    expiration: int | None = None,
    strike_filter: tuple[float, float] | None = None,
    option_type: Literal["C", "P"] | None = None,
    provider: str | None = None,
    underlying_id: str | None = None,
    underlying_collection: str | None = None,
    underlying_provider: str | None = None,
    drop_untraded: bool = False,
    progress: bool = True,
) -> OptionChainSnapshot:
    """Async load of the option chain at one as-of date.

    Uses server-side filtering on `eodDatasStart.<provider>` /
    `eodDatasEnd.<provider>` to prune the scan; provider is auto-resolved
    (IVOLATILITY for OPT_*, CBOE for OPT_VIX). Spot is loaded from
    `underlying_id`+`underlying_collection` (defaults: IND_<ROOT> in INDEX) when
    available, since real `eodGreeks` rows do not carry `spot`. When
    `drop_untraded=True` only contracts whose row has `.mark > 0` are kept.
    """
    coll = f"OPT_{root.upper()}"
    if coll not in await _coll_names(db):
        raise ValueError(f"unknown option root: {root!r}")
    asof = int(asof_date)

    # Resolve provider once (peek at one doc to discover available providers).
    sample_doc = await db[coll].find_one({}, projection=_OPTIONS_DOC_PROJECTION)
    if sample_doc is None:
        return OptionChainSnapshot(root=root.upper(), asof_date=asof, spot=None, contracts=())
    try:
        actual_provider = _pick_provider(sample_doc, provider, collection=coll)
    except LookupError:
        # eodDatas absent on the sample. Fall through with no provider filter; emit empty.
        return OptionChainSnapshot(root=root.upper(), asof_date=asof, spot=None, contracts=())

    # Build server-side query: window overlap on the chosen provider, plus other filters.
    query: dict[str, Any] = {
        f"eodDatasStart.{actual_provider}": {"$lte": asof},
        f"eodDatasEnd.{actual_provider}": {"$gte": asof},
    }
    if expiration is not None:
        query["expiration"] = {"$eq": int(expiration)}
    else:
        query["expiration"] = {"$gte": asof}
    # A1: server-side type regex (case-insensitive; covers OPT_VIX lowercase).
    if option_type is not None:
        c = option_type
        query["type"] = {"$regex": f"^[{c.upper()}{c.lower()}]"}
    # A5: server-side strike range filter when caller narrows.
    if strike_filter is not None:
        query["strike"] = {"$gte": float(strike_filter[0]), "$lte": float(strike_filter[1])}
    # Note: when collection lacks eodDatasStart/End indexes (e.g. legacy synthetic
    # fixtures), the query may yield 0 docs even though docs exist. We retry
    # with no provider-window filter as a fallback.
    found = await db[coll].count_documents(query)
    if found == 0:
        # Fallback path for fixtures without window-bound fields.
        query = {}

    contracts: list[OptionContractSeries] = []
    # Progress emitter: keep `load_option_chain` consistent with `load_chain`.
    # The single-asof path is usually fast (~1-3s), but we still want a
    # heartbeat for very wide chains; cap at 10 lines (vs 20 for load_chain).
    from .options import _LoadProgress  # local import: avoid circular at module load
    _prog = _LoadProgress(label=f"load_option_chain {root.upper()}", enabled=bool(progress), max_lines=10)
    async for doc in db[coll].find(query, projection=_OPTIONS_DOC_PROJECTION):
        _prog.tick()
        try:
            doc_exp = _parse_expiration(doc.get("expiration", 0))
        except ValueError:
            continue
        if expiration is not None and doc_exp != int(expiration):
            continue
        if doc_exp < asof and expiration is None:
            # Already-expired contract, ignore.
            continue
        try:
            otype = _option_type_from_doc(doc)
        except ValueError:
            continue
        if option_type is not None and otype != option_type:
            continue
        strike = float(doc.get("strike", 0.0) or 0.0)
        if strike_filter is not None and not (strike_filter[0] <= strike <= strike_filter[1]):
            continue
        eod = doc.get("eodDatas") or {}
        if isinstance(eod, dict):
            try:
                doc_provider = _pick_provider(doc, provider or actual_provider, collection=coll)
            except LookupError:
                continue
            eod_rows = eod.get(doc_provider) or []
        else:
            doc_provider = actual_provider
            eod_rows = list(eod)
        match = next((r for r in eod_rows if int(r.get("date", 0)) == asof), None)
        if match is None:
            continue
        greeks = _greeks_for_date(doc, asof, provider=doc_provider, collection=coll)
        row = _row_from_doc(asof, match, greeks)
        if drop_untraded and row.mark <= 0:
            continue
        contracts.append(
            OptionContractSeries(
                root=root.upper(),
                contract_id=str(doc.get("contractId") or _serialize_id(doc.get("_id"))),
                strike=strike,
                expiration=doc_exp,
                option_type=otype,
                rows=(row,),
            )
        )

    # Spot: prefer explicit underlying_id; fall back to a `spot` field in greeks (legacy).
    spot: float | None = None
    if underlying_id is not None:
        und_coll = underlying_collection or "INDEX"
        try:
            und_doc = await _resolve_doc(db, und_coll, underlying_id)
            und_provider = _pick_provider(und_doc, underlying_provider, collection=und_coll)
            ps = _doc_to_price_series(und_doc, und_provider, underlying_id, asof, asof)
            if len(ps):
                spot = float(ps.close[-1])
        except (LookupError, ValueError) as e:
            logger.debug("underlying spot lookup failed for %r in %r: %s", underlying_id, und_coll, e)
    if spot is None and underlying_id is None:
        # Legacy fallback: some test fixtures stash `spot` inside greeks.
        # Skip this scan entirely whenever the caller supplied `underlying_id`
        # (the canonical real-Mongo path) since the explicit underlying lookup
        # above already failed and a second collection scan won't recover it.
        async for doc in db[coll].find({}):
            try:
                _parse_expiration(doc.get("expiration", 0))
            except ValueError:
                continue
            try:
                doc_prov = _pick_provider(doc, provider or actual_provider, collection=coll)
            except LookupError:
                continue
            greeks = _greeks_for_date(doc, asof, provider=doc_prov, collection=coll)
            if isinstance(greeks, dict) and greeks.get("spot") is not None:
                try:
                    spot = float(greeks["spot"])
                    break
                except (TypeError, ValueError):
                    pass
    _prog.done()
    return OptionChainSnapshot(
        root=root.upper(),
        asof_date=asof,
        spot=spot,
        contracts=tuple(contracts),
    )


async def list_collections_for_root(db: Any, root: str) -> list[str]:
    """Return all collection names that mention this root (FUT_X, OPT_X, ...)."""
    names = await _coll_names(db)
    needle = root.upper()
    return [n for n in names if needle in n.upper()]


# ----------------------------------------------------------------------------- sync wrappers

load_index_bars_sync = _make_sync(load_index_bars)
load_etf_bars_sync = _make_sync(load_etf_bars)
load_fund_bars_sync = _make_sync(load_fund_bars)
load_forex_bars_sync = _make_sync(load_forex_bars)
load_equity_bars_sync = _make_sync(load_equity_bars)
load_futures_contract_sync = _make_sync(load_futures_contract)
load_continuous_futures_sync = _make_sync(load_continuous_futures)
load_option_chain_sync = _make_sync(load_option_chain)
load_option_contract_series_sync = _make_sync(load_option_contract_series)
list_futures_contracts_sync = _make_sync(list_futures_contracts)
list_collections_for_root_sync = _make_sync(list_collections_for_root)


# ----------------------------------------------------------------------------- generic dispatcher

_BARS_DISPATCH: dict[str, Callable[..., Any]] = {
    "INDEX": load_index_bars_sync,
    "ETF": load_etf_bars_sync,
    "FUND": load_fund_bars_sync,
    "FOREX": load_forex_bars_sync,
    # EQUITY routes to load_equity_bars; raises a clear error until the
    # production MongoDB ships an EQUITY collection. Single-stock requests
    # resolve to EQUITY rather than silently mis-classifying as ETF.
    "EQUITY": load_equity_bars_sync,
}


def load_bars(
    db: Any,
    *,
    asset_class: str,
    instrument_id: str,
    provider: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> PriceSeries:
    """Generic synchronous bar loader; routes by asset_class to the per-class loader.

    `provider=None` (the default) lets each per-class loader auto-resolve via
    `_PROVIDER_PRIORITY_PREFIX` — i.e. INDEX/ETF -> YAHOO, FUND -> BLOOMBERG,
    FOREX -> BITSTAMP, FUTURE -> IVOLATILITY. Pass an explicit string only when
    the agent has reason to override the canonical provider for that class.
    """
    key = str(asset_class).upper()
    fn = _BARS_DISPATCH.get(key)
    if fn is None:
        raise ValueError(
            f"unsupported asset_class={asset_class!r}; supported: {sorted(_BARS_DISPATCH)}"
        )
    return fn(db, instrument_id, provider=provider, start=start, end=end)


def load_continuous_future(
    db: Any,
    *,
    root: str,
    cycle: str = "HMUZ",
    roll_offset_days: int = 0,
    adjustment: Literal["none", "ratio", "difference"] = "none",
    provider: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> PriceSeries:
    """Sync alias for `load_continuous_futures` (singular form used in snippets).

    `provider=None` lets `_pick_provider` auto-resolve via the priority list
    (IVOLATILITY for FUT_*); the previous default of YAHOO raised LookupError
    against real Mongo.
    """
    return load_continuous_futures_sync(
        db,
        root,
        cycle=cycle,
        roll_offset_days=roll_offset_days,
        adjustment=adjustment,
        provider=provider,
        start=start,
        end=end,
    )


# ----------------------------------------------------------------------------- fetch wrappers
# Convenience wrappers that open the read-only sync mongo handle internally so
# snippets do not need to manage `mongo.sync_db()` directly. They inherit the
# read-only Mongo proxy (via `mongo.sync_db()` -> `_ReadOnlyDatabase`).


def fetch_index_bars(
    instrument_id: str,
    *,
    start: int,
    end: int,
    provider: str | None = None,
) -> PriceSeries:
    """Open default sync Mongo handle and load INDEX bars for `instrument_id`.

    `provider=None` lets the loader auto-resolve via the canonical priority
    (YAHOO for INDEX). Pass `provider="..."` to override.
    """
    from . import mongo as _mongo
    db = _mongo.sync_db()
    return load_bars(
        db,
        asset_class="INDEX",
        instrument_id=instrument_id,
        provider=provider,
        start=start,
        end=end,
    )


def fetch_etf_bars(
    instrument_id: str,
    *,
    start: int,
    end: int,
    provider: str | None = None,
) -> PriceSeries:
    """Open default sync Mongo handle and load ETF bars for `instrument_id`.

    `provider=None` auto-resolves to YAHOO for ETF.
    """
    from . import mongo as _mongo
    db = _mongo.sync_db()
    return load_bars(
        db,
        asset_class="ETF",
        instrument_id=instrument_id,
        provider=provider,
        start=start,
        end=end,
    )


def fetch_continuous_future(
    root: str,
    *,
    start: int,
    end: int,
    cycle: str = "HMUZ",
    roll_offset_days: int = 0,
    adjustment: Literal["none", "ratio", "difference"] = "none",
    provider: str | None = None,
) -> PriceSeries:
    """Open default sync Mongo handle and build a continuous front-month future series.

    Wrapper over `load_continuous_future` that manages the Mongo handle. The
    returned series is rolled / adjusted per the given parameters.
    """
    from . import mongo as _mongo
    db = _mongo.sync_db()
    return load_continuous_future(
        db,
        root=root,
        cycle=cycle,
        roll_offset_days=roll_offset_days,
        adjustment=adjustment,
        provider=provider,
        start=start,
        end=end,
    )


# ----------------------------------------------------------------------------- npz I/O


def save_bars_npz(bars: PriceSeries, path: str | Path) -> None:
    """Persist a PriceSeries to disk via np.savez (string fields stored as 0-d arrays)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        str(p),
        dates=np.asarray(bars.dates, dtype=np.int64),
        open=np.asarray(bars.open, dtype=np.float64),
        high=np.asarray(bars.high, dtype=np.float64),
        low=np.asarray(bars.low, dtype=np.float64),
        close=np.asarray(bars.close, dtype=np.float64),
        volume=np.asarray(bars.volume, dtype=np.float64),
        instrument_id=np.array(str(bars.instrument_id)),
        provider=np.array(str(bars.provider)),
    )


# Alias for the snippet-side name used by fetch_*: data_load.save_npz(bars, path).
save_npz = save_bars_npz


def load_npz(path: str | Path) -> PriceSeries:
    """Reverse of save_bars_npz; reconstruct a PriceSeries from an .npz file."""
    p = Path(path)
    with np.load(str(p), allow_pickle=False) as z:
        dates = np.asarray(z["dates"], dtype=np.int64)
        open_ = np.asarray(z["open"], dtype=np.float64)
        high = np.asarray(z["high"], dtype=np.float64)
        low = np.asarray(z["low"], dtype=np.float64)
        close = np.asarray(z["close"], dtype=np.float64)
        volume = np.asarray(z["volume"], dtype=np.float64)
        iid = str(z["instrument_id"]) if "instrument_id" in z.files else p.stem
        prov = str(z["provider"]) if "provider" in z.files else "UNKNOWN"
    return PriceSeries(
        instrument_id=iid,
        provider=prov,
        dates=dates,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        meta={"source_path": str(p)},
    )


def save_signal_npz(
    dates: NDArray[np.int64], signal: NDArray[np.float64], path: str | Path
) -> None:
    """Persist a (dates, signal) pair as an .npz file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        str(p),
        dates=np.asarray(dates, dtype=np.int64),
        signal=np.asarray(signal, dtype=np.float64),
    )


def load_signal_npz(path: str | Path) -> NDArray[np.float64]:
    """Return just the signal array from a {dates, signal} .npz file."""
    with np.load(str(Path(path)), allow_pickle=False) as z:
        return np.asarray(z["signal"], dtype=np.float64)


# ---------------------------------------------------------------------------
# data_summary.json schema writer (P2-F).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesRecord:
    """One series record matching `pipeline/02-data.md:41-58`'s data_summary schema."""

    id: str
    kind: str
    provider: str
    start: str  # ISO YYYY-MM-DD
    end: str    # ISO YYYY-MM-DD
    n_bars: int
    n_gaps: int
    n_nan_close: int
    cache_path: str


def write_data_summary(workspace_dir: str | Path, series_records: list[SeriesRecord]) -> Path:
    """Write `data/data_summary.json` matching the documented schema.

    Returns the written path. Creates `workspace_dir/data/` when missing.
    """
    import json
    from datetime import datetime as _dt, timezone as _tz

    ws = Path(workspace_dir)
    out_dir = ws / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "data_summary.json"

    payload = {
        "series": [
            {
                "id": r.id,
                "kind": r.kind,
                "provider": r.provider,
                "start": r.start,
                "end": r.end,
                "n_bars": int(r.n_bars),
                "n_gaps": int(r.n_gaps),
                "n_nan_close": int(r.n_nan_close),
                "cache_path": r.cache_path,
            }
            for r in series_records
        ],
        "loaded_at": _dt.now(_tz.utc).isoformat().replace("+00:00", "Z"),
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path
