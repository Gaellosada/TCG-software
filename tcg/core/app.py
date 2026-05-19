"""FastAPI application factory -- composition root for the TCG platform."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pymongo.errors
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


# Hard cap on inbound request body size. MongoDB rejects documents
# larger than 16 MB with ``DocumentTooLarge``; we cut the request off
# at 4 MB so a buggy or malicious client sees a clean 413 long before
# the request reaches the persistence layer. The cap applies to the
# whole application because the persistence router has no exclusive
# host header — keeping it global is the safer default.
_MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024  # 4 MB


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


class BodySizeLimitMiddleware:
    """Pure-ASGI middleware that caps inbound request body size at
    ``_MAX_REQUEST_BODY_BYTES`` regardless of transport.

    Why pure ASGI and not ``app.middleware("http")``?
    -------------------------------------------------
    The previous implementation read ``Content-Length`` from the
    headers and let the request through when the header was absent.
    That made the cap trivially bypassable by:

    1. HTTP/1.1 ``Transfer-Encoding: chunked`` requests — no
       ``Content-Length`` header is sent.
    2. HTTP/2 framed bodies — likewise no ``Content-Length`` per se.
    3. Clients that simply lie in the header (``Content-Length: 0``
       followed by a multi-MB chunked body) — well-behaved servers
       reject this but the previous middleware would have let the
       header-passing request through and then buffered the body.

    The bypass meant a malicious client could send hundreds of MB to
    the persistence endpoints; FastAPI would buffer the whole body
    before the route ran, and only the downstream MongoDB
    ``DocumentTooLarge`` catch would surface a 413 — *after* memory
    was already spent.

    This middleware closes that gap by wrapping the ASGI ``receive``
    callable and tallying bytes as they arrive. When the running sum
    exceeds the cap, it raises an internal :class:`_BodyTooLarge` and
    short-circuits the response with 413 before the downstream app
    sees any further chunks. Combined with the unchanged
    ``Content-Length`` fast-path precheck, well-behaved clients still
    get the cheap-and-early rejection, and chunked / HTTP/2 / lying
    clients can no longer exhaust memory.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # --- Fast path: trust Content-Length when present. -----------------
        headers = dict(scope.get("headers", []))
        cl_raw = headers.get(b"content-length")
        if cl_raw is not None:
            try:
                cl = int(cl_raw)
            except ValueError:
                cl = -1  # malformed — fall through to streaming guard
            if cl > _MAX_REQUEST_BODY_BYTES:
                await _send_too_large(send, cl)
                return

        # --- Streaming guard: tally bytes as they arrive. ------------------
        # We can't raise an exception out of ``receive_with_limit`` to
        # signal overflow because Starlette's ServerErrorMiddleware
        # (which sits between us and the routes) catches every
        # exception and returns 500. Instead, on overflow we:
        #   1. Set the overflow flag.
        #   2. Return a synthetic ``http.disconnect`` to the downstream
        #      app so it stops waiting for more body and exits cleanly.
        #   3. Swallow any response the app emits via the wrapped send.
        #   4. After ``self.app(...)`` returns, emit our own 413.
        # Box the mutable state in lists so the nested closures can
        # update them without explicit ``nonlocal``.
        observed = [0]
        overflowed = [False]
        response_started = [False]

        async def receive_with_limit() -> Message:
            # Once overflow has been signalled, every subsequent receive
            # returns a disconnect so the downstream app unblocks and
            # exits without trying to read more body.
            if overflowed[0]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                if chunk:
                    observed[0] += len(chunk)
                if observed[0] > _MAX_REQUEST_BODY_BYTES:
                    overflowed[0] = True
                    # Swap this chunk for a disconnect so the app
                    # doesn't keep waiting for more bytes.
                    return {"type": "http.disconnect"}
            return message

        async def send_with_guard(message: Message) -> None:
            # Once overflow has been detected, swallow any response
            # the downstream app emits — we will send our own 413
            # after ``self.app(...)`` returns.
            if overflowed[0]:
                return
            if message["type"] == "http.response.start":
                response_started[0] = True
            await send(message)

        await self.app(scope, receive_with_limit, send_with_guard)
        if overflowed[0]:
            # Only safe to send if the app hadn't already started its
            # response BEFORE we tripped overflow. Under normal flow
            # the app is still waiting on the body when overflow
            # fires, so response_started is False here.
            if not response_started[0]:
                await _send_too_large(send, observed[0])


async def _send_too_large(send: Send, size: int) -> None:
    """Emit a 413 JSON response via the raw ASGI ``send`` callable."""
    body = (
        b'{"error_type":"request_too_large","message":'
        b'"request body size ' + str(size).encode("ascii")
        + b" exceeds limit " + str(_MAX_REQUEST_BODY_BYTES).encode("ascii")
        + b'"}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _pymongo_error_handler(
    request: Request, exc: pymongo.errors.PyMongoError
) -> JSONResponse:
    """Catch-all for unhandled PyMongo errors → 503.

    Routes that care about specific PyMongo failure modes (e.g.
    ``DuplicateKeyError`` → 409 on create) catch them locally and
    raise ``HTTPException`` BEFORE the exception reaches this handler.
    What's left here are the unexpected ones — network blips, replica-
    set elections, auth/role errors, server-selection timeouts. None
    of those is the caller's fault, so we surface them as 503 Service
    Unavailable with a sanitized envelope (we deliberately do NOT
    include the PyMongo exception message in the response body — it
    can leak topology / IPs / credential hints).
    """
    # Log the real error server-side for ops; keep the response body
    # sterile. ``%r`` so the exception type is recoverable from logs.
    import logging

    logging.getLogger(__name__).warning(
        "persistence unavailable: %r (path=%s)", exc, request.url.path
    )
    return JSONResponse(
        status_code=503,
        content={
            "error_type": "persistence_unavailable",
            "message": "persistence layer is temporarily unavailable",
        },
    )


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
    # NOTE: middleware is applied in reverse registration order. The
    # body-size guard is registered FIRST so it runs LAST in the
    # outbound chain (i.e. it sits CLOSEST to the routes, with CORS
    # wrapping it). That order means well-formed CORS preflights are
    # answered before the size guard inspects them, and CORS headers
    # are added to the 413 on its way back out.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)
    # Catch-all for unhandled PyMongo errors — see ``_pymongo_error_handler``.
    # Routes that need a specific status (409 on duplicate, 413 on too-
    # large, 409 on CAS miss) catch the relevant subclass locally and
    # raise HTTPException, so those paths bypass this handler.
    app.add_exception_handler(
        pymongo.errors.PyMongoError, _pymongo_error_handler
    )
    app.include_router(data_router)
    app.include_router(portfolio_router)
    app.include_router(indicators_router)
    app.include_router(signals_router)
    app.include_router(options_router)
    app.include_router(persistence_router)
    app.include_router(statistics_router)
    return app


app = create_app()
