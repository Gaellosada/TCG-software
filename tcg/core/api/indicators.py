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

from datetime import date

import numpy as np
import pandas_market_calendars as mcal
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tcg.core.api._adapters import build_roll_config
from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models import (
    ContinuousInstrumentRef,
    OptionStreamRef,
    SeriesRef,
    SpotInstrumentRef,
)
from tcg.core.api._options_wiring import build_stream_resolver_wiring
from tcg.core.api._serializers import nan_safe_floats
from tcg.core.api.common import error_response, get_market_data
from tcg.core.api.options import (
    _criterion_pydantic_to_dataclass,
    _maturity_pydantic_to_dataclass,
)
from tcg.data._utils import date_to_int, int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.engine.indicator_exec import (
    IndicatorRuntimeError,
    IndicatorValidationError,
    run_indicator,
)
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.errors import DataNotFoundError

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
    # Asset-type compatibility guard (Wave 2b). Both fields are optional —
    # the check only fires when BOTH are populated. ``str``-typed at the
    # request boundary so legacy / free-form clients aren't rejected by
    # Pydantic before the route handler can return a structured 422.
    # The route handler validates ``asset_type`` against the canonical
    # ``ASSET_TYPES`` set (from ``tcg.core.indicators.asset_types``).
    asset_type: str | None = None
    compatible_asset_types: list[str] | None = None
    # Optional indicator id, echoed into the structured 422 body when the
    # compat check rejects. Front-end may omit it for ad-hoc / custom
    # indicators; in that case it is omitted from the error body too.
    indicator_id: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _incompatible_asset_response(
    *,
    indicator_id: str | None,
    asset_type: str,
    accepted_asset_types: list[str],
) -> JSONResponse:
    """Build the structured 422 response for asset-type compat failures.

    Body shape (FROZEN contract — frontend ``errorTaxonomy`` routes on
    ``error_code``):

        {
            "error_code": "INDICATOR_INCOMPATIBLE_ASSET",
            "indicator_id": <str>?,   # omitted when not provided by client
            "asset_type": <str>,
            "accepted_asset_types": [<str>, ...],
        }

    Status: HTTP 422 (Unprocessable Entity). ``error_response`` is not
    used because its envelope is ``{error_type, message}`` and grafting
    a structured payload onto it would diverge from the existing
    convention. A focused helper keeps the route handler readable.
    """
    content: dict = {
        "error_code": "INDICATOR_INCOMPATIBLE_ASSET",
        "asset_type": asset_type,
        "accepted_asset_types": accepted_asset_types,
    }
    if indicator_id is not None:
        content["indicator_id"] = indicator_id
    return JSONResponse(status_code=422, content=content)


# Greek streams that require the root's provider to surface stored
# greeks.  ``iv`` and ``delta`` are also greek-derived but every
# Phase-1 root with options data has them — the brief lists only
# gamma/vega/theta as gated by ``has_greeks``.  Centralised so a
# future stream addition touches one place.
_GREEKS_GATED_STREAMS: frozenset[str] = frozenset({"gamma", "vega", "theta"})


def _tautological_option_stream_response(
    *,
    indicator_id: str | None,
    label: str,
) -> JSONResponse:
    """422 for the v1 tautology rule.

    ``selection.kind == 'by_delta'`` combined with ``stream == 'delta'``
    is rejected — picking a contract by its delta and then reading that
    very same delta back as the stream value is a fixed point of the
    selection criterion; it produces a constant time series equal to
    the target delta plus selection slack.  The frontend should use
    a different selection criterion (e.g. ``ByMoneyness``) when the
    indicator actually wants delta as a stream.
    """
    content: dict = {
        "error_code": "TAUTOLOGICAL_OPTION_STREAM",
        "asset_type": "option",
        "accepted_asset_types": ["option"],
        "detail": (
            "selection=by_delta + stream='delta' is tautological "
            f"(label={label!r})"
        ),
    }
    if indicator_id is not None:
        content["indicator_id"] = indicator_id
    return JSONResponse(status_code=422, content=content)


def _stream_unavailable_for_root_response(
    *,
    indicator_id: str | None,
    root: str,
    stream: str,
    unavailable_streams: list[str],
) -> JSONResponse:
    """422 for streams not surfaced by the chosen root's provider.

    The provider for some roots does not store eod greeks (notably
    OPT_VIX, OPT_ETH).  Asking for ``gamma`` / ``vega`` / ``theta`` on
    such a root is a guaranteed all-NaN — we reject upfront with a
    typed error_code so the frontend can swap streams without a
    round-trip of all-empty results.
    """
    content: dict = {
        "error_code": "STREAM_UNAVAILABLE_FOR_ROOT",
        "asset_type": "option",
        "root": root,
        "stream": stream,
        "unavailable_streams": unavailable_streams,
    }
    if indicator_id is not None:
        content["indicator_id"] = indicator_id
    return JSONResponse(status_code=422, content=content)


def _business_dates_in_range(
    start: date | None, end: date | None
) -> list[date] | None:
    """Enumerate CME business days in [start, end].

    ``OptionStreamRef`` materialisation needs an explicit date axis
    (no underlying price series in the request to borrow it from).
    We enumerate business days on the same calendar Module 4 uses
    (``CME_TradeDate``).  ``None`` is returned when the range is
    invalid or empty — the caller surfaces a 400 in that case.
    """
    if start is None or end is None or start > end:
        return None
    cal = mcal.get_calendar("CME_TradeDate")
    vd = cal.valid_days(start_date=start, end_date=end)
    return [ts.date() for ts in vd]


