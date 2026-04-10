"""MongoDB read adapters for instruments and price data.

Handles the idiosyncrasies of legacy ``_id`` types and ``eodDatas`` format.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
from motor.motor_asyncio import AsyncIOMotorDatabase

from tcg.types.market import InstrumentId, PriceSeries

from tcg.data._mongo.helpers import (
    deserialize_doc_id,
    extract_price_data,
    parse_instrument_id,
)


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

    async def read_prices(
        self,
        collection: str,
        instrument_id: str,
        *,
        provider: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> PriceSeries | None:
        """Fetch OHLCV data for a single instrument.

        Tries multiple ``_id`` candidate types (ObjectId, then string)
        to handle the legacy platform's mixed ID storage.

        Date range filtering is applied *after* extraction since
        eodDatas is stored as an embedded array, not separate documents.
        """
        coll = self._db[collection]

        doc = await self._find_document(coll, instrument_id)
        if doc is None:
            return None

        series = extract_price_data(doc, provider=provider)
        if series is None:
            return None

        # Apply date range filter
        if start is not None or end is not None:
            series = _filter_date_range(series, start, end)
            if series is None or len(series) == 0:
                return None

        return series

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


def _filter_date_range(
    series: PriceSeries,
    start: date | None,
    end: date | None,
) -> PriceSeries | None:
    """Slice a ``PriceSeries`` to the given date range.

    Dates in the series are YYYYMMDD integers.
    """
    mask = np.ones(len(series), dtype=bool)

    if start is not None:
        start_int = start.year * 10000 + start.month * 100 + start.day
        mask &= series.dates >= start_int

    if end is not None:
        end_int = end.year * 10000 + end.month * 100 + end.day
        mask &= series.dates <= end_int

    if not mask.any():
        return None

    return PriceSeries(
        dates=series.dates[mask],
        open=series.open[mask],
        high=series.high[mask],
        low=series.low[mask],
        close=series.close[mask],
        volume=series.volume[mask],
    )
