"""Shared helpers for API routers.

Keeps the error-envelope shape and the frontend-string → domain-enum
mapping in a single place so the indicator and signal routers can't
drift apart.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from tcg.data.protocols import MarketDataService, MarketDataServiceV2
from tcg.types.market import AdjustmentMethod


def get_market_data(request: Request) -> MarketDataService:
    """Dependency: retrieve the MarketDataService from app state."""
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
