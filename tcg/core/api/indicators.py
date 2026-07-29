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
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tcg.core.api._adapters import build_roll_config
from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models_options import GREEKS_GATED_STREAMS
from tcg.core.api._models import (
    ContinuousInstrumentRef,
    OptionStreamRef,
    SeriesRef,
    SpotInstrumentRef,
)
from tcg.core.api._options_materialise import (
    _business_dates_in_range,
    _materialise_option_stream,
)
from tcg.core.api._serializers import nan_safe_floats
from tcg.core.api._v2_preconditions import (
    check_v2_option_coverage_floor,
    check_v2_preconditions,
    collect_v2_option_roots,
)
from tcg.core.api.common import (
    DataSource,
    effective_data_source,
    error_response,
    get_market_data_for,
    progress_clear,
    progress_register,
    progress_snapshot,
    progress_tick,
)
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


class IndicatorComputeRequest(BaseModel):
    code: str
    params: dict[str, int | float | bool] = {}
    # Label → series ref. The key is the user-chosen label that the
    # indicator code accesses as ``series['price']`` etc.
    series: dict[str, SeriesRef]
    start: str | None = None
    end: str | None = None
    # Inherited-default warehouse for this compute (per-instrument selection):
    # each series ref carries its OWN ``data_source`` and falls back to this
    # per-run value when it omitted the field. "v1" (DEFAULT) = the frozen
    # reference; indicators gain v2 support here for the first time.
    data_source: DataSource = "v1"
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
    # Optional task id for progress polling (UUID-shaped string, but the
    # route doesn't enforce a format — just a unique key the frontend
    # uses to poll ``GET /api/indicators/progress/{task_id}`` while the
    # compute is running). Only populated when the request involves an
    # ``option_stream`` ref (the per-date materialiser is the slow path
    # progress reports on); the value is ignored otherwise.
    task_id: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/progress/{task_id}")
async def get_compute_progress(task_id: str) -> dict:
    """Read the in-progress compute counter for a task.

    Returns ``{"done": int, "total": int, "fraction": float}`` where
    ``fraction = done / total`` (clamped to [0, 1], ``0.0`` when the
    entry is missing). The frontend polls this endpoint while waiting
    on ``POST /compute`` so the user can watch the per-date
    materialiser progress instead of staring at a static spinner.

    Response shape is intentionally permissive (no 404 on missing task)
    so the FE can poll without race conditions: a poll that fires
    *before* the route handler registers the entry simply sees zeros.

    Delegates to ``common.progress_snapshot`` (shared state with
    options router).
    """
    return progress_snapshot(task_id)


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


# GREEKS_GATED_STREAMS is imported from _models_options (shared with
# options.py).  Local alias keeps call sites short + unchanged.
_GREEKS_GATED_STREAMS = GREEKS_GATED_STREAMS


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
            f"selection=by_delta + stream='delta' is tautological (label={label!r})"
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


def _count_option_stream_dates(
    series: dict[str, SeriesRef],
    *,
    start_date: date | None,
    end_date: date | None,
) -> int:
    """Pre-compute the total per-date work units across every
    ``option_stream`` ref in ``series`` so the FE-visible progress
    fraction has a real denominator. Non-option-stream refs contribute
    zero — their wall-clock cost is dominated by a single MongoDB read
    that the user should not see ticking on a per-date basis."""
    n = len(_business_dates_in_range(start_date, end_date) or [])
    return n * sum(1 for ref in series.values() if isinstance(ref, OptionStreamRef))


