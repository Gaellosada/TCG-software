"""Scoped Motor client factory for the persistence (write) layer.

This is the ONLY module that reads ``MONGO_APP_WRITE_URI``. The URI
belongs to a Mongo user whose role is restricted, server-side, to
``readWrite`` on the single collection ``tcg-instrument.2026-app-data``
(see ``workspace/tasks/persistence-layer/output/scope-check-evidence.json``
for the privilege check).

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

from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient


_WRITE_URI_ENV = "MONGO_APP_WRITE_URI"


def _read_write_uri() -> str:
    """Return the scoped write URI from env or the project ``.env`` file.

    Priority: real env > .env (mirrors ``tcg.core.config.load_config``).
    Raises ``ValueError`` if the variable is missing — we do NOT fall
    back to any unscoped URI.
    """
    real = os.environ.get(_WRITE_URI_ENV)
    if real:
        return real
    # ``parents[2]`` resolves to the project root (``TCG-software/``)
    # from ``tcg/persistence/_client.py``.
    env_path = Path(__file__).resolve().parents[2] / ".env"
    env = dotenv_values(env_path)
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
