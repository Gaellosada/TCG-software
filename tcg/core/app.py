"""FastAPI application factory -- composition root for the TCG platform."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.core.agent.workspace import AgentWorkspace
from tcg.core.api.agent import agent_websocket, router as agent_router
from tcg.core.api.data import router as data_router
from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.indicators import router as indicators_router
from tcg.core.api.options import router as options_router
from tcg.core.api.portfolio import router as portfolio_router
from tcg.core.api.signals import router as signals_router
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
    """Startup: connect to MongoDB, build services, init agent. Shutdown: close client."""
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

    # Store mongo connection info for agent tools
    app.state.mongo_uri = config.uri
    app.state.mongo_db_name = config.db_name

    # Agent workspace -- always created (session management works without CLI)
    app.state.agent_workspace = AgentWorkspace()

    yield
    client.close()


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
    app.include_router(data_router)
    app.include_router(portfolio_router)
    app.include_router(indicators_router)
    app.include_router(signals_router)
    app.include_router(options_router)
    app.include_router(agent_router)

    # Mount WebSocket outside the router to avoid prefix routing issues
    app.websocket("/ws/agent/{session_id}")(agent_websocket)

    return app


app = create_app()
