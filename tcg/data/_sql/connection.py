"""PostgreSQL dwh connection pool for read-only market data access.

Enforces read-only semantics: default_transaction_read_only=on (server-side),
conn.read_only=True (driver-side), and statement_timeout (per-query cap).
Credentials and connection params come from environment (load_dwh_config).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


class DwhConnectionPool:
    """Async connection pool to the dwh warehouse (read-only)."""

    def __init__(
        self,
        host: str,
        port: int,
        db: str,
        user: str,
        password: str,
        min_size: int = 2,
        max_size: int = 10,
        statement_timeout_ms: int = 60_000,
    ) -> None:
        """Initialize the pool (does NOT connect yet).

        Parameters
        ----------
        host, port, db, user, password : str, int, str, str, str
            Connection credentials. Use only those provided by load_dwh_config.
        min_size, max_size : int, int
            Pool bounds. Desktop app: keep modest to avoid overwhelming the tunnel.
        statement_timeout_ms : int
            Per-query timeout in milliseconds (server-side GUC). Default 60s.
        """
        self._host = host
        self._port = port
        self._db = db
        self._user = user
        self._password = password
        self._min_size = min_size
        self._max_size = max_size
        self._statement_timeout_ms = statement_timeout_ms
        self._pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        """Open the connection pool. Must be called during app startup."""
        if self._pool is not None:
            logger.warning("DwhConnectionPool.connect() called but pool already open")
            return

        # Assemble the connection string
        connstr = f"postgresql://{self._user}:{self._password}@{self._host}:{self._port}/{self._db}"

        # Create the pool with read-only enforcement
        self._pool = AsyncConnectionPool(
            connstr,
            min_size=self._min_size,
            max_size=self._max_size,
            kwargs={
                "sslmode": "require",
                "connect_timeout": 15,
                "options": (
                    f"-c default_transaction_read_only=on "
                    f"-c statement_timeout={self._statement_timeout_ms}"
                ),
            },
        )

        # Open the pool (this establishes min_size connections)
        await self._pool.open()
        logger.info(
            "DwhConnectionPool opened: host=%s port=%d db=%s min=%d max=%d",
            self._host,
            self._port,
            self._db,
            self._min_size,
            self._max_size,
        )

    async def close(self) -> None:
        """Close all connections in the pool. Called during app shutdown."""
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None
        logger.info("DwhConnectionPool closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[tuple[object, ...]]]:
        """Context manager: yield a single connection from the pool.

        Usage:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    cur.execute(...)
        """
        if self._pool is None:
            raise RuntimeError("DwhConnectionPool not connected (call connect() first)")
        async with self._pool.connection() as conn:
            # Enforce read-only at the driver level as well (defense in depth)
            # Async psycopg requires await .set_read_only() instead of property assignment
            await conn.set_read_only(True)
            yield conn

    @property
    def is_open(self) -> bool:
        """Return True if the pool is open and healthy."""
        return self._pool is not None and not self._pool.closed


def load_dwh_config() -> dict[str, str | int]:
    """Load dwh connection config from environment variables.

    Expected vars (from .env or shell):
        DWH_HOST: PostgreSQL host
        DWH_PORT: PostgreSQL port (default 5432)
        DWH_DB: database name (default "dwh")
        DWH_USER: role name
        DWH_PASSWORD: role password

    Returns a dict suitable for passing to DwhConnectionPool(**config).
    Raises ValueError if any required var is missing.
    """
    host = os.environ.get("DWH_HOST", "").strip()
    port_str = os.environ.get("DWH_PORT", "5432").strip()
    db = os.environ.get("DWH_DB", "dwh").strip()
    user = os.environ.get("DWH_USER", "").strip()
    password = os.environ.get("DWH_PASSWORD", "").strip()

    if not host:
        raise ValueError("DWH_HOST not set in environment")
    if not user:
        raise ValueError("DWH_USER not set in environment")
    if not password:
        raise ValueError("DWH_PASSWORD not set in environment")

    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(f"DWH_PORT must be numeric, got {port_str!r}") from None

    return {
        "host": host,
        "port": port,
        "db": db,
        "user": user,
        "password": password,
    }
