"""FastAPI wiring for the persistence (write) layer.

Mirrors the ``_options_wiring`` pattern: lazy-init a singleton, hand
it out via ``Depends(...)``. The scoped Motor client is created on
first request and reused for the lifetime of the FastAPI app. The
``tcg.core.app.lifespan`` shutdown hook would normally close the
client, but the write client is a separate one (different URI / user)
and is closed implicitly when the process exits.

Why lazy-init rather than build at lifespan startup?
----------------------------------------------------
Two reasons:

1. **Local-dev ergonomics.** The read-only ``MONGO_URI`` is the only
   env var the existing developer setup requires. The write user is
   optional for read-only flows (e.g. running tests against the
   data layer alone). Lazy init means the app starts even without
   ``MONGO_APP_WRITE_URI``; only the persistence endpoints fail (with
   a clean 500) when the var is missing.
2. **Test isolation.** Unit tests that exercise the API surface
   without a live Mongo can avoid touching this dependency by simply
   not calling its endpoints — no startup-time crash.
"""

from __future__ import annotations

from tcg.core.config import load_config
from tcg.persistence import WriteRepository, build_write_client


_REPO_SINGLETON: WriteRepository | None = None


def get_write_repository() -> WriteRepository:
    """FastAPI dependency: return the process-wide ``WriteRepository``.

    Builds the scoped Motor client on first call. Subsequent calls
    reuse the same instance. Raises ``ValueError`` (propagated as a
    500 by FastAPI) when ``MONGO_APP_WRITE_URI`` is not configured.
    DB name and collection name are resolved from ``MongoConfig`` (env
    vars ``MONGO_APP_WRITE_DB_NAME`` and ``MONGO_APP_WRITE_COLLECTION``),
    with safe defaults ``tcg-app-data`` and ``2026-app-data``.
    """
    global _REPO_SINGLETON
    if _REPO_SINGLETON is None:
        cfg = load_config()
        _REPO_SINGLETON = WriteRepository(
            build_write_client(),
            db_name=cfg.app_write_db_name,
            collection_name=cfg.app_write_collection,
        )
    return _REPO_SINGLETON


def reset_write_repository_singleton() -> None:
    """Test-only hook: drop the cached instance so the next call
    rebuilds. Production code MUST NOT call this — it would orphan
    any open Motor connections."""
    global _REPO_SINGLETON
    _REPO_SINGLETON = None


__all__ = ["get_write_repository", "reset_write_repository_singleton"]
