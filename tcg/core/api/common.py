"""Shared helpers for API routers.

Keeps the error-envelope shape and the frontend-string → domain-enum
mapping in a single place so the indicator and signal routers can't
drift apart.
"""

from __future__ import annotations

from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse

from tcg.data.protocols import MarketDataService, MarketDataServiceV2
from tcg.types.market import AdjustmentMethod

# The per-instrument warehouse selector carried on ref / leg objects (and, as an
# inherited default, on compute request bodies).
# ``"v1"`` = ``tcg_instruments`` (the frozen reference), ``"v2"`` =
# ``tcg_instruments_v2`` through the compat adapter. Declared here so the
# portfolio and signal request models cannot drift.
DataSource = Literal["v1", "v2"]


def effective_data_source(own: str | None, default: DataSource) -> DataSource:
    """Resolve the warehouse a ref actually reads from, per the frozen precedence.

    Precedence (highest first): the ref's OWN ``data_source`` (``"v1"``/``"v2"``);
    else the enclosing body's ``data_source`` (``default``); else — since the
    per-run field itself defaults to ``"v1"`` — the frozen reference. A ``None``
    (unset) or any non-literal value inherits ``default``, so a leaf that omitted
    the field on the wire (the emit-only-when-``"v2"`` rule) inherits the body's
    source. ``"v1"`` and ``"v2"`` on a ref win over the body default.
    """
    return own if own in ("v1", "v2") else default


def get_market_data(request: Request) -> MarketDataService:
    """Dependency: retrieve the MarketDataService from app state."""
    return request.app.state.market_data


def get_market_data_for(request: Request, source: DataSource) -> MarketDataService:
    """Return the ``MarketDataService`` a compute should bind for ``source``.

    Both returned objects satisfy the SAME protocol, so every layer below the
    route (``make_signal_fetcher``, ``materialise_option_streams``,
    ``build_stream_resolver_wiring``, the per-leg evaluators) takes the service
    as a plain argument and none of them re-reads ``app.state`` — swapping the
    bound object at the route therefore propagates for free.

    ``"v1"`` returns the exact object ``get_market_data`` would, so the default
    path is unchanged (guardrail Sign 1). Any other value than the two literals
    is impossible: the request models constrain it to ``DataSource``.
    """
    if source == "v2":
        return request.app.state.market_data_v2_compat
    return request.app.state.market_data


def get_market_data_v2(request: Request) -> MarketDataServiceV2:
    """Dependency: retrieve the v2 market-data service from app state.

    Typed against the ``MarketDataServiceV2`` protocol (a data-layer interface,
    not the concrete ``DefaultMarketDataServiceV2``) so this shared helper stays
    core→data and mirrors the v1 ``get_market_data`` symmetry.
    """
    return request.app.state.market_data_v2


def error_response(
    error_type: str,
    message: str,
    *,
    status: int = 400,
    traceback: str | None = None,
) -> JSONResponse:
    """Single source of truth for the error envelope shape.

    All error responses from compute-style routers share the same JSON body:
    ``{"error_type": str, "message": str, "traceback"?: str}``.
    """
    content: dict = {"error_type": error_type, "message": message}
    if traceback:
        content["traceback"] = traceback
    return JSONResponse(status_code=status, content=content)


ADJUSTMENT_MAP: dict[str, AdjustmentMethod] = {
    "none": AdjustmentMethod.NONE,
    "ratio": AdjustmentMethod.RATIO,
    "difference": AdjustmentMethod.DIFFERENCE,
}


# ---------------------------------------------------------------------------
# Shared progress tracking
#
# Both the indicators and options routers expose a ``/progress/{task_id}``
# polling endpoint for long-running materialisation tasks.  The state is
# stored in a single module-level dict keyed by task_id (UUID-shaped
# strings generated client-side, so no collision between routers).
# ---------------------------------------------------------------------------

_PROGRESS_STATE: dict[str, dict[str, int]] = {}


def progress_register(task_id: str, total: int) -> None:
    """Initialise a progress entry.

    Overwrites any prior entry with the same key (a stale carry-over from
    an aborted compute is the most likely cause; the new compute's total
    takes precedence).
    """
    _PROGRESS_STATE[task_id] = {"done": 0, "total": max(int(total), 0)}


def progress_tick(task_id: str) -> None:
    """Increment the done counter.

    No-op when the entry was already removed (e.g. the compute finished
    and cleaned up while a stray callback was still in flight).
    """
    entry = _PROGRESS_STATE.get(task_id)
    if entry is not None:
        entry["done"] += 1


def progress_clear(task_id: str) -> None:
    """Remove the entry.  Idempotent — safe to call in a finally block
    even when registration was skipped."""
    _PROGRESS_STATE.pop(task_id, None)


def progress_snapshot(task_id: str) -> dict:
    """Return ``{done, total, fraction}`` for a task.

    Returns zeros when the entry is missing — the FE can poll before
    registration without triggering a 404.
    """
    entry = _PROGRESS_STATE.get(task_id)
    if entry is None:
        return {"done": 0, "total": 0, "fraction": 0.0}
    done = entry["done"]
    total = entry["total"]
    fraction = (done / total) if total > 0 else 0.0
    if fraction > 1.0:
        fraction = 1.0
    return {"done": done, "total": total, "fraction": fraction}
