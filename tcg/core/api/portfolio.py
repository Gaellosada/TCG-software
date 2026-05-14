"""Portfolio router -- weighted portfolio computation endpoint."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Literal

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models import OptionStreamLabel, OptionStreamRef
from tcg.core.api._models_options import MaturityRule, SelectionCriterion
from tcg.core.api._options_materialise import (
    PRICE_LIKE_STREAMS,
    materialise_option_streams,
)
from tcg.core.api._serializers import nan_safe_floats
from tcg.core.api.common import get_market_data
from tcg.core.api.signals import (
    IndicatorSpecIn,
    SignalIn,
    compute_input_overlap,
    make_signal_fetcher,
    parse_signal,
)
from tcg.data._mongo.registry import CollectionRegistry
from tcg.data._utils import date_to_int, int_to_iso
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
from tcg.types.signal import (
    InstrumentContinuous,
    InstrumentOptionStream,
    InstrumentSpot,
    Trade,
)


logger = logging.getLogger(__name__)


def _signal_input_underlying_id(instrument: object) -> str | None:
    """Resolve a signal Input's bound instrument to the underlying instrument
    identifier used elsewhere in the portfolio response.

    Mirrors the direct-leg conventions:
      * spot       → ``instrument_id`` (matches ``LegSpec.symbol``)
      * continuous → ``collection``    (matches ``LegSpec.collection``)
      * option_stream → ``collection``

    Returns ``None`` for unknown instrument variants so the caller can fall
    back to the signal-local input id rather than crash.
    """
    if isinstance(instrument, InstrumentSpot):
        return instrument.instrument_id
    if isinstance(instrument, InstrumentContinuous):
        return instrument.collection
    if isinstance(instrument, InstrumentOptionStream):
        return instrument.collection
    return None


@dataclass(frozen=True)
class _SignalLegEvalResult:
    """Internal aggregate of what a signal leg produces for the portfolio.

    ``index`` and ``synthetic`` keep the existing aggregation contract;
    ``trades`` and ``positions_payload`` are bubbled up for the trade log.
    Each entry in ``positions_payload`` mirrors the signals-API positions
    shape: ``{input_id, price: {label, values} | None}``.
    """

    index: npt.NDArray[np.int64]
    synthetic: npt.NDArray[np.float64]
    trades: tuple[Trade, ...] = ()
    positions_payload: tuple[dict, ...] = ()


router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


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
    type: str  # "instrument", "continuous", "signal", or "option_stream"
    collection: str | None = (
        None  # Required for "instrument"/"continuous"/"option_stream"
    )
    symbol: str | None = None  # Required for "instrument"
    strategy: str | None = None  # Required for "continuous"
    adjustment: str | None = None  # Optional for "continuous" (default "none")
    cycle: str | None = None  # Optional for "continuous" and "option_stream"
    roll_offset: int | None = None  # Optional for "continuous" (days before expiration)
    signal_spec: SignalLegSpec | None = None  # Required for "signal"
    # Option-stream fields (required when type == "option_stream")
    option_type: Literal["C", "P"] | None = None
    maturity: MaturityRule | None = None
    selection: SelectionCriterion | None = None
    stream: OptionStreamLabel | None = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("instrument", "continuous", "signal", "option_stream"):
            raise ValueError(
                f"leg type must be 'instrument', 'continuous', 'signal', "
                f"or 'option_stream', got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def validate_signal_has_spec(self) -> LegSpec:
        if self.type == "signal" and self.signal_spec is None:
            raise ValueError("signal legs require 'signal_spec'")
        return self

    @model_validator(mode="after")
    def validate_option_stream_has_fields(self) -> LegSpec:
        """Ensure option_stream legs carry all required option fields."""
        if self.type != "option_stream":
            return self
        missing: list[str] = []
        if self.collection is None:
            missing.append("collection")
        if self.option_type is None:
            missing.append("option_type")
        if self.maturity is None:
            missing.append("maturity")
        if self.selection is None:
            missing.append("selection")
        if self.stream is None:
            missing.append("stream")
        if missing:
            raise ValueError(f"option_stream legs require: {', '.join(missing)}")
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

    Only processes instrument/continuous legs; signal and option_stream
    legs are skipped (handled separately).
    """
    legs_spec: dict[str, InstrumentId | ContinuousLegSpec] = {}

    for label, leg in legs.items():
        if leg.type in ("signal", "option_stream"):
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
) -> _SignalLegEvalResult:
    """Evaluate a signal leg and bubble up everything the portfolio path needs.

    The synthetic price series starts at 100 and accumulates the sum of
    all per-input realized_pnl arrays from the signal evaluation:

        synthetic = 100.0 * (1.0 + aggregated_pnl)

    Returns:
        ``_SignalLegEvalResult`` carrying the YYYYMMDD int date index, the
        synthetic price series, the raw per-signal ``Trade`` tuple (bar
        indices in the signal's own index space, NOT the portfolio's
        common_dates — caller is responsible for re-mapping), and the
        per-input price payloads matching the signals-API positions shape.
    """
    if leg.signal_spec is None:
        raise ValidationError(f"Leg '{label}': signal legs require 'signal_spec'")

    # 1. Parse the signal spec into engine types
    try:
        signal = parse_signal(leg.signal_spec.spec)
    except SignalValidationError as exc:
        raise ValidationError(f"Leg '{label}': signal validation error: {exc}") from exc

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
            svc,
            signal,
            start_date,
            end_date,
        )
    except SignalDataError as exc:
        raise ValidationError(f"Leg '{label}': signal data error: {exc}") from exc

    # 4. Create fetcher and evaluate
    fetcher = make_signal_fetcher(svc, overlap_start, overlap_end)
    try:
        result = await evaluate_signal(signal, indicators, fetcher)
    except SignalValidationError as exc:
        raise ValidationError(f"Leg '{label}': signal validation error: {exc}") from exc
    except SignalDataError as exc:
        raise ValidationError(f"Leg '{label}': signal data error: {exc}") from exc
    except SignalRuntimeError as exc:
        raise ValidationError(f"Leg '{label}': signal runtime error: {exc}") from exc

    # 5. Aggregate realized_pnl across all inputs
    T = len(result.index)
    aggregated_pnl = np.zeros(T, dtype=np.float64)
    for pos in result.positions:
        aggregated_pnl += pos.realized_pnl

    # 6. Convert to synthetic prices (starting at 100)
    synthetic = 100.0 * (1.0 + aggregated_pnl)

    # 7. Build the signal-local → underlying instrument id remap. Trades
    #    and per-input positions are keyed by the signal-LOCAL input name
    #    (e.g. "index"); at the portfolio layer we want the actual
    #    underlying instrument id (e.g. "SPX") so signal-leg trades line
    #    up with direct-leg trades in the TradeLog. Missing entries fall
    #    back to the signal-local id with a warning (would indicate a
    #    bug or stale data).
    underlying_by_local: dict[str, str] = {}
    for inp in signal.inputs:
        underlying = _signal_input_underlying_id(inp.instrument)
        if underlying is None:
            logger.warning(
                "portfolio: signal %r input %r has unrecognised instrument "
                "variant %r — keeping signal-local id for trade/position "
                "remap",
                label,
                inp.id,
                type(inp.instrument).__name__,
            )
            continue
        underlying_by_local[inp.id] = underlying

    def _remap_id(local_id: str) -> str:
        mapped = underlying_by_local.get(local_id)
        if mapped is None:
            logger.warning(
                "portfolio: signal %r emitted input_id %r with no matching "
                "Input — keeping original id",
                label,
                local_id,
            )
            return local_id
        return mapped

    remapped_trades = tuple(
        replace(tr, input_id=_remap_id(tr.input_id)) for tr in result.trades
    )

    # 8. Build per-input price payloads in the signals-API shape so the
    #    portfolio TradeLog can look up open/close prices by input_id.
    positions_payload: list[dict] = []
    for pos in result.positions:
        if pos.price_label is None or pos.price_values is None:
            price_payload: dict | None = None
        else:
            price_payload = {
                "label": pos.price_label,
                "values": nan_safe_floats(pos.price_values),
            }
        positions_payload.append(
            {"input_id": _remap_id(pos.input_id), "price": price_payload}
        )

    return _SignalLegEvalResult(
        index=result.index,
        synthetic=synthetic,
        trades=remapped_trades,
        positions_payload=tuple(positions_payload),
    )


