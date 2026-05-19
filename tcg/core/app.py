"""FastAPI application factory -- composition root for the TCG platform."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.core.api.data import router as data_router
from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.indicators import router as indicators_router
from tcg.core.api.options import router as options_router
from tcg.core.api.persistence import router as persistence_router
from tcg.core.api.portfolio import router as portfolio_router
from tcg.core.api.signals import router as signals_router
from tcg.core.api.statistics import router as statistics_router
from tcg.core.config import load_config
from tcg.data import create_services
from tcg.types.errors import TCGError


def _cors_origins() -> list[str]:
    """Resolve CORS origins from env. Default to the Vite dev server only.

    The endpoints execute user-supplied Python (/api/indicators/compute,
    signals) — an ``allow_origins=["*"]`` middleware would let any site a
    browser visits reach them. Operators that actually want the old
    behaviour can set ``TCG_CORS_ORIGINS=*`` explicitly.
    """
    raw = os.environ.get("TCG_CORS_ORIGINS", "http://localhost:5173")
    return [o.strip() for o in raw.split(",") if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect to MongoDB, build services. Shutdown: close client."""
    config = load_config()
    client = AsyncIOMotorClient(
        config.uri,
        serverSelectionTimeoutMS=30_000,
        connectTimeoutMS=60_000,
        socketTimeoutMS=300_000,
        maxPoolSize=20,
    )
    db = client[config.db_name]
    services = await create_services(db)
    app.state.market_data = services["market_data"]
    yield
    client.close()


async def _request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Project envelope for Pydantic body/query validation failures.

    FastAPI's default 422 payload (``{"detail": [...]}``) breaks the
    frontend, which reads ``body.message``. Map to the same shape
    ``ValidationError`` uses so callers see a unified error contract.
    """
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = first.get("msg", "invalid request body")
        message = f"{loc}: {msg}" if loc else msg
    else:
        message = "invalid request body"
    return JSONResponse(
        status_code=400,
        content={"error_type": "validation_error", "message": message},
    )


def create_app() -> FastAPI:
    """Build the FastAPI application with all routers and middleware."""
    app = FastAPI(title="TCG Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)
    app.include_router(data_router)
    app.include_router(portfolio_router)
    app.include_router(indicators_router)
    app.include_router(signals_router)
    app.include_router(options_router)
    app.include_router(persistence_router)
    app.include_router(statistics_router)
    return app


app = create_app()
