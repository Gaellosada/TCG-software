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
from tcg.data.protocols import MarketDataServiceV2
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import AdjustmentMethod, ContinuousRollConfig, RollStrategy

router = APIRouter(prefix="/api/data-v2", tags=["data-v2"])


# --- Object browsing ---


@router.get("/objects")
async def list_objects(
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
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
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
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
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
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
    target: float | None = Query(
        None, description="Strike (absolute) or moneyness ratio"
    ),
    option_type: str = Query("put", description="call | put"),
    roll: str = Query("at_expiry", description="Roll rule (only at_expiry in v2)"),
    start: str | None = Query(None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(None, description="End date YYYY-MM-DD"),
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Build a v2 continuous options settlement stream.

    ``criterion='delta'`` returns HTTP 400 (greeks unavailable in v2) — rejected
    outright, whether or not ``target`` is supplied. ``roll`` only supports
    ``at_expiry`` this round.
    """
    if roll != "at_expiry":
        raise ValidationError(
            f"Invalid roll '{roll}'. Only 'at_expiry' is supported in v2."
        )
    # Delta is rejected outright in v2 (no greeks) — return the friendly 400
    # regardless of whether ``target`` was given (an omitted target would
    # otherwise surface a generic 422 before this point).
    if criterion == "delta":
        raise ValidationError(
            "Delta-based selection is unavailable in Database v2: the v2 "
            "warehouse has no greeks (fact_greeks is empty). Use criterion "
            "'strike' or 'moneyness'."
        )
    if target is None:
        raise ValidationError(
            f"Query param 'target' is required for criterion {criterion!r} "
            "(absolute strike or moneyness ratio)."
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
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Read one serie's facts (fact table dispatched by ``serie.type``)."""
    try:
        start_date, end_date = parse_iso_range(start, end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return await svc.get_series(serie_id, start=start_date, end=end_date)


@router.get("/objects/{object_id}/facets")
async def get_object_facets(
    object_id: int,
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Filterable dimensions of one object — feeds the series filter form.

    Kept with the object routes, above the ``/objects/{object_id}`` catch-all.
    Unlike the single-segment routes above, declaration order is NOT
    load-bearing here: the catch-all compiles to ``objects/(?P<object_id>[^/]+)$``
    and ``[^/]+`` cannot span the ``/facets`` segment, so it can never capture
    this path whatever the order (measured — see
    ``test_facets_route_dispatches_to_the_facets_handler``). It would start to
    matter if that convertor were ever widened to ``{object_id:path}``.
    """
    return await svc.get_object_facets(object_id)


#: Filter enum domains, mirrored from the reader's whitelist so a bad value is
#: a clean 400 at the boundary rather than a DataAccessError from the adapter.
#: Duplicated rather than imported so the error message has a stable order (the
#: reader's are frozensets); ``test_route_enum_whitelists_match_the_readers``
#: pins them against each other so the copies cannot drift.
_SERIE_TYPE_VALUES = ("bar", "value", "greeks", "bbba", "any")
_FREQ_VALUES = ("1m", "daily", "any")
_OPTION_TYPE_VALUES = ("call", "put", "both")


@router.get("/objects/{object_id}/series")
async def list_object_series(
    object_id: int,
    expiration_min: str | None = Query(
        None, description="Earliest expiration YYYY-MM-DD"
    ),
    expiration_max: str | None = Query(
        None, description="Latest expiration YYYY-MM-DD"
    ),
    strike_min: float | None = Query(None, description="Strike lower bound"),
    strike_max: float | None = Query(None, description="Strike upper bound"),
    option_type: str = Query("both", description="call | put | both"),
    serie_type: str = Query("any", description="bar | value | greeks | bbba | any"),
    freq: str = Query("any", description="1m | daily | any"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """One filtered, paginated page of an object's series.

    Kept with the object routes, above the ``/objects/{object_id}`` catch-all.
    As with ``/facets``, declaration order is NOT load-bearing for a
    two-segment path (``[^/]+`` cannot span the ``/series`` segment); the
    placement is for consistency. ``limit`` is capped at 500 — the same cap v1
    uses in ``tcg/core/api/data.py``.

    A filter that matches nothing returns 200 with an empty ``items`` and
    ``total: 0`` — a narrow filter is a result, not an error.
    """
    if serie_type not in _SERIE_TYPE_VALUES:
        raise ValidationError(
            f"Invalid serie_type {serie_type!r}. Must be one of: "
            f"{', '.join(_SERIE_TYPE_VALUES)}"
        )
    if freq not in _FREQ_VALUES:
        raise ValidationError(
            f"Invalid freq {freq!r}. Must be one of: {', '.join(_FREQ_VALUES)}"
        )
    if option_type not in _OPTION_TYPE_VALUES:
        raise ValidationError(
            f"Invalid option_type {option_type!r}. Must be one of: "
            f"{', '.join(_OPTION_TYPE_VALUES)}"
        )
    try:
        exp_min, exp_max = parse_iso_range(expiration_min, expiration_max)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if exp_min is not None and exp_max is not None and exp_min > exp_max:
        raise ValidationError(
            f"expiration_min ({exp_min.isoformat()}) is after "
            f"expiration_max ({exp_max.isoformat()})"
        )
    if strike_min is not None and strike_max is not None and strike_min > strike_max:
        raise ValidationError(
            f"strike_min ({strike_min}) is greater than strike_max ({strike_max})"
        )

    return await svc.list_object_series(
        object_id,
        expiration_min=exp_min,
        expiration_max=exp_max,
        strike_min=strike_min,
        strike_max=strike_max,
        option_type=option_type,
        serie_type=serie_type,
        freq=freq,
        skip=skip,
        limit=limit,
    )


# --- Object detail (catch-all id route: declared LAST) ---


@router.get("/objects/{object_id}")
async def get_object_detail(
    object_id: int,
    svc: MarketDataServiceV2 = Depends(get_market_data_v2),
) -> dict:
    """Return ``{object}`` for one object — metadata only.

    Contracts and series are deliberately NOT here: they come from
    ``/objects/{id}/facets`` (aggregated dimensions) and
    ``/objects/{id}/series`` (filtered + paginated). Shipping them inline was
    38 239 859 bytes in ~36 s on object 12.
    """
    return await svc.get_object_detail(object_id)
