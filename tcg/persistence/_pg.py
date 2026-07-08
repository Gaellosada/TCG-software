"""PostgreSQL read-WRITE connection pool for app-data persistence.

This is the app-data counterpart to :mod:`tcg.data._sql.connection` (the
read-only market-data pool). It targets the SAME dwh RDS / ``dwh``
database but a DIFFERENT schema (``tcg_app_data``) and a read-WRITE role
(``tcg_app_rw``). It deliberately does NOT enforce read-only:

* no ``default_transaction_read_only`` libpq option,
* no ``conn.set_read_only(True)`` in the per-connection configure hook.

``autocommit=True`` is kept so every single-statement CRUD operation
commits immediately and no pooled connection lingers idle-in-transaction.
A ``statement_timeout`` still caps a runaway query.

Credentials/params come from the environment / ``.env`` via
:func:`load_app_db_config`. Host / port / database default to the
``DWH_*`` values (same RDS, same database) — only the role
(``APP_DB_USER`` / ``APP_DB_PASSWORD``) and schema differ.

Living INSIDE ``tcg.persistence`` (not ``tcg.data._sql``) keeps the
``persistence-write-boundary`` import-linter contract meaningful:
``tcg.persistence`` imports only ``tcg.types`` (+ third-party psycopg),
and nothing in ``tcg.data`` / ``tcg.engine`` gains a path to the
app-data write pool.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import dotenv_values
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# Schema that holds the four app-data tables. Constant — every statement
# schema-qualifies its table name with this.
DEFAULT_SCHEMA = "tcg_app_data"

# ``.env`` lives at the repo root (two levels up from this module:
# tcg/persistence/_pg.py -> repo root), matching tcg.core.config.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


async def _configure_connection(conn: AsyncConnection) -> None:
    """Per-connection setup the pool runs once when it creates a connection.

    Unlike the market-data pool this is read-WRITE: we keep
    ``autocommit=True`` (each statement is its own committed transaction —
    correct for single-statement CRUD and avoids idle-in-transaction
    locks) but do NOT set the connection read-only.
    """
    await conn.set_autocommit(True)


class AppDbConnectionPool:
    """Async read-WRITE connection pool to the ``tcg_app_data`` schema."""

    def __init__(
        self,
        host: str,
        port: int,
        db: str,
        user: str,
        password: str,
        schema: str = DEFAULT_SCHEMA,
        min_size: int = 1,
        max_size: int = 4,
        statement_timeout_ms: int = 60_000,
        sslmode: str = "require",
        connect_timeout: int = 15,
    ) -> None:
        """Initialize the pool (does NOT connect yet).

        Parameters mirror :class:`tcg.data._sql.connection.DwhConnectionPool`
        plus ``schema`` (the app-data schema, default ``tcg_app_data``).
        Single-user desktop app → keep the pool small.
        """
        self._host = host
        self._port = port
        self._db = db
        self._user = user
        self._password = password
        self._schema = schema
        self._min_size = min_size
        self._max_size = max_size
        self._statement_timeout_ms = statement_timeout_ms
        self._sslmode = sslmode
        self._connect_timeout = connect_timeout
        self._pool: AsyncConnectionPool | None = None

    @property
    def schema(self) -> str:
        """The app-data schema these connections write to."""
        return self._schema

    def _conninfo(self) -> str:
        """Build the libpq conninfo (password included — never log this).

        Unlike the market pool there is NO ``default_transaction_read_only``
        GUC: this role writes. ``statement_timeout`` still caps a runaway
        query. ``make_conninfo`` correctly quotes a password containing
        ``@`` / ``:`` / ``/``.
        """
        options = f"-c statement_timeout={self._statement_timeout_ms}"
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
            logger.warning("AppDbConnectionPool.connect() called but pool already open")
            return

        self._pool = AsyncConnectionPool(
            self._conninfo(),
            min_size=self._min_size,
            max_size=self._max_size,
            # dict rows: the repository selects by column name (id / type /
            # category / locked / payload / created_at / updated_at).
            kwargs={"row_factory": dict_row},
            configure=_configure_connection,
            # check runs on every BORROW: a lightweight liveness probe that
            # discards+replaces a connection the server has silently closed
            # (RDS/NAT idle-reap) before handing it to a query, so a stale
            # pooled socket can never surface "server closed the connection
            # unexpectedly" and wedge every subsequent request. See the twin
            # comment in tcg/data/_sql/connection.py.
            check=AsyncConnectionPool.check_connection,
            open=False,
            name="app-data",
        )
        await self._pool.open(wait=True, timeout=float(self._connect_timeout))
        logger.info(
            "AppDbConnectionPool opened: host=%s port=%d db=%s schema=%s min=%d max=%d",
            self._host,
            self._port,
            self._db,
            self._schema,
            self._min_size,
            self._max_size,
        )

    async def close(self) -> None:
        """Close all connections in the pool. Called during app shutdown."""
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None
        logger.info("AppDbConnectionPool closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
        """Context manager: yield a single connection from the pool.

        Usage::

            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
        """
        if self._pool is None:
            raise RuntimeError(
                "AppDbConnectionPool not connected (call connect() first)"
            )
        async with self._pool.connection() as conn:
            yield conn

    @property
    def is_open(self) -> bool:
        """Return True if the pool is open and healthy."""
        return self._pool is not None and not self._pool.closed


def load_app_db_config() -> dict[str, str | int]:
    """Load app-data connection config from ``.env`` / environment.

    Priority per key: real env var > ``.env`` file > default — matching
    :func:`tcg.data._sql.connection.load_dwh_config`.

    The app-data store shares the dwh RDS host / port / database with the
    market-data store, so those default to the ``DWH_*`` values; only the
    role and schema are app-data specific.

    Expected vars:
        APP_DB_USER:     role name ``tcg_app_rw`` (required)
        APP_DB_PASSWORD: role password (required)
        APP_DB_HOST:     host (default DWH_HOST)
        APP_DB_PORT:     port (default DWH_PORT, then 5432)
        APP_DB_NAME:     database (default DWH_DB, then "dwh")
        APP_DB_SCHEMA:   schema (default "tcg_app_data")
        DWH_SSLMODE:     libpq sslmode (default "require")
        DWH_STATEMENT_TIMEOUT_MS: per-query cap in ms (default 60000)

    Returns a dict suitable for ``AppDbConnectionPool(**config)``. Raises
    ``ValueError`` if a required var is missing or a numeric var is
    non-numeric.
    """
    env = dotenv_values(_ENV_PATH)

    def _get(key: str, default: str = "") -> str:
        return (os.environ.get(key) or env.get(key) or default).strip()

    user = _get("APP_DB_USER")
    password = _get("APP_DB_PASSWORD")
    # Host / port / db inherit DWH_* (same RDS + database).
    host = _get("APP_DB_HOST") or _get("DWH_HOST")
    port_str = _get("APP_DB_PORT") or _get("DWH_PORT", "5432")
    db = _get("APP_DB_NAME") or _get("DWH_DB", "dwh")
    schema = _get("APP_DB_SCHEMA", DEFAULT_SCHEMA)
    sslmode = _get("DWH_SSLMODE", "require")
    timeout_str = _get("DWH_STATEMENT_TIMEOUT_MS", "60000")

    missing = [
        k
        for k, val in (
            ("APP_DB_USER", user),
            ("APP_DB_PASSWORD", password),
            ("APP_DB_HOST (or DWH_HOST)", host),
        )
        if not val
    ]
    if missing:
        raise ValueError(
            "app-data persistence requires the following variables which "
            f"are not set: {', '.join(missing)}"
        )

    if not port_str.isdigit():
        raise ValueError(f"APP_DB_PORT/DWH_PORT must be numeric, got {port_str!r}")
    if not timeout_str.isdigit():
        raise ValueError(
            f"DWH_STATEMENT_TIMEOUT_MS must be numeric, got {timeout_str!r}"
        )

    return {
        "host": host,
        "port": int(port_str),
        "db": db,
        "user": user,
        "password": password,
        "schema": schema,
        "sslmode": sslmode,
        "statement_timeout_ms": int(timeout_str),
    }


__all__ = ["AppDbConnectionPool", "load_app_db_config", "DEFAULT_SCHEMA"]
