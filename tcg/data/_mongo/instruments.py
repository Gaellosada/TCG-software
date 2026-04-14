"""MongoDB read adapters for instruments and price data.

Handles the idiosyncrasies of legacy ``_id`` types and ``eodDatas`` format.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import numpy as np
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING
from pymongo.errors import PyMongoError

from tcg.types.errors import DataAccessError
from tcg.types.market import ContractPriceData, InstrumentId, PriceResult, PriceSeries
from tcg.data._utils import date_to_int, filter_date_range

from tcg.data._mongo.helpers import (
    deserialize_doc_id,
    extract_price_data,
    parse_instrument_id,
    serialize_doc_id,
)

logger = logging.getLogger(__name__)


# Heavy fields to exclude from listing queries.
_LISTING_EXCLUSION: dict[str, int] = {
    "eodDatas": 0,
    "intradayDatas": 0,
    "eodGreeks": 0,
}


class MongoInstrumentReader:
    """Low-level read access to instrument documents in MongoDB.

    Used exclusively by ``DefaultMarketDataService``. Not part of the
    public API.
    """

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def list_instruments(
        self,
        collection: str,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[InstrumentId], int]:
        """List instruments in *collection* with pagination.

        Returns ``(instruments, total_count)``.
        """
        try:
            coll = self._db[collection]
            total = await coll.count_documents({})

            cursor = (
                coll.find({}, projection=_LISTING_EXCLUSION)
                .skip(skip)
                .limit(limit)
            )
            instruments: list[InstrumentId] = []
            async for doc in cursor:
                instruments.append(parse_instrument_id(doc, collection))

            return instruments, total
        except PyMongoError as exc:
            raise DataAccessError(
                f"MongoDB error listing instruments in '{collection}': {exc}"
            ) from exc

    async def read_prices(
        self,
        collection: str,
        instrument_id: str,
        *,
        provider: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> PriceResult | None:
        """Fetch OHLCV data for a single instrument.

        Tries multiple ``_id`` candidate types (ObjectId, then string)
        to handle the legacy platform's mixed ID storage.

        Date range filtering is applied *after* extraction since
        eodDatas is stored as an embedded array, not separate documents.

        Returns a ``PriceResult`` containing the prices, the resolved
        provider, and the list of all available providers.
        """
        try:
            coll = self._db[collection]

            doc = await self._find_document(coll, instrument_id)
            if doc is None:
                return None

            result = extract_price_data(
                doc, provider=provider, collection=collection,
            )
            if result is None:
                return None

            # Apply date range filter
            if start is not None or end is not None:
                filtered = filter_date_range(result.prices, start, end)
                if filtered is None:
                    return None
                result = PriceResult(
                    prices=filtered,
                    provider=result.provider,
                    available_providers=result.available_providers,
                )

            return result
        except PyMongoError as exc:
            raise DataAccessError(
                f"MongoDB error reading '{instrument_id}' from '{collection}': {exc}"
            ) from exc

    async def fetch_futures_contracts(
        self,
        collection: str,
        *,
        cycle: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[ContractPriceData], tuple[str, ...], str]:
        """Fetch all futures contracts in a collection, ordered by expiration.

        Queries MongoDB for documents with non-null expiration.
        If cycle is provided, filters by expirationCycle field.
        Returns contracts ordered by expiration date (ascending).

        The ``provider`` parameter is passed to each contract's
        extraction, selecting a consistent provider across all contracts.

        Returns
        -------
        (contracts, available_providers, resolved_provider)
            ``contracts`` is a list of ContractPriceData.
            ``available_providers`` is the intersection of providers
            available across all contracts.
            ``resolved_provider`` is the provider actually used for
            the first contract (representative of the auto-resolved choice).

        Performance: benefits from indexes on ``expiration`` (sort key)
        and ``expirationCycle`` (filter). Without these, MongoDB performs
        a collection scan.
        """
        try:
            coll = self._db[collection]

            query: dict[str, Any] = {"expiration": {"$ne": None}}
            if cycle is not None:
                query["expirationCycle"] = cycle

            projection = {"_id": 1, "eodDatas": 1, "expiration": 1}
            cursor = coll.find(query, projection).sort("expiration", ASCENDING)

            contracts: list[ContractPriceData] = []
            all_available: list[set[str]] = []
            resolved_provider: str | None = None

            async for doc in cursor:
                result = extract_price_data(
                    doc, provider=provider, collection=collection,
                )
                if result is None:
                    continue

                contract_id = serialize_doc_id(doc["_id"])
                expiration = _parse_expiration(doc["expiration"])
                if expiration is None:
                    logger.warning(
                        "Skipping contract with unparseable expiration: "
                        "collection=%s contract_id=%s expiration=%r",
                        collection,
                        contract_id,
                        doc["expiration"],
                    )
                    continue

                contracts.append(
                    ContractPriceData(
                        contract_id=contract_id,
                        expiration=expiration,
                        prices=result.prices,
                    )
                )
                all_available.append(set(result.available_providers))
                if resolved_provider is None:
                    resolved_provider = result.provider

            # Intersection of available providers across all contracts
            if all_available:
                common_providers = all_available[0]
                for s in all_available[1:]:
                    common_providers &= s
                available = tuple(sorted(common_providers))
            else:
                available = ()

            return contracts, available, resolved_provider or ""
        except PyMongoError as exc:
            raise DataAccessError(
                f"MongoDB error fetching futures contracts from '{collection}': {exc}"
            ) from exc

    async def fetch_available_cycles(
        self,
        collection: str,
    ) -> list[str]:
        """Return distinct expirationCycle values for a futures collection."""
        try:
            values = await self._db[collection].distinct("expirationCycle")
            return sorted(v for v in values if isinstance(v, str) and v)
        except PyMongoError as exc:
            raise DataAccessError(
                f"MongoDB error fetching cycles from '{collection}': {exc}"
            ) from exc

    async def _find_document(
        self,
        coll: Any,
        instrument_id: str,
    ) -> dict[str, Any] | None:
        """Try multiple _id candidates until a document is found."""
        candidates = deserialize_doc_id(instrument_id)
        for candidate in candidates:
            doc = await coll.find_one({"_id": candidate})
            if doc is not None:
                return doc
        return None



def _parse_expiration(value: Any) -> int | None:
    """Convert a MongoDB expiration field to a YYYYMMDD integer.

    Handles datetime objects, ISO strings, and raw integers.
    Returns None if the value cannot be parsed.
    """
    if isinstance(value, datetime):
        return date_to_int(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return date_to_int(value)
    if isinstance(value, int):
        # Assume already YYYYMMDD — basic sanity check
        if 19000101 <= value <= 21001231:
            return value
        return None
    if isinstance(value, str):
        # Try ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS...)
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date_to_int(dt)
        except (ValueError, TypeError):
            pass
    return None
