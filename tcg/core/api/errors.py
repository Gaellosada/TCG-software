"""Exception handler for TCGError subclasses -- maps error_type to HTTP status."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from tcg.types.errors import TCGError

STATUS_MAP: dict[str, int] = {
    "data_not_found": 404,
    "data_access_error": 502,
    "strategy_execution_error": 422,
    "simulation_error": 500,
    "validation_error": 400,
}


async def tcg_error_handler(request: Request, exc: TCGError) -> JSONResponse:
    """Convert any TCGError into a structured JSON response."""
    return JSONResponse(
        status_code=STATUS_MAP.get(exc.error_type, 500),
        content={"error_type": exc.error_type, "message": exc.message},
    )