@router.post("/compute")
async def compute_indicator(
    body: IndicatorComputeRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict:
    """Execute a user-defined indicator against one or more price series.

    Per-instrument v1/v2 selection: each series ref reads from the warehouse
    matching its own ``data_source`` (inheriting ``body.data_source`` when
    omitted). ``svc_for`` maps a concrete source → service; ``inst_svc_for``
    bakes the body default into a ref-source→service map. An all-v1 compute
    resolves every ref to the same object the old ``Depends(get_market_data)``
    gave (Sign 1).
    """

    def svc_for(source: DataSource) -> MarketDataService:
        return get_market_data_for(request, source)

    def inst_svc_for(ref: SeriesRef) -> MarketDataService:
        return svc_for(
            effective_data_source(getattr(ref, "data_source", None), body.data_source)
        )

    # ── 1. Basic request validation ──

    if not body.series:
        return error_response("validation", "'series' must contain at least one entry")

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

    # v2 boundary preconditions — indicators' FIRST v2 support. Per-node: gates
    # only the v2 refs and no-ops for an all-v1 compute (Sign 1). Without this a
    # v2 ref the warehouse cannot serve dies as an unattributable all-NaN series
    # (the engine swallows the reader's typed error); check it here where the
    # message still names the offending series and the fix.
    payload = body.model_dump(mode="json")
    try:
        check_v2_preconditions(payload, data_source=body.data_source)
    except ValidationError as exc:
        return error_response("validation", str(exc))
    v2_option_roots = collect_v2_option_roots(payload, data_source=body.data_source)
    if v2_option_roots:
        try:
            await check_v2_option_coverage_floor(
                v2_option_roots, svc_for("v2"), start_date
            )
        except ValidationError as exc:
            return error_response("validation", str(exc))

    # Progress tracking: only register when the request involves an
    # option_stream ref (the slow path) AND the FE supplied a task_id.
    # The progress callback ticks once per resolved trade date; the FE
    # polls /progress/{task_id} while waiting on the main response.
    # Cleanup is queued as a BackgroundTask so it runs after response
    # send regardless of which early-return path the route takes.
    progress_task_id: str | None = None
    progress_callback = None
    if body.task_id:
        total = _count_option_stream_dates(
            body.series, start_date=start_date, end_date=end_date
        )
        if total > 0:
            progress_task_id = body.task_id
            progress_register(progress_task_id, total)

            def progress_callback(tid: str = progress_task_id) -> None:
                progress_tick(tid)

            background_tasks.add_task(progress_clear, progress_task_id)

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
                (f"param {name!r} must be numeric or bool, got {type(value).__name__}"),
            )
        fvalue = float(value)
        if fvalue != fvalue:  # NaN guard
            return error_response("validation", f"param {name!r} must not be NaN")
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
    # Root metadata is warehouse-specific, so cache it per RESOLVED source (a
    # mixed v1/v2 compute may need both) instead of a single slot.
    cached_root_metadata: dict[str, dict[str, object]] = {}
    for label, ref in body.series.items():
        if ref.type != "option_stream":
            continue
        # Rule 1 — tautological by_delta + stream='delta'.
        if getattr(ref.selection, "kind", None) == "by_delta" and ref.stream == "delta":
            return _tautological_option_stream_response(
                indicator_id=body.indicator_id, label=label
            )
        # Rule 2 — gamma/vega/theta on a no-greeks root.  Cache the
        # root list per source across labels in this request.
        if ref.stream in _GREEKS_GATED_STREAMS:
            ref_svc = inst_svc_for(ref)
            src_key = effective_data_source(
                getattr(ref, "data_source", None), body.data_source
            )
            if src_key not in cached_root_metadata:
                roots = await ref_svc.list_option_roots()
                cached_root_metadata[src_key] = {r.collection: r for r in roots}
            root_info = cached_root_metadata[src_key].get(ref.collection)
            if root_info is not None and not getattr(root_info, "has_greeks", True):
                return _stream_unavailable_for_root_response(
                    indicator_id=body.indicator_id,
                    root=ref.collection,
                    stream=ref.stream,
                    unavailable_streams=sorted(_GREEKS_GATED_STREAMS),
                )

    for label, ref in body.series.items():
        diagnostics: list[str | None] | None = None
        ref_svc = inst_svc_for(ref)
        try:
            match ref.type:
                case "spot":
                    series = await ref_svc.get_prices(
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
                            ref.adjustment,
                            ref.cycle,
                            ref.rollOffset,
                            strategy=ref.strategy,
                        )
                    except ValueError as exc:
                        return error_response(
                            "validation",
                            f"Series label {label!r}: {exc}",
                        )
                    cseries = await ref_svc.get_continuous(
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
                        ref,
                        svc=ref_svc,
                        start_date=start_date,
                        end_date=end_date,
                        progress_callback=progress_callback,
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
            return error_response("data", f"Series label {label!r}: {exc}")
        # Reject malformed series up front: each series' dates must be
        # strictly monotonically increasing (no duplicates, no unsorted
        # input). Otherwise ``np.intersect1d`` + ``np.isin`` alignment
        # below silently produces differing lengths, which later fails
        # with a confusing sandbox-level error.
        if dates.size >= 2 and not bool(np.all(np.diff(dates) > 0)):
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
        return error_response("runtime", str(exc), traceback=exc.user_traceback or None)

    # ── 5. Build response ──

    dates_iso = [int_to_iso(int(d)) for d in common_dates_sorted]
    indicator_list = nan_safe_floats(indicator)

    return {
        "dates": dates_iso,
        "series": series_response,
        "indicator": indicator_list,
    }


# Backward-compatible aliases — tests import these names from indicators.py.
_progress_register = progress_register
_progress_tick = progress_tick
_progress_clear = progress_clear
