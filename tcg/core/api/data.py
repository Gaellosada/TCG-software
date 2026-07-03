"""Data router -- endpoints wrapping MarketDataService."""

from __future__ import annotations

from typing import Annotated, Union

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from tcg.core.api._basket_compute import compute_basket_series
from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models import BasketRefInline, BasketRefSaved
from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.api.common import get_market_data
from tcg.data.protocols import MarketDataService
from tcg.engine.signal_exec import SignalDataError, SignalValidationError
from tcg.persistence import WriteRepository
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import (
    AssetClass,
    AdjustmentMethod,
    ContinuousRollConfig,
    RollStrategy,
)

router = APIRouter(prefix="/api/data", tags=["data"])


# --- Basket series (Data-page exploration) ---


class BasketSeriesRequest(BaseModel):
    """Request body for ``POST /api/data/basket/series``.

    The basket itself is a discriminated union over the SAME wire models
    the signals path uses — ``{kind:"saved", basket_id}`` or
    ``{kind:"inline", asset_class, legs}`` — so the Data-page composer
    and the signals composer emit an identical basket shape.  ``start`` /
    ``end`` (ISO ``YYYY-MM-DD``) are optional for spot/continuous-only
    baskets (the leaf resolvers borrow the date axis from the price
    series) but REQUIRED when any leg is an option-stream (the
    option_stream resolver needs a concrete date window).
    """

    model_config = ConfigDict(extra="forbid")

    basket: Annotated[
        Union[BasketRefSaved, BasketRefInline],
        Field(discriminator="kind"),
    ]
    start: str | None = Field(None, description="Start date YYYY-MM-DD")
    end: str | None = Field(None, description="End date YYYY-MM-DD")
    field: str = Field("close", description="Price field: close/open/high/low/volume")


@router.post("/basket/series")
async def get_basket_series(
    body: BasketSeriesRequest,
    svc: MarketDataService = Depends(get_market_data),
    repo: WriteRepository = Depends(get_write_repository),
) -> dict:
    """Compute a basket's composite weighted-sum series as ``{dates, values}``.

    Serves BOTH saved baskets (``{kind:"saved", basket_id}``) and inline
    baskets (``{kind:"inline", asset_class, legs}``).  Reuses the same
    materialisers + fetcher the in-signal basket path uses, so the
    series is identical (parity-tested).
    """
    try:
        start_date, end_date = parse_iso_range(body.start, body.end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    if isinstance(body.basket, BasketRefSaved):
        basket_id: str | None = body.basket.basket_id
        asset_class: str | None = None
        legs = None
    else:
        basket_id = None
        asset_class = body.basket.asset_class
        legs = body.basket.legs

    coverage: dict = {}
    try:
        dates, values = await compute_basket_series(
            svc=svc,
            repo=repo,
            basket_id=basket_id,
            asset_class=asset_class,
            legs=legs,
            start=start_date,
            end=end_date,
            field=body.field,
            coverage_out=coverage,
        )
    except (SignalValidationError, SignalDataError) as exc:
        # Both surface as a 400 on the Data page: an unknown/empty basket,
        # a disjoint leg date range, or an option-stream leg missing an
        # explicit window are all client-input problems (never a 500).
        raise ValidationError(str(exc)) from exc

    # ``coverage`` explains missing points (option legs with no two-sided quote,
    # no chain on a date, …) so the chart can annotate gaps instead of drawing a
    # silently broken line.  Empty for spot/continuous-only baskets.
    return {"dates": dates.tolist(), "values": values.tolist(), "coverage": coverage}


@router.get("/collections")
async def list_collections(
    asset_class: str | None = Query(
        None, description="Filter by asset class (equity, index, future)"
    ),
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
    adjustment: str = Query(
        "none", description="Adjustment method: none, ratio, difference"
    ),
    cycle: str | None = Query(None, description="Expiration cycle filter (e.g. HMUZ)"),
    roll_offset: int = Query(
        0, ge=0, le=30, description="Days before expiration to roll"
    ),
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
        start_date, end_date = parse_iso_range(start, end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    roll_config = ContinuousRollConfig(
        strategy=roll_strategy,
        adjustment=adj_method,
        cycle=cycle,
        roll_offset_days=roll_offset,
    )

    series = await svc.get_continuous(
        collection, roll_config, start=start_date, end=end_date
    )
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
        start_date, end_date = parse_iso_range(start, end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

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
