"""FastAPI wiring for the persistence (write) layer.

The read-WRITE PostgreSQL pool is built once in
``tcg.core.app.lifespan`` and a single :class:`WriteRepository` bound to
it is stored on ``app.state.app_db_repo`` (mirroring how the market-data
service lives on ``app.state.market_data``). This dependency simply hands
that instance to the route — same pattern as ``common.get_market_data``.

Building the pool at startup (rather than lazily on first request) gives
fail-fast startup and a clean shutdown hook (the pool is closed in the
lifespan teardown), which the previous lazy Motor singleton lacked.
"""

from __future__ import annotations

from fastapi import Request

from tcg.persistence import WriteRepository


def get_write_repository(request: Request) -> WriteRepository:
    """FastAPI dependency: return the app-wide ``WriteRepository``.

    Reads the instance built in the lifespan and stored on
    ``request.app.state.app_db_repo``. Tests that exercise the HTTP
    surface override this dependency with an in-memory fake.
    """
    return request.app.state.app_db_repo


def reset_write_repository_singleton() -> None:
    """Test-only hook retained for API compatibility.

    The repository is now owned by the app lifespan (on ``app.state``)
    rather than a module-level singleton, so there is nothing to reset
    here. Kept as a no-op so existing test imports keep working.
    """
    return None


__all__ = ["get_write_repository", "reset_write_repository_singleton"]
