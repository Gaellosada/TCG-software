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

import asyncio
from datetime import date

import numpy as np
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tcg.core.api.data import get_market_data
from tcg.data._utils import int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.engine.indicator_exec import (
    IndicatorRuntimeError,
    IndicatorValidationError,
    run_indicator,
)
from tcg.types.errors import DataNotFoundError, ValidationError

router = APIRouter(prefix="/api/indicators", tags=["indicators"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SeriesRef(BaseModel):
    collection: str
    instrument_id: str


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
        return JSONResponse(
            status_code=400,
            content={
                "error_type": "validation",
                "message": "'series' must contain at least one entry",
            },
        )

    try:
        start_date = date.fromisoformat(body.start) if body.start else None
        end_date = date.fromisoformat(body.end) if body.end else None
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error_type": "validation",
                "message": f"Invalid date format: {exc}",
            },
        )

    # Param validation (pydantic accepts int/float/bool; we still guard NaN
    # for the numeric path and forward bools unchanged).
    params: dict[str, int | float | bool] = {}
    for name, value in body.params.items():
        if isinstance(value, bool):
            params[name] = value
            continue
        if not isinstance(value, (int, float)):
            return JSONResponse(
                status_code=400,
                content={
                    "error_type": "validation",
                    "message": (
                        f"param {name!r} must be numeric or bool, got "
                        f"{type(value).__name__}"
                    ),
                },
            )
        fvalue = float(value)
        if fvalue != fvalue:  # NaN guard
            return JSONResponse(
                status_code=400,
                content={
                    "error_type": "validation",
                    "message": f"param {name!r} must not be NaN",
                },
            )
        # Preserve int vs float so the sandbox can type-check properly.
        params[name] = int(value) if isinstance(value, int) else fvalue

    # ── 2. Fetch each labeled series ──

    # Preserve the user's insertion order so the response matches the
    # request — Python dicts preserve insertion order (3.7+).
    fetched: list[tuple[str, SeriesRef, np.ndarray, np.ndarray]] = []
    for label, ref in body.series.items():
        try:
            series = await svc.get_prices(
                ref.collection,
                ref.instrument_id,
                start=start_date,
                end=end_date,
            )
        except DataNotFoundError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error_type": "data",
                    "message": f"Series label {label!r}: {exc}",
                },
            )
        if series is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error_type": "data",
                    "message": (
                        f"Series label {label!r}: instrument "
                        f"'{ref.instrument_id}' not found in collection "
                        f"'{ref.collection}'"
                    ),
                },
            )
        # Reject malformed series up front: each series' dates must be
        # strictly monotonically increasing (no duplicates, no unsorted
        # input). Otherwise ``np.intersect1d`` + ``np.isin`` alignment
        # below silently produces differing lengths, which later fails
        # with a confusing sandbox-level error.
        if series.dates.size >= 2 and not bool(
            np.all(np.diff(series.dates) > 0)
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "error_type": "validation",
                    "message": (
                        f"Series {label!r} has non-monotonic or duplicate dates"
                    ),
                },
            )
        fetched.append((label, ref, series.dates, series.close))

    # ── 3. Inner-join on the intersection of dates ──

    common_dates = fetched[0][2]
    for _label, _ref, dates, _close in fetched[1:]:
        common_dates = np.intersect1d(common_dates, dates, assume_unique=False)

    if common_dates.size == 0:
        return JSONResponse(
            status_code=400,
            content={
                "error_type": "validation",
                "message": "No overlapping dates across requested series",
            },
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
        # NaN-safe serialization: JSON ``NaN`` is not valid JSON per RFC
        # 8259. Map NaN → ``None`` the same way the indicator list does
        # below. This matches strict JSON parsers on the frontend.
        close_list: list[float | None] = [
            None if (v != v) else float(v)
            for v in aligned_closes_sorted.tolist()
        ]
        series_response.append(
            {
                "label": label,
                "collection": ref.collection,
                "instrument_id": ref.instrument_id,
                "close": close_list,
            }
        )

    # Also sort the common_dates array ascending for the response.
    common_dates_sorted = np.sort(common_dates)

    # ── 4. Run the indicator in the sandbox ──
    #
    # run_indicator is synchronous (uses SIGALRM) and may spend up to
    # TIMEOUT_SECONDS on CPU-bound user code.  Offload to a thread so
    # the event loop remains free for other requests during execution.
    try:
        indicator = await asyncio.to_thread(
            run_indicator, body.code, params, aligned_closes
        )
    except IndicatorValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error_type": "validation",
                "message": str(exc),
            },
        )
    except IndicatorRuntimeError as exc:
        content: dict = {
            "error_type": "runtime",
            "message": str(exc),
        }
        if exc.user_traceback:
            content["traceback"] = exc.user_traceback
        return JSONResponse(status_code=400, content=content)

    # ── 5. Build response ──

    dates_iso = [int_to_iso(int(d)) for d in common_dates_sorted]
    indicator_list: list[float | None] = [
        None if (v != v) else float(v)  # NaN → null
        for v in indicator.tolist()
    ]

    return {
        "dates": dates_iso,
        "series": series_response,
        "indicator": indicator_list,
    }
