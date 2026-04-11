"""Data router -- endpoints wrapping MarketDataService."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request

from tcg.data.protocols import MarketDataService
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import (
    AssetClass,
    AdjustmentMethod,
    ContinuousRollConfig,
    RollStrategy,
)

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
    try:
        ac = AssetClass(asset_class) if asset_class is not None else None
    except ValueError:
        raise ValidationError(
            f"Invalid asset_class '{asset_class}'. Must be one of: {', '.join(e.value for e in AssetClass)}"
        )
    collections = await svc.list_collections(ac)
    return {"collections": collections}


# --- Continuous futures series (must precede /{collection} catch-all) ---


@router.get("/continuous/{collection}")
async def get_continuous_series(
    collection: str,
    strategy: str = Query("front_month", description="Roll strategy"),
    adjustment: str = Query("none", description="Adjustment method: none, proportional, difference"),
    cycle: str | None = Query(None, description="Expiration cycle filter (e.g. HMUZ)"),
    start: str | None = Query(None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(None, description="End date YYYY-MM-DD"),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Build a continuous futures series from rolled contracts."""
    try:
        roll_strategy = RollStrategy(strategy)
    except ValueError:
        raise ValidationError(
            f"Invalid strategy '{strategy}'. Must be one of: {', '.join(e.value for e in RollStrategy)}"
        )

    try:
        adj_method = AdjustmentMethod(adjustment)
    except ValueError:
        raise ValidationError(
            f"Invalid adjustment '{adjustment}'. Must be one of: {', '.join(e.value for e in AdjustmentMethod)}"
        )

    try:
        start_date = date.fromisoformat(start) if start else None
        end_date = date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise ValidationError(f"Invalid date format: {exc}") from exc

    roll_config = ContinuousRollConfig(
        strategy=roll_strategy,
        adjustment=adj_method,
        cycle=cycle,
    )

    series = await svc.get_continuous(collection, roll_config, start=start_date, end=end_date)
    if series is None:
        raise DataNotFoundError(
            f"No continuous series found for collection '{collection}'"
        )

    return {
        "collection": series.collection,
        "strategy": roll_config.strategy.value,
        "adjustment": roll_config.adjustment.value,
        "cycle": roll_config.cycle,
        "roll_dates": list(series.roll_dates),
        "contracts": list(series.contracts),
        "dates": series.prices.dates.tolist(),
        "open": series.prices.open.tolist(),
        "high": series.prices.high.tolist(),
        "low": series.prices.low.tolist(),
        "close": series.prices.close.tolist(),
        "volume": series.prices.volume.tolist(),
    }


@router.get("/continuous/{collection}/cycles")
async def get_available_cycles(
    collection: str,
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Return available expiration cycles for a futures collection."""
    cycles = await svc.get_available_cycles(collection)
    return {"cycles": cycles}


# --- Generic collection/instrument endpoints ---


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
    try:
        start_date = date.fromisoformat(start) if start else None
        end_date = date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise ValidationError(f"Invalid date format: {exc}") from exc

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
