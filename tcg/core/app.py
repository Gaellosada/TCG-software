"""FastAPI application factory -- composition root for the TCG platform."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.core.api.data import router as data_router
from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.indicators import router as indicators_router
from tcg.core.api.portfolio import router as portfolio_router
from tcg.core.config import load_config
from tcg.data import create_services
from tcg.types.errors import TCGError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect to MongoDB, build services. Shutdown: close client."""
    config = load_config()
    client = AsyncIOMotorClient(config.uri, serverSelectionTimeoutMS=5000)
    db = client[config.db_name]
    services = await create_services(db)
    app.state.market_data = services["market_data"]
    yield
    client.close()


def create_app() -> FastAPI:
    """Build the FastAPI application with all routers and middleware."""
    app = FastAPI(title="TCG Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Dev only
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(data_router)
    app.include_router(portfolio_router)
    app.include_router(indicators_router)
    return app


app = create_app()