def _compute_level_metrics(values: npt.NDArray[np.float64]) -> dict:
    """Compute summary metrics for a level (non-price) series."""
    valid = values[~np.isnan(values)]
    if len(valid) == 0:
        return {
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "first": None,
            "last": None,
            "change": None,
        }
    return {
        "mean": float(np.mean(valid)),
        "std": float(np.std(valid)),
        "min": float(np.min(valid)),
        "max": float(np.max(valid)),
        "first": float(valid[0]),
        "last": float(valid[-1]),
        "change": float(valid[-1] - valid[0]),
    }


async def _evaluate_option_stream_leg(
    label: str,
    leg: LegSpec,
    svc: MarketDataService,
    start_date: date | None,
    end_date: date | None,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64], str]:
    """Resolve an option_stream leg and return (dates, values, stream_mode).

    stream_mode is "price" for mid stream (participates in equity curve)
    or "level" for greeks/IV (tracking overlay only).

    Returns:
        Tuple of (YYYYMMDD int dates, values array, stream_mode).
    """
    # 1. Build an OptionStreamRef from the leg's fields
    ref = OptionStreamRef(
        type="option_stream",
        collection=leg.collection,
        option_type=leg.option_type,
        cycle=leg.cycle,
        maturity=leg.maturity,
        selection=leg.selection,
        stream=leg.stream,
    )

    # 2. Materialise via shared infrastructure
    result = await materialise_option_streams(
        [("_leg", ref)],
        svc=svc,
        start_date=start_date,
        end_date=end_date,
    )
    if isinstance(result, str):
        raise ValidationError(f"Leg '{label}': {result}")

    dates_arr, values, _diagnostics, _contracts = result["_leg"]

    # 3. Determine stream mode
    stream_mode = "price" if leg.stream in PRICE_LIKE_STREAMS else "level"

    # 4. Forward-fill NaN for price streams (needed for returns/equity)
    if stream_mode == "price":
        nan_mask = np.isnan(values)
        if nan_mask.all():
            raise ValidationError(f"Leg '{label}': all option stream values are NaN")
        # Forward fill
        for i in range(1, len(values)):
            if nan_mask[i]:
                values[i] = values[i - 1]
        # If first value is NaN, backfill from first valid
        if nan_mask[0]:
            first_valid = int(np.argmax(~nan_mask))
            values[:first_valid] = values[first_valid]

    return dates_arr, values, stream_mode


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

    try:
        start_date, end_date = parse_iso_range(body.start, body.end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    # ── 2. Separate legs by type ──

    instrument_legs = {
        label: leg
        for label, leg in body.legs.items()
        if leg.type in ("instrument", "continuous")
    }
    signal_legs = {
        label: leg for label, leg in body.legs.items() if leg.type == "signal"
    }
    option_stream_legs = {
        label: leg for label, leg in body.legs.items() if leg.type == "option_stream"
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
            lo = date_to_int(start_date) if start_date else 0
            hi = date_to_int(end_date) if end_date else 99999999
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
    # Per-leg trade + positions payloads bubbled up from _evaluate_signal_leg
    # for portfolio-level trade log aggregation (see §10 below).
    signal_trades_map: dict[str, tuple[Trade, ...]] = {}
    signal_positions_map: dict[str, tuple[dict, ...]] = {}

    for label, leg in signal_legs.items():
        leg_result = await _evaluate_signal_leg(
            label,
            leg,
            svc,
            start_date,
            end_date,
        )
        signal_dates_map[label] = leg_result.index
        signal_closes[label] = leg_result.synthetic
        signal_trades_map[label] = leg_result.trades
        signal_positions_map[label] = leg_result.positions_payload
        all_date_grids.append(leg_result.index)

    # ── 4.5. Evaluate option_stream legs (if any) ──

    option_stream_dates_map: dict[str, npt.NDArray[np.int64]] = {}
    option_stream_closes: dict[str, npt.NDArray[np.float64]] = {}
    tracking_series: dict[str, dict] = {}  # level legs -> separate response section

    for label, leg in option_stream_legs.items():
        os_dates, os_values, stream_mode = await _evaluate_option_stream_leg(
            label,
            leg,
            svc,
            start_date,
            end_date,
        )

        if stream_mode == "price":
            # Price leg -- joins the main portfolio equity curve
            option_stream_dates_map[label] = os_dates
            option_stream_closes[label] = os_values
            all_date_grids.append(os_dates)
        else:
            # Level leg -- tracking overlay only (not in equity curve)
            tracking_series[label] = {
                "dates": [int_to_iso(int(d)) for d in os_dates],
                "values": nan_safe_floats(os_values),
                "stream": leg.stream,
                "stream_mode": "level",
                "metrics": _compute_level_metrics(os_values),
            }

    # ── 5. Align all series to common dates ──

    if not all_date_grids:
        raise ValidationError(
            "No price-like legs to compute portfolio equity curve. "
            "Use 'mid' stream for option legs that should participate "
            "in the portfolio."
        )

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
            signal_dates_map[label],
            common_dates,
            assume_unique=True,
        )
        aligned_closes[label] = signal_closes[label][sig_mask]

    # Slice option_stream price closes to common dates
    for label in option_stream_closes:
        os_mask = np.isin(
            option_stream_dates_map[label],
            common_dates,
            assume_unique=True,
        )
        aligned_closes[label] = option_stream_closes[label][os_mask]

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
    for label in option_stream_dates_map:
        full_date_grids.append(option_stream_dates_map[label])

    full_common_all = full_date_grids[0]
    for grid in full_date_grids[1:]:
        full_common_all = np.intersect1d(
            full_common_all,
            grid,
            assume_unique=False,
        )
    full_start_iso = int_to_iso(int(full_common_all[0]))
    full_end_iso = int_to_iso(int(full_common_all[-1]))

    # ── 7. Compute portfolio ──

    # Filter weights to only include legs present in aligned_closes.
    # Level-mode option_stream legs are in tracking_series, not
    # aligned_closes, so they are naturally excluded.
    result = compute_weighted_portfolio(
        aligned_closes,
        {label: body.weights[label] for label in aligned_closes},
        rebalance_freq.value,
        body.return_type,
        common_dates,
    )

    # ── 8. Compute metrics ──

    metrics = compute_metrics(result.portfolio_equity)
    leg_metrics = {
        label: compute_metrics(eq) for label, eq in result.per_leg_equities.items()
    }

    # ── 9. Aggregate returns ──

    monthly = aggregate_returns(
        common_dates,
        result.portfolio_returns,
        result.per_leg_returns,
        body.return_type,
        "monthly",
    )
    yearly = aggregate_returns(
        common_dates,
        result.portfolio_returns,
        result.per_leg_returns,
        body.return_type,
        "yearly",
    )

    # ── 10. Aggregate trades + per-input positions across signal legs ──
    #
    # Each signal leg evaluates against its own date overlap (per-signal
    # ``result.index``). Trade bar indices and positions price arrays are
    # therefore in that per-signal axis, NOT the portfolio's common_dates.
    # We re-map every trade endpoint onto common_dates via a date→index
    # dict; trades whose endpoints fall outside common_dates are DROPPED
    # (not clamped) — they refer to bars the user can't see in the
    # portfolio chart, so they'd index out of bounds on the frontend.

    cd_index: dict[int, int] = {int(d): i for i, d in enumerate(common_dates)}

    aggregated_trades: list[dict] = []
    for label, trades in signal_trades_map.items():
        sig_idx = signal_dates_map[label]
        # ``body.weights[label]`` is the user-facing PERCENT allocation
        # (frontend default 100). For trade-size scaling we need the
        # FRACTION form (0.0 … 1.0+) so ``signed_weight`` stays in
        # fraction units across direct + signal legs.
        leg_fraction = float(body.weights[label]) / 100.0
        for tr in trades:
            # Re-map the open bar (signal-axis index → common_dates index).
            # If the trade's open date isn't part of common_dates, DROP —
            # the trade can't be placed on the portfolio's date axis.
            sig_open_date = int(sig_idx[tr.open_bar])
            new_open = cd_index.get(sig_open_date)
            if new_open is None:
                continue
            if tr.close_bar is None:
                # Open trade: open date is in common_dates → keep with
                # close_bar=None. The frontend renders an effective close
                # price using the last finite value from positions[].
                # ``open_bar`` is NOT restricted to the signal's last bar;
                # the engine emits open trades wherever an entry block
                # latched and never closed (see engine
                # test_trades_open_at_end).
                new_close: int | None = None
            else:
                sig_close_date = int(sig_idx[tr.close_bar])
                mapped_close = cd_index.get(sig_close_date)
                if mapped_close is None:
                    continue
                new_close = mapped_close
            aggregated_trades.append(
                {
                    "input_id": tr.input_id,
                    "entry_block_id": tr.entry_block_id,
                    "entry_block_name": tr.entry_block_name,
                    "exit_block_id": tr.exit_block_id,
                    "exit_block_name": tr.exit_block_name,
                    "open_bar": new_open,
                    "close_bar": new_close,
                    "direction": tr.direction,
                    "signed_weight": tr.signed_weight * leg_fraction,
                    "holding_id": label,
                    "holding_name": label,
                }
            )

    # Direct legs have no engine trades; surface them as a single open Holding
    # so they appear in the trade log alongside signal-leg trades.
    for label, leg in body.legs.items():
        if leg.type == "signal":
            continue
        if leg.type == "instrument":
            direct_input_id = leg.symbol or label
        elif leg.type == "continuous":
            direct_input_id = leg.collection or label
        else:
            direct_input_id = label
        # See note above: convert PERCENT allocation → FRACTION for the
        # trade's signed_weight (trades use fraction units uniformly).
        leg_fraction = float(body.weights[label]) / 100.0
        aggregated_trades.append(
            {
                "input_id": direct_input_id,
                "entry_block_id": "holding",
                "entry_block_name": "Holding",
                "exit_block_id": None,
                "exit_block_name": None,
                "open_bar": 0,
                "close_bar": None,
                "direction": "long" if leg_fraction >= 0 else "short",
                "signed_weight": leg_fraction,
                "holding_id": label,
                "holding_name": label,
            }
        )

    aggregated_trades.sort(
        key=lambda t: (t["open_bar"], t["entry_block_id"])
    )

    # Build top-level positions payload (matches signals response shape).
    # First leg that references a given input_id wins; downstream conflicts
    # (same input_id, different prices across legs) are not expected and
    # would surface here.
    aggregated_positions: list[dict] = []
    seen_inputs: set[str] = set()
    for label, pos_list in signal_positions_map.items():
        sig_idx = signal_dates_map[label]
        # Projection from common_dates onto signal-bar indices: -1 marks
        # portfolio bars where the signal has no data (rendered as null).
        sig_index_of_date: dict[int, int] = {
            int(d): j for j, d in enumerate(sig_idx)
        }
        proj = [sig_index_of_date.get(int(d), -1) for d in common_dates]
        for pos in pos_list:
            iid = pos["input_id"]
            if iid in seen_inputs:
                continue
            seen_inputs.add(iid)
            price = pos.get("price")
            if price is None:
                aggregated_positions.append({"input_id": iid, "price": None})
                continue
            src_values = price["values"]
            remapped: list[float | None] = [
                (src_values[j] if j >= 0 else None) for j in proj
            ]
            aggregated_positions.append(
                {
                    "input_id": iid,
                    "price": {"label": price["label"], "values": remapped},
                }
            )

    # Direct (non-signal) leg price series → positions[]. Reuse the already-
    # aligned closes (length == len(common_dates)); first-leg-wins dedup.
    for label, leg in body.legs.items():
        if leg.type == "signal":
            continue
        if label not in aligned_closes:
            continue
        if leg.type == "instrument":
            direct_input_id = leg.symbol or label
            price_label = f"{leg.symbol}.close" if leg.symbol else f"{label}.close"
        elif leg.type == "continuous":
            direct_input_id = leg.collection or label
            price_label = f"{leg.collection}.close" if leg.collection else f"{label}.close"
        else:
            direct_input_id = label
            price_label = f"{label}.close"
        if direct_input_id in seen_inputs:
            continue
        seen_inputs.add(direct_input_id)
        aggregated_positions.append(
            {
                "input_id": direct_input_id,
                "price": {
                    "label": price_label,
                    "values": nan_safe_floats(aligned_closes[label]),
                },
            }
        )

    # ── 11. Build response ──

    dates_iso = [int_to_iso(int(d)) for d in common_dates]

    return {
        "dates": dates_iso,
        "portfolio_equity": result.portfolio_equity.tolist(),
        "leg_equities": {
            label: eq.tolist() for label, eq in result.per_leg_equities.items()
        },
        "raw_leg_equities": {
            label: eq.tolist() for label, eq in result.raw_leg_equities.items()
        },
        "rebalance_dates": [int_to_iso(int(d)) for d in result.rebalance_dates],
        "metrics": asdict(metrics),
        "leg_metrics": {label: asdict(m) for label, m in leg_metrics.items()},
        "monthly_returns": monthly,
        "yearly_returns": yearly,
        "date_range": {"start": dates_iso[0], "end": dates_iso[-1]},
        "full_date_range": {"start": full_start_iso, "end": full_end_iso},
        "rebalance": rebalance_freq.value,
        "return_type": body.return_type,
        "tracking_series": tracking_series,
        "trades": aggregated_trades,
        "positions": aggregated_positions,
    }
