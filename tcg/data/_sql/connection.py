"""PostgreSQL dwh connection pool for read-only market data access.

Enforces read-only semantics three ways (defence in depth):
``default_transaction_read_only=on`` (libpq ``options`` GUC, inherited by
every connection), ``conn.set_read_only(True)`` (driver-side), and the
server-side grant of the ``tcg_read`` role (SELECT only). A
``statement_timeout`` caps runaway scans.

Credentials and connection params come from the environment / ``.env``
(:func:`load_dwh_config`). ``sslmode`` is config-driven (default ``require``)
so the same code runs against an in-VPC RDS host (prod) and a tunneled
``localhost`` (dev) with zero change.

Decimal handling: PostgreSQL ``NUMERIC`` arrives as :class:`decimal.Decimal`;
the engine / NumPy layers expect ``float``. Conversion happens at this
boundary (:func:`to_float`) so no ``Decimal`` leaks into a DTO.
"""

from __future__ import annotations

import logging
import math
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import dotenv_values
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tcg.types.market import DEFAULT_DWH_POOL_MAX_SIZE

logger = logging.getLogger(__name__)

SCHEMA = "tcg_instruments"

# ``.env`` lives at the repo root (three levels up from this module:
# tcg/data/_sql/connection.py -> repo root), matching tcg.core.config.
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


async def _configure_connection(conn: AsyncConnection) -> None:
    """Per-connection setup the pool runs once when it creates a connection.

    The libpq ``options`` GUC already forces ``default_transaction_read_only``;
    setting it on the driver too makes the intent explicit and survives a GUC
    reset. ``autocommit=True`` keeps each SELECT its own transaction so a
    pooled connection never holds an idle-in-transaction lock.
    """
    await conn.set_autocommit(True)
    await conn.set_read_only(True)


