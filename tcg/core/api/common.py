"""Shared helpers for API routers.

Keeps the error-envelope shape and the frontend-string → domain-enum
mapping in a single place so the indicator and signal routers can't
drift apart.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from tcg.data.protocols import MarketDataService
from tcg.types.market import AdjustmentMethod


def get_market_data(request: Request) -> MarketDataService:
    """Dependency: retrieve the MarketDataService from app state."""
    return request.app.state.market_data


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
    "proportional": AdjustmentMethod.PROPORTIONAL,
    "difference": AdjustmentMethod.DIFFERENCE,
}
