"""``MongoOptionsDataReader`` — concrete ``OptionsDataReader`` over Motor.

Mirrors the pattern of ``tcg.data._mongo.instruments.MongoInstrumentReader``:
single ``AsyncIOMotorDatabase`` injected, every public method wraps
``PyMongoError`` as ``OptionsDataAccessError``. Read-only by construction
(guardrail #1).

This module is the only place in Phase 1 that translates raw Mongo
documents into the option DTOs; ``service.py`` delegates to an instance
of this class. Module 2 (pricing) MUST NOT import from here directly —
the public Protocol lives in ``protocol.py``.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterable, Literal, Mapping

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING
from pymongo.errors import PyMongoError

from tcg.data._mongo.helpers import deserialize_doc_id
from tcg.data._mongo.registry import CollectionRegistry
from tcg.data.options._doc_to_dto import (
    _parse_yyyymmdd,
    bar_and_greek_to_row,
    doc_to_contract,
    index_greeks_by_date,
)
from tcg.data.options._provider import has_greeks_for_root, select_provider
from tcg.data.options._strike_factor import STRIKE_FACTOR_VERIFIED
from tcg.types.errors import OptionsContractNotFound, OptionsDataAccessError
from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)

logger = logging.getLogger(__name__)


# Friendly display names for OPT_* roots. Anything not listed falls back
# to a generated title from the collection name.
_ROOT_DISPLAY_NAMES: dict[str, str] = {
    "OPT_SP_500": "SP 500",
    "OPT_NASDAQ_100": "NASDAQ 100",
    "OPT_GOLD": "Gold",
    "OPT_BTC": "Bitcoin",
    "OPT_ETH": "Ethereum",
    "OPT_VIX": "VIX",
    "OPT_T_NOTE_10_Y": "T-Note 10Y",
    "OPT_T_BOND": "T-Bond",
    "OPT_EURUSD": "EUR/USD",
    "OPT_JPYUSD": "JPY/USD",
}


def _display_name(collection: str) -> str:
    if collection in _ROOT_DISPLAY_NAMES:
        return _ROOT_DISPLAY_NAMES[collection]
    return collection.removeprefix("OPT_").replace("_", " ").title()


class MongoOptionsDataReader:
    """Read-only adapter for OPT_* collections.

    Satisfies :class:`tcg.data.options.protocol.OptionsDataReader`.
    """

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        registry: CollectionRegistry,
    ) -> None:
        self._db = db
        self._registry = registry

    # ------------------------------------------------------------------
    # get_contract
    # ------------------------------------------------------------------

    async def get_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        try:
            coll = self._db[collection]
            doc = await self._find_document(coll, contract_id)
        except PyMongoError as exc:
            raise OptionsDataAccessError(
                f"MongoDB error reading contract '{contract_id}' "
                f"from '{collection}': {exc}"
            ) from exc

        if doc is None:
            raise OptionsContractNotFound(
                f"Contract '{contract_id}' not found in '{collection}'"
            )

        eod_datas: Mapping[str, Any] | None = doc.get("eodDatas")
        provider = select_provider(collection, eod_datas)
        if provider is None:
            # No usable provider data — return an empty series with the
            # default deterministic provider (kept consistent for display).
            provider = _fallback_provider(collection)

        contract = doc_to_contract(doc, collection, provider)
        if contract is None:
            raise OptionsContractNotFound(
                f"Contract '{contract_id}' in '{collection}' is missing "
                f"required fields (expiration / strike / type)"
            )

        rows = _build_rows(doc, provider, allow_greeks=has_greeks_for_root(collection))
        return OptionContractSeries(contract=contract, rows=tuple(rows))

    # ------------------------------------------------------------------
    # query_chain
    # ------------------------------------------------------------------

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
        try:
            coll = self._db[root]
            query: dict[str, Any] = {
                "expiration": {
                    "$gte": _date_to_int(expiration_min),
                    "$lte": _date_to_int(expiration_max),
                },
            }
            cursor = coll.find(query).sort("expiration", ASCENDING)

            type_filter = type.upper() if isinstance(type, str) else "BOTH"
            target_yyyymmdd = _date_to_int(date)

            results: list[tuple[OptionContractDoc, OptionDailyRow]] = []
            async for doc in cursor:
                pair = _materialize_chain_row(
                    doc=doc,
                    collection=root,
                    target_yyyymmdd=target_yyyymmdd,
                    type_filter=type_filter,
                    strike_min=strike_min,
                    strike_max=strike_max,
                )
                if pair is not None:
                    results.append(pair)
            return results
        except PyMongoError as exc:
            raise OptionsDataAccessError(
                f"MongoDB error querying chain on '{root}' for "
                f"{date.isoformat()}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # list_roots
    # ------------------------------------------------------------------

    async def list_roots(self) -> list[OptionRootInfo]:
        out: list[OptionRootInfo] = []
        for collection in self._registry.all_options:
            try:
                info = await self._summarize_root(collection)
            except PyMongoError as exc:
                raise OptionsDataAccessError(
                    f"MongoDB error summarizing options root "
                    f"'{collection}': {exc}"
                ) from exc
            out.append(info)
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _summarize_root(self, collection: str) -> OptionRootInfo:
        coll = self._db[collection]
        doc_count = await coll.estimated_document_count()

        first_doc = await coll.find(
            {"expiration": {"$ne": None}},
            projection={"expiration": 1},
        ).sort("expiration", ASCENDING).limit(1).to_list(length=1)
        last_doc = await coll.find(
            {"expiration": {"$ne": None}},
            projection={"expiration": 1},
        ).sort("expiration", -1).limit(1).to_list(length=1)

        expiration_first = (
            _int_to_date(first_doc[0]["expiration"]) if first_doc else None
        )
        expiration_last = (
            _int_to_date(last_doc[0]["expiration"]) if last_doc else None
        )

        providers = tuple(await _peek_providers(coll))
        last_trade_date = await _peek_last_trade_date(coll)

        return OptionRootInfo(
            collection=collection,
            name=_display_name(collection),
            has_greeks=has_greeks_for_root(collection),
            providers=providers,
            expiration_first=expiration_first,
            expiration_last=expiration_last,
            doc_count_estimated=int(doc_count),
            strike_factor_verified=STRIKE_FACTOR_VERIFIED.get(collection, False),
            last_trade_date=last_trade_date,
        )

    async def _find_document(
        self,
        coll: Any,
        contract_id: str,
    ) -> dict[str, Any] | None:
        for candidate in deserialize_doc_id(contract_id):
            if isinstance(candidate, dict):
                # MongoDB compound `_id` equality is byte-wise on BSON, so
                # `{a:x, b:y}` does not equal `{b:y, a:x}` when looked up
                # via `{"_id": <dict>}`. `serialize_doc_id` sorts keys
                # alphabetically and `deserialize_doc_id` reconstructs the
                # dict in that same order, but the stored _id is in
                # whatever field order the document was written with —
                # often not alphabetical. Querying by sub-fields is
                # order-independent and uses the `_id.<field>` index path
                # when present.
                query = {f"_id.{k}": v for k, v in candidate.items()}
                doc = await coll.find_one(query)
            else:
                doc = await coll.find_one({"_id": candidate})
            if doc is not None:
                return doc
        return None


# ---------------------------------------------------------------------------
# Free helpers — kept module-private but importable for tests
# ---------------------------------------------------------------------------


def _fallback_provider(collection: str) -> str:
    """Provider key used on docs that have no ``eodDatas`` at all.

    The chosen value is informational only (no rows will be produced);
    we still want a deterministic, audit-friendly string.
    """
    if collection == "OPT_BTC":
        return "INTERNAL"
    if collection == "OPT_VIX":
        return "CBOE"
    if collection == "OPT_ETH":
        return "DERIBIT"
    return "IVOLATILITY"


def _materialize_chain_row(
    *,
    doc: Mapping[str, Any],
    collection: str,
    target_yyyymmdd: int,
    type_filter: str,
    strike_min: float | None,
    strike_max: float | None,
) -> tuple[OptionContractDoc, OptionDailyRow] | None:
    """Apply client-side filters and merge bar+greek for a single doc.

    Returns ``None`` when the doc is filtered out, has no usable data, or
    misses the target date.
    """
    eod_datas: Mapping[str, Any] | None = doc.get("eodDatas")
    provider = select_provider(collection, eod_datas)
    if provider is None or not eod_datas:
        return None

    contract = doc_to_contract(doc, collection, provider)
    if contract is None:
        return None

    if type_filter in ("C", "P") and contract.type != type_filter:
        return None
    if strike_min is not None and contract.strike < strike_min:
        return None
    if strike_max is not None and contract.strike > strike_max:
        return None

    bars = eod_datas.get(provider) if isinstance(eod_datas, Mapping) else None
    if not bars:
        return None
    bar = _find_bar_for_date(bars, target_yyyymmdd)
    if bar is None:
        return None

    greeks_list = None
    if has_greeks_for_root(collection):
        eod_greeks = doc.get("eodGreeks")
        if isinstance(eod_greeks, Mapping):
            greeks_list = eod_greeks.get(provider)

    greeks_index = index_greeks_by_date(greeks_list)
    target_date = _int_to_date(target_yyyymmdd)
    greek_entry = greeks_index.get(target_date) if target_date else None

    row = bar_and_greek_to_row(bar, greek_entry)
    if row is None:
        return None
    return contract, row


def _build_rows(
    doc: Mapping[str, Any],
    provider: str,
    *,
    allow_greeks: bool,
) -> list[OptionDailyRow]:
    """Materialize the full chronological row list for a single contract."""
    eod_datas = doc.get("eodDatas")
    if not isinstance(eod_datas, Mapping):
        return []
    bars = eod_datas.get(provider)
    if not bars:
        return []

    greeks_list: list[Mapping[str, Any]] | None = None
    if allow_greeks:
        eod_greeks = doc.get("eodGreeks")
        if isinstance(eod_greeks, Mapping):
            raw = eod_greeks.get(provider)
            if isinstance(raw, list):
                greeks_list = raw
    greeks_index = index_greeks_by_date(greeks_list)

    rows: list[OptionDailyRow] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        # Look up the greek entry by parsed bar date.
        bar_date = _parse_yyyymmdd(bar.get("date"))
        greek_entry = greeks_index.get(bar_date) if bar_date else None
        row = bar_and_greek_to_row(bar, greek_entry)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: r.date)
    return rows


def _find_bar_for_date(
    bars: Iterable[Mapping[str, Any]],
    target_yyyymmdd: int,
) -> Mapping[str, Any] | None:
    for bar in bars:
        raw = bar.get("date") if isinstance(bar, Mapping) else None
        try:
            iv = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            continue
        if iv == target_yyyymmdd:
            return bar
    return None


def _date_to_int(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def _int_to_date(value: Any) -> date | None:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    if not (19000101 <= iv <= 21001231):
        return None
    try:
        return date(iv // 10000, (iv // 100) % 100, iv % 100)
    except ValueError:
        return None


async def _peek_providers(coll: Any) -> tuple[str, ...]:
    """Best-effort: read one doc's ``eodDatas`` keys to report providers.

    This is purely informational on ``list_roots()``. We do not exhaustively
    enumerate providers across the collection; one doc is enough for the
    11-collection legacy schema where each root has a single provider
    (DB §6).
    """
    try:
        doc = await coll.find_one(
            {"eodDatas": {"$exists": True}},
            projection={"eodDatas": 1},
        )
    except PyMongoError:
        return ()
    if not doc:
        return ()
    eod = doc.get("eodDatas")
    if not isinstance(eod, Mapping):
        return ()
    return tuple(sorted(eod.keys()))


async def _peek_last_trade_date(coll: Any) -> date | None:
    """Return the latest bar date for a live contract in this collection.

    Single direct path:
      1. Pick the contract with the smallest ``expiration >= today``.
      2. Scan its ``eodDatas`` bars (across whatever providers are on
         the doc — collections like OPT_BTC are heterogeneous).
      3. Return the max ``date`` field.

    Returns None only when there is no live contract or no bars; the
    caller surfaces that as "no data available" rather than guessing.
    """
    today_yyyymmdd = (
        date.today().year * 10000 + date.today().month * 100 + date.today().day
    )
    try:
        doc = await coll.find_one(
            {"expiration": {"$gte": today_yyyymmdd}, "eodDatas": {"$exists": True}},
            projection={"eodDatas": 1},
            sort=[("expiration", ASCENDING)],
        )
    except PyMongoError:
        return None
    if not doc:
        return None
    eod = doc.get("eodDatas")
    if not isinstance(eod, Mapping):
        return None

    best: date | None = None
    for bars in eod.values():
        if not isinstance(bars, list):
            continue
        for bar in bars:
            if not isinstance(bar, Mapping):
                continue
            d = _int_to_date(bar.get("date"))
            if d is not None and (best is None or d > best):
                best = d
    return best
