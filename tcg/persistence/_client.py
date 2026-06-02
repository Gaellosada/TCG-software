"""Scoped Motor client factory for the persistence (write) layer.

This is the ONLY module that reads ``MONGO_APP_WRITE_URI``. The URI
belongs to a Mongo user whose role is restricted, server-side, to
``readWrite`` on the single collection ``tcg-app-data.2026-app-data``
(see ``workspace/tasks/persistence-layer/output/migration-scope-check-evidence.json``
for the privilege check — the write namespace was migrated out of the
legacy ``tcg-instrument`` database into its own dedicated database so
the scoped user has zero visibility into ``tcg-instrument``).

The two-layer isolation model is:

  1. **Mongo role** — the server refuses any operation outside the one
     authorised namespace with ``OperationFailure`` code 13.
  2. **WriteRepository** — the application binds the collection handle
     exactly once in ``__init__`` and exposes no method that takes a
     collection name, so application bugs cannot route a write past
     the role boundary either.

If layer 1 fails closed (e.g. role mis-edit), layer 2 still blocks the
write; if layer 2 fails open (e.g. someone adds an escape hatch), the
server still rejects. Belt-and-suspenders by design.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient


_WRITE_URI_ENV = "MONGO_APP_WRITE_URI"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _read_write_uri() -> str:
    """Return the scoped write URI from env or the project ``.env`` file.

    When the SSM tunnel is enabled AND dedicated write credentials are
    set (``MONGO_APP_WRITE_USER`` + ``MONGO_APP_WRITE_PASSWORD``), the
    URI is assembled to route through localhost.  Otherwise falls through
    to the existing ``MONGO_APP_WRITE_URI`` logic.

    Priority: real env > .env (mirrors ``tcg.core.config.load_config``).
    Raises ``ValueError`` if no write URI can be resolved.
    """
    env = dotenv_values(_ENV_PATH)

    def _get(key: str, default: str = "") -> str:
        return os.getenv(key) or env.get(key) or default

    tunnel_enabled = _get("SSM_TUNNEL_ENABLED").lower() == "true"

    if tunnel_enabled:
        user = _get("MONGO_APP_WRITE_USER")
        password = _get("MONGO_APP_WRITE_PASSWORD")
        if user and password:
            local_port = _get("LOCAL_PORT", "27017")
            auth_source = _get("MONGO_AUTH_SOURCE", "admin")
            return (
                f"mongodb://{quote_plus(user)}:{quote_plus(password)}"
                f"@localhost:{local_port}/"
                f"?authSource={quote_plus(auth_source)}&directConnection=true"
            )
        # Write credentials not set — fall through to MONGO_APP_WRITE_URI

    real = os.environ.get(_WRITE_URI_ENV)
    if real:
        return real
    fromfile = env.get(_WRITE_URI_ENV)
    if fromfile:
        return fromfile
    raise ValueError(
        f"persistence: required env var {_WRITE_URI_ENV} is not set. "
        "The scoped Mongo write user must be provisioned and exported "
        "before the write layer can be used."
    )


def build_write_client() -> AsyncIOMotorClient:
    """Construct a Motor client bound to the scoped write URI.

    Caller is responsible for the client's lifecycle (Motor clients are
    cheap to keep alive — typically one instance per FastAPI app).
    """
    uri = _read_write_uri()
    return AsyncIOMotorClient(
        uri,
        serverSelectionTimeoutMS=30_000,
        connectTimeoutMS=60_000,
        socketTimeoutMS=300_000,
        maxPoolSize=10,
        # BSON stores datetimes as naive UTC; without tz_aware=True the
        # decoder returns naive datetimes that fail to compare equal
        # against timezone-aware values produced by ``datetime.now(utc)``.
        # The dataclasses standardise on tz-aware UTC, so the client must
        # match.
        tz_aware=True,
    )


__all__ = ["build_write_client"]
