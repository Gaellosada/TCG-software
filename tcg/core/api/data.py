"""Data router -- three endpoints wrapping MarketDataService."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request

from tcg.data.protocols import MarketDataService
from tcg.types.errors import DataNotFoundError
from tcg.types.market import AssetClass

router = APIRouter(prefix="/api/data", tags=["data"])


def get_market_data(request: Request) -> MarketDataService:
    """Dependency: retrieve the MarketDataService from app state."""
    return request.app.state.market_data


@router.get("/collections")
async def list_collections(
    asset_class: str | None = Query(None, description="Filter by asset class (equity, index, future)"),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """List available data collections, optionally filtered by asset class."""
    ac = AssetClass(asset_class) if asset_class is not None else None
    collections = await svc.list_collections(ac)
    return {"collections": collections}


@router.get("/{collection}")
async def list_instruments(
    collection: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """List instruments in a collection with pagination."""
    result = await svc.list_instruments(collection, skip=skip, limit=limit)
    return {
        "items": [
            {
                "symbol": inst.symbol,
                "asset_class": inst.asset_class.value,
                "collection": inst.collection,
                "exchange": inst.exchange,
            }
            for inst in result.items
        ],
        "total": result.total,
        "skip": result.skip,
        "limit": result.limit,
    }


@router.get("/{collection}/{instrument_id}")
async def get_prices(
    collection: str,
    instrument_id: str,
    start: str | None = Query(None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(None, description="End date YYYY-MM-DD"),
    provider: str | None = Query(None, description="Data provider filter"),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Fetch OHLCV price data for an instrument."""
    start_date = date.fromisoformat(start) if start else None
    end_date = date.fromisoformat(end) if end else None

    series = await svc.get_prices(
        collection,
        instrument_id,
        start=start_date,
        end=end_date,
        provider=provider,
    )
    if series is None:
        raise DataNotFoundError(
            f"Instrument '{instrument_id}' not found in collection '{collection}'"
        )

    return {
        "dates": series.dates.tolist(),
        "open": series.open.tolist(),
        "high": series.high.tolist(),
        "low": series.low.tolist(),
        "close": series.close.tolist(),
        "volume": series.volume.tolist(),
    }
