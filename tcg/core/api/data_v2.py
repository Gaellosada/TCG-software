"""Data v2 router — endpoints over the ``tcg_instruments_v2`` star schema.

Prefix ``/api/data-v2``. Parallel to the v1 ``/api/data`` router; reuses the
same read-only ``tcg_read`` pool via a distinct service
(:class:`DefaultMarketDataServiceV2`). Route ordering matters: the
``/continuous/*`` and ``/series/*`` routes are declared BEFORE any
``/objects/{object_id}`` path so a literal segment is never captured as an id
(the v1 catch-all gotcha).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from tcg.core.api._dates import parse_iso_range
from tcg.core.api.common import get_market_data_v2
from tcg.data.service_v2 import DefaultMarketDataServiceV2
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import AdjustmentMethod, ContinuousRollConfig, RollStrategy

router = APIRouter(prefix="/api/data-v2", tags=["data-v2"])


# --- Object browsing ---


@router.get("/objects")
async def list_objects(
    svc: DefaultMarketDataServiceV2 = Depends(get_market_data_v2),
) -> list[dict]:
    """List every v2 object (all kinds). The frontend groups by ``kind``."""
    objects = await svc.list_objects()
    return [
        {
            "object_id": o["object_id"],
            "kind": o["kind"],
            "symbol": o["symbol"],
            "name": o["name"],
            "cycle": o["cycle"],
            "underlying_object_id": o["underlying_object_id"],
        }
        for o in objects
    ]


# --- Continuous futures (declared BEFORE /objects/{object_id}) ---


@router.get("/continuous/futures/{object_id}/cycles")
async def get_future_cycles(
    object_id: int,
    svc: DefaultMarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Return available listing cycles for a future object."""
    cycles = await svc.get_future_cycles(object_id)
    return {"cycles": cycles}


@router.get("/continuous/futures/{object_id}")
async def get_continuous_future(
    object_id: int,
    strategy: str = Query("front_month", description="Roll strategy"),
    adjustment: str = Query(
        "none", description="Adjustment method: none, ratio, difference"
    ),
    cycle: str | None = Query(None, description="Listing cycle (informational in v2)"),
    roll_offset: int = Query(
        0, ge=0, le=365, description="Days before expiration to roll (0-365)"
    ),
    rank: int = Query(
        1,
        ge=1,
        le=12,
        description="NTH_NEAREST only: hold the rank-th nearest contract (1=front)",
    ),
    start: str | None = Query(None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(None, description="End date YYYY-MM-DD"),
    svc: DefaultMarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Build a continuous futures series (v1-continuous-shape) for a v2 future."""
    try:
        roll_strategy = RollStrategy(strategy)
    except ValueError:
        raise ValidationError(
            f"Invalid strategy '{strategy}'. Must be one of: "
            f"{', '.join(e.value for e in RollStrategy)}"
        )
    try:
        adj_method = AdjustmentMethod(adjustment)
    except ValueError:
        raise ValidationError(
            f"Invalid adjustment '{adjustment}'. Must be one of: "
            f"{', '.join(e.value for e in AdjustmentMethod)}"
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
        rank=rank,
    )
    series = await svc.get_continuous_future(
        object_id, roll_config, start=start_date, end=end_date
    )
    if series is None:
        raise DataNotFoundError(f"No continuous series found for object {object_id}")
    return {
        "object_id": object_id,
        "collection": series.collection,
        "strategy": roll_config.strategy.value,
        "adjustment": roll_config.adjustment.value,
        "cycle": roll_config.cycle,
        "rank": roll_config.rank,
        "roll_dates": list(series.roll_dates),
        "contracts": list(series.contracts),
        "prices": {
            "dates": series.prices.dates.tolist(),
            "open": series.prices.open.tolist(),
            "high": series.prices.high.tolist(),
            "low": series.prices.low.tolist(),
            "close": series.prices.close.tolist(),
            "volume": series.prices.volume.tolist(),
        },
        # Flat mirrors (v1 parity) so a v1-style Chart consumer works unchanged.
        "dates": series.prices.dates.tolist(),
        "open": series.prices.open.tolist(),
        "high": series.prices.high.tolist(),
        "low": series.prices.low.tolist(),
        "close": series.prices.close.tolist(),
        "volume": series.prices.volume.tolist(),
    }


# --- Continuous options (v2-native settlement selection) ---


@router.get("/continuous/options/{object_id}")
async def get_continuous_options(
    object_id: int,
    criterion: str = Query(
        "strike", description="Selection criterion: strike | moneyness | delta"
    ),
    target: float = Query(..., description="Strike (absolute) or moneyness ratio"),
    option_type: str = Query("put", description="call | put"),
    roll: str = Query("at_expiry", description="Roll rule (only at_expiry in v2)"),
    start: str | None = Query(None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(None, description="End date YYYY-MM-DD"),
    svc: DefaultMarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Build a v2 continuous options settlement stream.

    ``criterion='delta'`` returns HTTP 400 (greeks unavailable in v2). ``roll``
    only supports ``at_expiry`` this round.
    """
    if roll != "at_expiry":
        raise ValidationError(
            f"Invalid roll '{roll}'. Only 'at_expiry' is supported in v2."
        )
    try:
        start_date, end_date = parse_iso_range(start, end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    result = await svc.get_continuous_options(
        object_id,
        criterion=criterion,
        target=target,
        option_type=option_type,
        start=start_date,
        end=end_date,
    )
    if not result.dates:
        raise DataNotFoundError(
            f"No option settlement data for object {object_id} "
            f"({option_type}, {criterion}={target}) in the given window"
        )
    return {
        "object_id": result.object_id,
        "criterion": result.criterion,
        "option_type": result.option_type,
        "target": target,
        "roll": roll,
        "spot_source": "underlying_future_front_close"
        if result.criterion == "moneyness"
        else None,
        "points": {
            "ts": list(result.dates),
            "value": list(result.values),
            "contract": list(result.contract_codes),  # per-date, 1:1 with ts
        },
        "roll_dates": list(result.roll_dates),
        "contracts": list(result.contracts),
    }


# --- Series facts (declared BEFORE /objects/{object_id}) ---


@router.get("/series/{serie_id}")
async def get_series(
    serie_id: int,
    start: str | None = Query(None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(None, description="End date YYYY-MM-DD"),
    svc: DefaultMarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Read one serie's facts (fact table dispatched by ``serie.type``)."""
    try:
        start_date, end_date = parse_iso_range(start, end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return await svc.get_series(serie_id, start=start_date, end=end_date)


# --- Object detail (catch-all id route: declared LAST) ---


@router.get("/objects/{object_id}")
async def get_object_detail(
    object_id: int,
    svc: DefaultMarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Return ``{object, contracts, series}`` for one object."""
    return await svc.get_object_detail(object_id)