class DwhConnectionPool:
    """Async connection pool to the dwh warehouse (read-only)."""

    def __init__(
        self,
        host: str,
        port: int,
        db: str,
        user: str,
        password: str,
        min_size: int = 1,
        max_size: int = DEFAULT_DWH_POOL_MAX_SIZE,
        statement_timeout_ms: int = 60_000,
        sslmode: str = "require",
        connect_timeout: int = 15,
    ) -> None:
        """Initialize the pool (does NOT connect yet).

        Parameters
        ----------
        host, port, db, user, password : str, int, str, str, str
            Connection credentials. Use only those provided by load_dwh_config.
        min_size, max_size : int, int
            Pool bounds. Single-user desktop app: keep modest to avoid
            overwhelming a forwarded port.
        statement_timeout_ms : int
            Per-query timeout in milliseconds (server-side GUC). Default 60s.
        sslmode : str
            libpq sslmode. ``require`` for RDS (prod in-VPC and tunneled dev);
            set ``disable`` only for a local plaintext proxy. Config-driven so
            no code changes between environments.
        connect_timeout : int
            Per-connection TCP/handshake timeout in seconds.
        """
        self._host = host
        self._port = port
        self._db = db
        self._user = user
        self._password = password
        self._min_size = min_size
        self._max_size = max_size
        self._statement_timeout_ms = statement_timeout_ms
        self._sslmode = sslmode
        self._connect_timeout = connect_timeout
        self._pool: AsyncConnectionPool | None = None

    def _conninfo(self) -> str:
        """Build the libpq conninfo (password included — never log this).

        ``make_conninfo`` correctly quotes a password containing ``@``/``:``/
        ``/`` etc., which a hand-built ``postgresql://`` URL would corrupt.
        The ``options`` GUCs force read-only and cap runaway scans.
        """
        options = (
            f"-c default_transaction_read_only=on "
            f"-c statement_timeout={self._statement_timeout_ms}"
        )
        return make_conninfo(
            host=self._host,
            port=self._port,
            dbname=self._db,
            user=self._user,
            password=self._password,
            sslmode=self._sslmode,
            connect_timeout=self._connect_timeout,
            options=options,
        )

    async def connect(self) -> None:
        """Open the connection pool. Must be called during app startup."""
        if self._pool is not None:
            logger.warning("DwhConnectionPool.connect() called but pool already open")
            return

        # open=False + explicit open() per psycopg_pool guidance (opening in
        # the constructor is deprecated). This also surfaces a bad connection
        # at startup rather than lazily on the first query.
        self._pool = AsyncConnectionPool(
            self._conninfo(),
            min_size=self._min_size,
            max_size=self._max_size,
            # dict rows: readers select by column name, insensitive to order.
            kwargs={"row_factory": dict_row},
            # configure runs once per new connection: belt-and-braces
            # read-only on top of the GUC + autocommit so no idle transaction
            # lingers on a pooled connection.
            configure=_configure_connection,
            # check runs on every BORROW: a lightweight liveness probe that
            # discards+replaces a connection the server has silently closed
            # (RDS/NAT idle-reap) BEFORE handing it to a query. Without it a
            # dead pooled socket is handed out and the first query fails with
            # "server closed the connection unexpectedly" — and, since the dead
            # connection is never replaced, EVERY subsequent request fails too.
            check=AsyncConnectionPool.check_connection,
            open=False,
            name="dwh-market",
        )
        await self._pool.open(wait=True, timeout=float(self._connect_timeout))
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
    async def connection(
        self, timeout: float | None = None
    ) -> AsyncIterator[AsyncConnection[tuple[object, ...]]]:
        """Context manager: yield a single connection from the pool.

        ``timeout`` (seconds) bounds how long to wait for a free connection to be
        handed out; ``None`` uses psycopg_pool's default (30s).  Previously this
        kwarg was swallowed, so the 30s acquire timeout was silently un-tunable
        (a saturated pool hung 30s before ``PoolTimeout``); it is now forwarded.

        Usage:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    cur.execute(...)
        """
        if self._pool is None:
            raise RuntimeError("DwhConnectionPool not connected (call connect() first)")
        # Read-only + autocommit are applied once per connection by the pool's
        # ``configure`` callback (_configure_connection), so nothing to set here.
        kwargs = {} if timeout is None else {"timeout": timeout}
        async with self._pool.connection(**kwargs) as conn:
            yield conn

    async def fetch_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Run a parameterized query and return the first row (dict) or None."""
        async with self.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchone()

    @property
    def is_open(self) -> bool:
        """Return True if the pool is open and healthy."""
        return self._pool is not None and not self._pool.closed


def load_dwh_config() -> dict[str, str | int]:
    """Load dwh connection config from ``.env`` / environment.

    Priority per key: real env var > ``.env`` file > default — matching
    ``tcg.core.config`` so a developer's ``.env`` is honoured without
    exporting anything.

    Expected vars:
        DWH_HOST: PostgreSQL host (required)
        DWH_PORT: PostgreSQL port (default 5432)
        DWH_DB: database name (default "dwh")
        DWH_USER: role name (required)
        DWH_PASSWORD: role password (required)
        DWH_SSLMODE: libpq sslmode (default "require")
        DWH_STATEMENT_TIMEOUT_MS: per-query cap in ms (default 60000)

    Returns a dict suitable for ``DwhConnectionPool(**config)``.
    Raises ``ValueError`` if any required var is missing or a numeric var
    is non-numeric.
    """
    env = dotenv_values(_ENV_PATH)

    def _get(key: str, default: str = "") -> str:
        return (os.environ.get(key) or env.get(key) or default).strip()

    host = _get("DWH_HOST")
    user = _get("DWH_USER")
    password = _get("DWH_PASSWORD")

    missing = [
        k
        for k, val in (
            ("DWH_HOST", host),
            ("DWH_USER", user),
            ("DWH_PASSWORD", password),
        )
        if not val
    ]
    if missing:
        raise ValueError(
            "dwh market-data reads require the following variables which "
            f"are not set: {', '.join(missing)}"
        )

    port_str = _get("DWH_PORT", "5432")
    if not port_str.isdigit():
        raise ValueError(f"DWH_PORT must be numeric, got {port_str!r}")

    timeout_str = _get("DWH_STATEMENT_TIMEOUT_MS", "60000")
    if not timeout_str.isdigit():
        raise ValueError(
            f"DWH_STATEMENT_TIMEOUT_MS must be numeric, got {timeout_str!r}"
        )

    return {
        "host": host,
        "port": int(port_str),
        "db": _get("DWH_DB", "dwh"),
        "user": user,
        "password": password,
        "sslmode": _get("DWH_SSLMODE", "require"),
        "statement_timeout_ms": int(timeout_str),
    }


# --------------------------------------------------------------------------- #
# Decimal / NULL coercion at the boundary
# --------------------------------------------------------------------------- #
def to_float(value: Any) -> float | None:
    """Coerce a psycopg scalar (Decimal / int / float / None) to float | None.

    SQL NULL stays ``None``. A NaN (the dwh CHECKs forbid it, but be
    defensive) collapses to ``None`` so it never poisons downstream NumPy
    arithmetic — mirroring the Mongo adapter's NaN handling.
    """
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, (Decimal, int)):
        f = float(value)
        return None if math.isnan(f) else f
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def to_float_or(value: Any, default: float) -> float:
    """Like :func:`to_float` but substitute *default* for NULL / NaN.

    Used for non-critical OHLCV fields (open/high/low/volume) where the Mongo
    adapter filled missing values with ``0.0`` rather than dropping the bar.
    """
    f = to_float(value)
    return default if f is None else f
