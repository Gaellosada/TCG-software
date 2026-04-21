"""Portfolio router -- weighted portfolio computation endpoint."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from tcg.core.api.signals import (
    IndicatorSpecIn,
    SignalIn,
    compute_input_overlap,
    make_signal_fetcher,
    parse_signal,
)
from tcg.data._mongo.registry import CollectionRegistry
from tcg.data._utils import int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.engine import (
    aggregate_returns,
    compute_metrics,
    compute_weighted_portfolio,
)
from tcg.engine.signal_exec import (
    IndicatorSpecInput,
    SignalDataError,
    SignalRuntimeError,
    SignalValidationError,
    evaluate_signal,
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


class SignalLegSpec(BaseModel):
    """Full signal definition embedded in a portfolio leg."""

    spec: SignalIn
    indicators: list[IndicatorSpecIn] = Field(default_factory=list)


class LegSpec(BaseModel):
    type: str  # "instrument", "continuous", or "signal"
    collection: str | None = None    # Required for "instrument"/"continuous"
    symbol: str | None = None        # Required for "instrument"
    strategy: str | None = None      # Required for "continuous"
    adjustment: str | None = None    # Optional for "continuous" (default "none")
    cycle: str | None = None         # Optional for "continuous"
    roll_offset: int | None = None   # Optional for "continuous" (days before expiration)
    signal_spec: SignalLegSpec | None = None  # Required for "signal"

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("instrument", "continuous", "signal"):
            raise ValueError(
                f"leg type must be 'instrument', 'continuous', or 'signal', got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def validate_signal_has_spec(self) -> LegSpec:
        if self.type == "signal" and self.signal_spec is None:
            raise ValueError("signal legs require 'signal_spec'")
        return self


class PortfolioRequest(BaseModel):
    legs: dict[str, LegSpec]
    weights: dict[str, float]
    rebalance: str = "none"
    return_type: str = "normal"
    start: str | None = None
    end: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_legs(
    legs: dict[str, LegSpec],
    registry: CollectionRegistry,
) -> dict[str, InstrumentId | ContinuousLegSpec]:
    """Convert request leg specs to service-layer types with validation.

    Only processes instrument/continuous legs; signal legs are skipped.
    """
    legs_spec: dict[str, InstrumentId | ContinuousLegSpec] = {}

    for label, leg in legs.items():
        if leg.type == "signal":
            continue

        if leg.type == "instrument":
            if not leg.collection:
                raise ValidationError(
                    f"Leg '{label}': 'collection' is required for instrument legs"
                )
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
            if not leg.collection:
                raise ValidationError(
                    f"Leg '{label}': 'collection' is required for continuous legs"
                )
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

            roll_offset_days = 0
            if leg.roll_offset is not None:
                if not (0 <= leg.roll_offset <= 30):
                    raise ValidationError(
                        f"Leg '{label}': roll_offset must be between 0 and 30"
                    )
                roll_offset_days = leg.roll_offset

            legs_spec[label] = ContinuousLegSpec(
                collection=leg.collection,
                roll_config=ContinuousRollConfig(
                    strategy=roll_strategy,
                    adjustment=adj_method,
                    cycle=leg.cycle,
                    roll_offset_days=roll_offset_days,
                ),
            )

    return legs_spec


async def _evaluate_signal_leg(
    label: str,
    leg: LegSpec,
    svc: MarketDataService,
    start_date: date | None,
    end_date: date | None,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
    """Evaluate a signal leg and return (dates, synthetic_prices).

    The synthetic price series starts at 100 and accumulates the sum of
    all per-input realized_pnl arrays from the signal evaluation:

        synthetic = 100.0 * (1.0 + aggregated_pnl)

    Returns:
        Tuple of (YYYYMMDD int dates, synthetic price array).
    """
    if leg.signal_spec is None:
        raise ValidationError(f"Leg '{label}': signal legs require 'signal_spec'")

    # 1. Parse the signal spec into engine types
    try:
        signal = parse_signal(leg.signal_spec.spec)
    except SignalValidationError as exc:
        raise ValidationError(
            f"Leg '{label}': signal validation error: {exc}"
        ) from exc

    if len(signal.inputs) == 0:
        raise ValidationError(f"Leg '{label}': signal has no inputs")

    # 2. Parse indicators into IndicatorSpecInput dict
    indicators: dict[str, IndicatorSpecInput] = {}
    for ind_spec in leg.signal_spec.indicators:
        if ind_spec.id in indicators:
            raise ValidationError(
                f"Leg '{label}': duplicate indicator id {ind_spec.id!r}"
            )
        series_labels = tuple(ind_spec.seriesMap.keys())
        indicators[ind_spec.id] = IndicatorSpecInput(
            code=ind_spec.code,
            params=dict(ind_spec.params),
            series_labels=series_labels,
            series_map={
                lbl: (ref.collection, ref.instrument_id)
                for lbl, ref in ind_spec.seriesMap.items()
            },
        )

    # 3. Compute input overlap dates
    try:
        overlap_start, overlap_end = await compute_input_overlap(
            svc, signal, start_date, end_date,
        )
    except SignalDataError as exc:
        raise ValidationError(
            f"Leg '{label}': signal data error: {exc}"
        ) from exc

    # 4. Create fetcher and evaluate
    fetcher = make_signal_fetcher(svc, overlap_start, overlap_end)
    try:
        result = await evaluate_signal(signal, indicators, fetcher)
    except SignalValidationError as exc:
        raise ValidationError(
            f"Leg '{label}': signal validation error: {exc}"
        ) from exc
    except SignalDataError as exc:
        raise ValidationError(
            f"Leg '{label}': signal data error: {exc}"
        ) from exc
    except SignalRuntimeError as exc:
        raise ValidationError(
            f"Leg '{label}': signal runtime error: {exc}"
        ) from exc

    # 5. Aggregate realized_pnl across all inputs
    T = len(result.index)
    aggregated_pnl = np.zeros(T, dtype=np.float64)
    for pos in result.positions:
        aggregated_pnl += pos.realized_pnl

    # 6. Convert to synthetic prices (starting at 100)
    synthetic = 100.0 * (1.0 + aggregated_pnl)

    return result.index, synthetic


# ---------------------------------------------------------------------------
# Endpoints
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

    # ── 2. Separate legs by type ──

    instrument_legs = {
        label: leg for label, leg in body.legs.items()
        if leg.type in ("instrument", "continuous")
    }
    signal_legs = {
        label: leg for label, leg in body.legs.items()
        if leg.type == "signal"
    }

    # ── 3. Fetch instrument prices (if any) ──

    # Will hold YYYYMMDD int dates for each source.
    all_date_grids: list[npt.NDArray[np.int64]] = []
    # Will hold (full_dates, full_closes) per label for instrument legs.
    instrument_full_dates: npt.NDArray[np.int64] | None = None
    instrument_dates: npt.NDArray[np.int64] | None = None
    instrument_closes: dict[str, npt.NDArray[np.float64]] = {}

    if instrument_legs:
        legs_spec = _parse_legs(body.legs, registry)

        # Fetch full overlapping date range for instrument legs.
        full_common_dates, full_aligned_series = await svc.get_aligned_prices(
            legs_spec,
        )
        instrument_full_dates = full_common_dates

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

        instrument_dates = common_dates
        instrument_closes = {
            label: series.close for label, series in aligned_series.items()
        }
        all_date_grids.append(instrument_dates)

    # ── 4. Evaluate signal legs (if any) ──

    # signal_dates[label] = YYYYMMDD array, signal_closes[label] = synthetic prices
    signal_dates_map: dict[str, npt.NDArray[np.int64]] = {}
    signal_closes: dict[str, npt.NDArray[np.float64]] = {}

    for label, leg in signal_legs.items():
        sig_dates, sig_prices = await _evaluate_signal_leg(
            label, leg, svc, start_date, end_date,
        )
        signal_dates_map[label] = sig_dates
        signal_closes[label] = sig_prices
        all_date_grids.append(sig_dates)

    # ── 5. Align all series to common dates ──

    if not all_date_grids:
        raise ValidationError("No valid legs to compute")

    # Find intersection of all date grids
    common_dates = all_date_grids[0]
    for grid in all_date_grids[1:]:
        common_dates = np.intersect1d(common_dates, grid, assume_unique=False)

    if len(common_dates) == 0:
        raise ValidationError(
            "No overlapping dates across all legs — "
            "instrument and signal date ranges are disjoint"
        )

    # Slice instrument closes to common dates
    aligned_closes: dict[str, npt.NDArray[np.float64]] = {}
    if instrument_dates is not None:
        inst_mask = np.isin(instrument_dates, common_dates, assume_unique=True)
        for label, closes in instrument_closes.items():
            aligned_closes[label] = closes[inst_mask]

    # Slice signal closes to common dates
    for label in signal_closes:
        sig_mask = np.isin(
            signal_dates_map[label], common_dates, assume_unique=True,
        )
        aligned_closes[label] = signal_closes[label][sig_mask]

    # ── 6. Compute full date range for the slider ──
    #
    # For instrument-only portfolios the full_date_range is the full
    # (unfiltered) instrument overlap.  For mixed or signal-only, we
    # combine all full-extent date arrays.

    full_date_grids: list[npt.NDArray[np.int64]] = []
    if instrument_full_dates is not None:
        full_date_grids.append(instrument_full_dates)
    for label in signal_dates_map:
        # Signal dates are already the full evaluation range (overlap_start
        # to overlap_end within each signal). Use them as-is.
        full_date_grids.append(signal_dates_map[label])

    full_common_all = full_date_grids[0]
    for grid in full_date_grids[1:]:
        full_common_all = np.intersect1d(
            full_common_all, grid, assume_unique=False,
        )
    full_start_iso = int_to_iso(int(full_common_all[0]))
    full_end_iso = int_to_iso(int(full_common_all[-1]))

    # ── 7. Compute portfolio ──

    result = compute_weighted_portfolio(
        aligned_closes,
        body.weights,
        rebalance_freq.value,
        body.return_type,
        common_dates,
    )

    # ── 8. Compute metrics ──

    metrics = compute_metrics(result.portfolio_equity)
    leg_metrics = {
        label: compute_metrics(eq)
        for label, eq in result.per_leg_equities.items()
    }

    # ── 9. Aggregate returns ──

    monthly = aggregate_returns(
        common_dates, result.portfolio_returns, result.per_leg_returns,
        body.return_type, "monthly",
    )
    yearly = aggregate_returns(
        common_dates, result.portfolio_returns, result.per_leg_returns,
        body.return_type, "yearly",
    )

    # ── 10. Build response ──

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
