"""Portfolio router -- weighted portfolio computation endpoint."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, field_validator

from tcg.data._mongo.registry import CollectionRegistry
from tcg.data._utils import int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.engine import (
    aggregate_returns,
    compute_metrics,
    compute_weighted_portfolio,
)
from tcg.types.errors import ValidationError
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousLegSpec,
    ContinuousRollConfig,
    InstrumentId,
    RollStrategy,
)
from tcg.types.portfolio import RebalanceFreq

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_market_data(request: Request) -> MarketDataService:
    """Dependency: retrieve the MarketDataService from app state."""
    return request.app.state.market_data


def get_registry(request: Request) -> CollectionRegistry:
    """Dependency: retrieve the CollectionRegistry from the service.

    The registry is an internal detail of DefaultMarketDataService, but we
    need it to resolve asset_class from collection names.  Accessing
    ``_registry`` is acceptable here -- the API layer is tightly coupled
    to the concrete service by design.
    """
    return request.app.state.market_data._registry


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class LegSpec(BaseModel):
    type: str  # "instrument" or "continuous"
    collection: str
    symbol: str | None = None        # Required for "instrument"
    strategy: str | None = None      # Required for "continuous"
    adjustment: str | None = None    # Optional for "continuous" (default "none")
    cycle: str | None = None         # Optional for "continuous"

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("instrument", "continuous"):
            raise ValueError(f"leg type must be 'instrument' or 'continuous', got {v!r}")
        return v


class PortfolioRequest(BaseModel):
    legs: dict[str, LegSpec]
    weights: dict[str, float]
    rebalance: str = "none"
    return_type: str = "normal"
    start: str | None = None
    end: str | None = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/compute")
async def compute_portfolio(
    body: PortfolioRequest,
    svc: MarketDataService = Depends(get_market_data),
    registry: CollectionRegistry = Depends(get_registry),
) -> dict:
    """Compute a weighted portfolio with rebalancing and return full analytics."""

    # ── 1. Validate inputs ──

    if not body.legs:
        raise ValidationError("legs must not be empty")

    # Weights must cover every leg label
    missing_weights = set(body.legs.keys()) - set(body.weights.keys())
    if missing_weights:
        raise ValidationError(
            f"weights missing for legs: {', '.join(sorted(missing_weights))}"
        )

    # Rebalance frequency
    try:
        rebalance_freq = RebalanceFreq(body.rebalance)
    except ValueError:
        raise ValidationError(
            f"Invalid rebalance '{body.rebalance}'. "
            f"Must be one of: {', '.join(e.value for e in RebalanceFreq)}"
        )

    # Return type
    if body.return_type not in ("normal", "log"):
        raise ValidationError(
            f"return_type must be 'normal' or 'log', got {body.return_type!r}"
        )

    # Date parsing
    try:
        start_date = date.fromisoformat(body.start) if body.start else None
        end_date = date.fromisoformat(body.end) if body.end else None
    except ValueError as exc:
        raise ValidationError(f"Invalid date format: {exc}") from exc

    # ── 2. Convert legs to service types ──

    legs_spec: dict[str, InstrumentId | ContinuousLegSpec] = {}

    for label, leg in body.legs.items():
        if leg.type == "instrument":
            if not leg.symbol:
                raise ValidationError(
                    f"Leg '{label}': 'symbol' is required for instrument legs"
                )
            asset_class = registry.asset_class_for(leg.collection)
            if asset_class is None:
                raise ValidationError(
                    f"Leg '{label}': cannot determine asset class for "
                    f"collection '{leg.collection}'"
                )
            legs_spec[label] = InstrumentId(
                symbol=leg.symbol,
                asset_class=asset_class,
                collection=leg.collection,
            )

        else:  # "continuous"
            if not leg.strategy:
                raise ValidationError(
                    f"Leg '{label}': 'strategy' is required for continuous legs"
                )
            try:
                roll_strategy = RollStrategy(leg.strategy)
            except ValueError:
                raise ValidationError(
                    f"Leg '{label}': invalid strategy '{leg.strategy}'. "
                    f"Must be one of: {', '.join(e.value for e in RollStrategy)}"
                )

            adj_method = AdjustmentMethod.NONE
            if leg.adjustment:
                try:
                    adj_method = AdjustmentMethod(leg.adjustment)
                except ValueError:
                    raise ValidationError(
                        f"Leg '{label}': invalid adjustment '{leg.adjustment}'. "
                        f"Must be one of: {', '.join(e.value for e in AdjustmentMethod)}"
                    )

            legs_spec[label] = ContinuousLegSpec(
                collection=leg.collection,
                roll_config=ContinuousRollConfig(
                    strategy=roll_strategy,
                    adjustment=adj_method,
                    cycle=leg.cycle,
                ),
            )

    # ── 3. Fetch aligned prices ──
    #
    # Fetch the FULL overlapping date range first so we can report it to the
    # frontend (the slider needs to know the full extent even when computing
    # on a sub-range).  Then apply the user's start/end filter locally.

    full_common_dates, full_aligned_series = await svc.get_aligned_prices(
        legs_spec,
    )

    full_start_iso = int_to_iso(int(full_common_dates[0]))
    full_end_iso = int_to_iso(int(full_common_dates[-1]))

    # Apply optional date filter
    if start_date or end_date:
        lo = int(start_date.strftime("%Y%m%d")) if start_date else 0
        hi = int(end_date.strftime("%Y%m%d")) if end_date else 99999999
        mask = (full_common_dates >= lo) & (full_common_dates <= hi)
        common_dates = full_common_dates[mask]
        aligned_series = {
            label: type(series)(
                dates=series.dates[mask],
                open=series.open[mask],
                high=series.high[mask],
                low=series.low[mask],
                close=series.close[mask],
                volume=series.volume[mask],
            )
            for label, series in full_aligned_series.items()
        }
        if len(common_dates) == 0:
            raise ValidationError("No data in the selected date range")
    else:
        common_dates = full_common_dates
        aligned_series = full_aligned_series

    # Extract close prices for the engine
    aligned_closes = {label: series.close for label, series in aligned_series.items()}

    # ── 4. Compute portfolio ──

    result = compute_weighted_portfolio(
        aligned_closes,
        body.weights,
        rebalance_freq.value,
        body.return_type,
        common_dates,
    )

    # ── 5. Compute metrics ──

    metrics = compute_metrics(result.portfolio_equity)
    leg_metrics = {
        label: compute_metrics(eq)
        for label, eq in result.per_leg_equities.items()
    }

    # ── 6. Aggregate returns ──

    monthly = aggregate_returns(
        common_dates, result.portfolio_returns, result.per_leg_returns,
        body.return_type, "monthly",
    )
    yearly = aggregate_returns(
        common_dates, result.portfolio_returns, result.per_leg_returns,
        body.return_type, "yearly",
    )

    # ── 7. Build response ──

    dates_iso = [int_to_iso(int(d)) for d in common_dates]

    return {
        "dates": dates_iso,
        "portfolio_equity": result.portfolio_equity.tolist(),
        "leg_equities": {
            label: eq.tolist()
            for label, eq in result.per_leg_equities.items()
        },
        "raw_leg_equities": {
            label: eq.tolist()
            for label, eq in result.raw_leg_equities.items()
        },
        "rebalance_dates": [
            int_to_iso(int(d)) for d in result.rebalance_dates
        ],
        "metrics": asdict(metrics),
        "leg_metrics": {label: asdict(m) for label, m in leg_metrics.items()},
        "monthly_returns": monthly,
        "yearly_returns": yearly,
        "date_range": {"start": dates_iso[0], "end": dates_iso[-1]},
        "full_date_range": {"start": full_start_iso, "end": full_end_iso},
        "rebalance": rebalance_freq.value,
        "return_type": body.return_type,
    }
