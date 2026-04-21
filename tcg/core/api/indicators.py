"""Indicators router — run user-defined Python indicators on price series.

Exposes:

* ``POST /api/indicators/compute`` — execute a user-supplied ``compute``
  function against one or more aligned price time series.

In-memory only: indicator definitions are NOT persisted.

Instrument discovery (including the default S&P 500 spot index the
frontend preselects) is delegated to the existing ``/api/data/*``
endpoints — the same path the Data page uses — so we avoid inventing a
second, divergent discovery code path here.
"""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models import (
    ContinuousInstrumentRef,
    SeriesRef,
    SpotInstrumentRef,
)
from tcg.core.api._serializers import nan_safe_floats
from tcg.core.api.common import ADJUSTMENT_MAP, error_response, get_market_data
from tcg.data._utils import int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.engine.indicator_exec import (
    IndicatorRuntimeError,
    IndicatorValidationError,
    run_indicator,
)
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import ContinuousRollConfig, RollStrategy

router = APIRouter(prefix="/api/indicators", tags=["indicators"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class IndicatorComputeRequest(BaseModel):
    code: str
    params: dict[str, int | float | bool] = {}
    # Label → series ref. The key is the user-chosen label that the
    # indicator code accesses as ``series['price']`` etc.
    series: dict[str, SeriesRef]
    start: str | None = None
    end: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/compute")
async def compute_indicator(
    body: IndicatorComputeRequest,
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Execute a user-defined indicator against one or more price series."""

    # ── 1. Basic request validation ──

    if not body.series:
        return error_response(
            "validation", "'series' must contain at least one entry"
        )

    try:
        start_date, end_date = parse_iso_range(body.start, body.end)
    except ValueError as exc:
        return error_response("validation", str(exc))

    # Param validation (pydantic accepts int/float/bool; we still guard NaN
    # for the numeric path and forward bools unchanged).
    params: dict[str, int | float | bool] = {}
    for name, value in body.params.items():
        if isinstance(value, bool):
            params[name] = value
            continue
        if not isinstance(value, (int, float)):
            return error_response(
                "validation",
                (
                    f"param {name!r} must be numeric or bool, got "
                    f"{type(value).__name__}"
                ),
            )
        fvalue = float(value)
        if fvalue != fvalue:  # NaN guard
            return error_response(
                "validation", f"param {name!r} must not be NaN"
            )
        # Preserve int vs float so the sandbox can type-check properly.
        params[name] = int(value) if isinstance(value, int) else fvalue

    # ── 2. Fetch each labeled series ──

    # Preserve the user's insertion order so the response matches the
    # request — Python dicts preserve insertion order (3.7+).
    fetched: list[
        tuple[str, SpotInstrumentRef | ContinuousInstrumentRef, np.ndarray, np.ndarray]
    ] = []
    for label, ref in body.series.items():
        try:
            match ref.type:
                case "spot":
                    series = await svc.get_prices(
                        ref.collection,
                        ref.instrument_id,
                        start=start_date,
                        end=end_date,
                    )
                    if series is None:
                        return error_response(
                            "data",
                            (
                                f"Series label {label!r}: instrument "
                                f"'{ref.instrument_id}' not found in "
                                f"collection '{ref.collection}'"
                            ),
                        )
                    dates, closes = series.dates, series.close

                case "continuous":
                    adj = ADJUSTMENT_MAP.get(ref.adjustment)
                    if adj is None:
                        return error_response(
                            "validation",
                            (
                                f"Series label {label!r}: unknown adjustment "
                                f"method {ref.adjustment!r}"
                            ),
                        )
                    roll_config = ContinuousRollConfig(
                        strategy=RollStrategy.FRONT_MONTH,
                        adjustment=adj,
                        cycle=ref.cycle or None,
                        roll_offset_days=int(ref.rollOffset),
                    )
                    cseries = await svc.get_continuous(
                        ref.collection,
                        roll_config,
                        start=start_date,
                        end=end_date,
                    )
                    if cseries is None:
                        return error_response(
                            "data",
                            (
                                f"Series label {label!r}: continuous series "
                                f"unavailable for collection "
                                f"'{ref.collection}'"
                            ),
                        )
                    dates, closes = cseries.prices.dates, cseries.prices.close

                case _:
                    return error_response(
                        "validation",
                        f"Series label {label!r}: unhandled series type {ref.type!r}",
                    )

        except DataNotFoundError as exc:
            return error_response(
                "data", f"Series label {label!r}: {exc}"
            )
        # Reject malformed series up front: each series' dates must be
        # strictly monotonically increasing (no duplicates, no unsorted
        # input). Otherwise ``np.intersect1d`` + ``np.isin`` alignment
        # below silently produces differing lengths, which later fails
        # with a confusing sandbox-level error.
        if dates.size >= 2 and not bool(
            np.all(np.diff(dates) > 0)
        ):
            return error_response(
                "validation",
                f"Series {label!r} has non-monotonic or duplicate dates",
            )
        fetched.append((label, ref, dates, closes))

    # ── 3. Inner-join on the intersection of dates ──

    common_dates = fetched[0][2]
    for _label, _ref, dates, _close in fetched[1:]:
        common_dates = np.intersect1d(common_dates, dates, assume_unique=False)

    if common_dates.size == 0:
        return error_response(
            "validation", "No overlapping dates across requested series"
        )

    aligned_closes: dict[str, np.ndarray] = {}
    series_response: list[dict] = []
    for label, ref, dates, closes in fetched:
        mask = np.isin(dates, common_dates)
        aligned_dates = dates[mask]
        aligned = closes[mask]
        # Sort each series by date so alignment order matches common_dates.
        order = np.argsort(aligned_dates)
        aligned_closes_sorted = aligned[order].astype(np.float64, copy=False)
        aligned_closes[label] = aligned_closes_sorted
        close_list = nan_safe_floats(aligned_closes_sorted)
        # Build the response entry — shape differs by instrument type so
        # the frontend can reconstruct the ref for follow-up requests.
        entry: dict = {"label": label, "close": close_list}
        match ref.type:
            case "spot":
                entry["type"] = "spot"
                entry["collection"] = ref.collection
                entry["instrument_id"] = ref.instrument_id
            case "continuous":
                entry["type"] = "continuous"
                entry["collection"] = ref.collection
                entry["adjustment"] = ref.adjustment
                entry["cycle"] = ref.cycle
                entry["rollOffset"] = ref.rollOffset
                entry["strategy"] = ref.strategy
            case _:
                return error_response(
                    "validation",
                    f"Series label {label!r}: unhandled series type {ref.type!r} in response builder",
                    status=500,
                )
        series_response.append(entry)

    # Also sort the common_dates array ascending for the response.
    common_dates_sorted = np.sort(common_dates)

    # ── 4. Run the indicator in the sandbox ──
    #
    # run_indicator is synchronous and uses SIGALRM for the wall-clock
    # timeout — SIGALRM only fires on the main thread, so we MUST call
    # it inline rather than offloading to a worker thread (doing so
    # silently disables the timeout; see PR #12 round-2 review). The
    # event loop stalls for up to TIMEOUT_SECONDS, which is acceptable
    # for this trusted single-user deploy.
    try:
        indicator = run_indicator(body.code, params, aligned_closes)
    except IndicatorValidationError as exc:
        return error_response("validation", str(exc))
    except IndicatorRuntimeError as exc:
        return error_response(
            "runtime", str(exc), traceback=exc.user_traceback or None
        )

    # ── 5. Build response ──

    dates_iso = [int_to_iso(int(d)) for d in common_dates_sorted]
    indicator_list = nan_safe_floats(indicator)

    return {
        "dates": dates_iso,
        "series": series_response,
        "indicator": indicator_list,
    }