async def _materialise_option_stream(
    ref: OptionStreamRef,
    *,
    svc: MarketDataService,
    start_date: date | None,
    end_date: date | None,
) -> tuple[np.ndarray, np.ndarray, list[str | None]] | str:
    """Materialise an ``OptionStreamRef`` into ``(dates, values, diagnostics)``.

    Returns the triple on success or a string error message (for the
    400 ``error_response``) when the date range is missing.  This
    keeps the route handler's ``case "option_stream":`` block tight
    — a guideline from the Wave 2a brief.
    """
    trade_dates = _business_dates_in_range(start_date, end_date)
    if not trade_dates:
        return "option_stream requires explicit ISO 'start' and 'end' dates"
    chain_reader, mat_resolver, ul_resolver = build_stream_resolver_wiring(svc)
    values, diagnostics = await resolve_option_stream(
        dates=trade_dates,
        collection=ref.collection,
        option_type=ref.option_type,
        cycle=ref.cycle,
        maturity=_maturity_pydantic_to_dataclass(ref.maturity),
        selection=_criterion_pydantic_to_dataclass(ref.selection),
        stream=ref.stream,
        chain_reader=chain_reader,
        maturity_resolver=mat_resolver,
        underlying_price_resolver=ul_resolver,
    )
    dates_arr = np.array([date_to_int(d) for d in trade_dates], dtype=np.int64)
    return dates_arr, values, diagnostics


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

    # Asset-type compatibility guard. Asymmetric design: enforced on the
    # backend (canonical), advisory on the frontend (UX). Only fires when
    # BOTH ``asset_type`` and ``compatible_asset_types`` are populated;
    # otherwise the request is treated as legacy and proceeds.
    if (
        body.asset_type is not None
        and body.compatible_asset_types is not None
        and body.asset_type not in body.compatible_asset_types
    ):
        return _incompatible_asset_response(
            indicator_id=body.indicator_id,
            asset_type=body.asset_type,
            accepted_asset_types=list(body.compatible_asset_types),
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
        tuple[
            str,
            SpotInstrumentRef | ContinuousInstrumentRef | OptionStreamRef,
            np.ndarray,
            np.ndarray,
            list[str | None] | None,
        ]
    ] = []
    # Pre-flight validation for option_stream variants — both rules emit a
    # typed 422 BEFORE we touch the database, matching
    # ``_incompatible_asset_response`` precedent.
    cached_root_metadata: dict[str, object] | None = None
    for label, ref in body.series.items():
        if ref.type != "option_stream":
            continue
        # Rule 1 — tautological by_delta + stream='delta'.
        if (
            getattr(ref.selection, "kind", None) == "by_delta"
            and ref.stream == "delta"
        ):
            return _tautological_option_stream_response(
                indicator_id=body.indicator_id, label=label
            )
        # Rule 2 — gamma/vega/theta on a no-greeks root.  Cache the
        # root list across labels in this request.
        if ref.stream in _GREEKS_GATED_STREAMS:
            if cached_root_metadata is None:
                roots = await svc.list_option_roots()
                cached_root_metadata = {r.collection: r for r in roots}
            root_info = cached_root_metadata.get(ref.collection)
            if root_info is not None and not getattr(
                root_info, "has_greeks", True
            ):
                return _stream_unavailable_for_root_response(
                    indicator_id=body.indicator_id,
                    root=ref.collection,
                    stream=ref.stream,
                    unavailable_streams=sorted(_GREEKS_GATED_STREAMS),
                )

    for label, ref in body.series.items():
        diagnostics: list[str | None] | None = None
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
                    try:
                        roll_config = build_roll_config(
                            ref.adjustment, ref.cycle, ref.rollOffset
                        )
                    except ValueError as exc:
                        return error_response(
                            "validation",
                            f"Series label {label!r}: {exc}",
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

                case "option_stream":
                    materialised = await _materialise_option_stream(
                        ref, svc=svc, start_date=start_date, end_date=end_date,
                    )
                    if isinstance(materialised, str):
                        return error_response(
                            "validation", f"Series label {label!r}: {materialised}"
                        )
                    dates, closes, diagnostics = materialised

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
        fetched.append((label, ref, dates, closes, diagnostics))

    # ── 3. Inner-join on the intersection of dates ──

    common_dates = fetched[0][2]
    for _label, _ref, dates, _close, _diag in fetched[1:]:
        common_dates = np.intersect1d(common_dates, dates, assume_unique=False)

    if common_dates.size == 0:
        return error_response(
            "validation", "No overlapping dates across requested series"
        )

    aligned_closes: dict[str, np.ndarray] = {}
    series_response: list[dict] = []
    for label, ref, dates, closes, diagnostics in fetched:
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
            case "option_stream":
                entry["type"] = "option_stream"
                entry["collection"] = ref.collection
                entry["option_type"] = ref.option_type
                entry["cycle"] = ref.cycle
                entry["stream"] = ref.stream
                if diagnostics is not None:
                    # Align diagnostics with common_dates the same way as values.
                    diag_arr = np.array(
                        [d if d is not None else "" for d in diagnostics],
                        dtype=object,
                    )
                    aligned_diag = diag_arr[mask][order].tolist()
                    entry["diagnostics"] = [
                        d if d != "" else None for d in aligned_diag
                    ]
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
