"""Async Motor client factory + .env discovery + sync wrapper."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, TypeVar

from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# Real Motor / pymongo collection classes — used for `isinstance` detection in
# `_ReadOnlyDatabase.__getattr__`. Both imports are best-effort: if either is
# unavailable, we fall through to the type-name suffix match (which keeps the
# test fakes working). See M4 in `review-final-opinionated.md`.
_COLLECTION_CLASSES: tuple[type, ...]
try:
    from motor.motor_asyncio import (  # noqa: F401
        AsyncIOMotorCollection as _MotorColl,
    )
    try:
        from pymongo.collection import Collection as _PyMongoColl  # noqa: F401
        _COLLECTION_CLASSES = (_MotorColl, _PyMongoColl)
    except ImportError:
        _COLLECTION_CLASSES = (_MotorColl,)
except ImportError:  # pragma: no cover - motor is a hard dep above
    try:
        from pymongo.collection import Collection as _PyMongoColl  # noqa: F401
        _COLLECTION_CLASSES = (_PyMongoColl,)
    except ImportError:
        _COLLECTION_CLASSES = ()

T = TypeVar("T")

DEFAULT_URI = "mongodb://localhost:27017"
DEFAULT_DB = "tcg-instrument"


def _find_workspace_root(start: Path) -> Path | None:
    """Walk upwards looking for STRATEGY.yaml; first hit wins."""
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "STRATEGY.yaml").is_file():
            return parent
    return None


def _find_repo_root(start: Path) -> Path | None:
    """Walk upwards looking for the backtester repo root."""
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        py = parent / "pyproject.toml"
        if py.is_file():
            try:
                if "[tool.tcg-claude-backtester]" in py.read_text(encoding="utf-8"):
                    return parent
            except OSError:
                pass
        if (parent / "lib" / "mongo.py").is_file():
            return parent
    return None


def resolve_env(env_path: Path | None = None) -> dict[str, str]:
    """Resolve env values for MONGO_URI / MONGO_DB_NAME (real env > .env > defaults)."""
    file_vals: dict[str, str] = {}
    if env_path is not None:
        if not env_path.is_file():
            raise FileNotFoundError(f"explicit env_path not found: {env_path}")
        file_vals = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
    else:
        cwd = Path.cwd()
        candidates: list[Path] = []
        ws = _find_workspace_root(cwd)
        if ws is not None:
            candidates.append(ws / ".env")
        repo = _find_repo_root(cwd)
        if repo is not None:
            candidates.append(repo / ".env")
        for cand in candidates:
            if cand.is_file():
                file_vals = {k: v for k, v in dotenv_values(cand).items() if v is not None}
                break

    out: dict[str, str] = {
        "MONGO_URI": os.environ.get("MONGO_URI") or file_vals.get("MONGO_URI") or DEFAULT_URI,
        "MONGO_DB_NAME": os.environ.get("MONGO_DB_NAME") or file_vals.get("MONGO_DB_NAME") or DEFAULT_DB,
    }
    return out


def load_env(env_path: Path | None = None) -> dict[str, str]:
    """Alias for resolve_env, kept for spec parity."""
    return resolve_env(env_path)


def create_client(
    env_path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    server_selection_timeout_ms: int = 5000,
) -> AsyncIOMotorClient:
    """Build an AsyncIOMotorClient (lazy connect, 5s default selection timeout).

    Validates the URI scheme (`mongodb://` or `mongodb+srv://`) before constructing
    the client to defend against URI confusion / unintended targets via .env override.
    """
    cfg = env if env is not None else resolve_env(env_path)
    uri = cfg["MONGO_URI"]
    if not (uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")):
        # Extract scheme only — never include the rest of the URI in the error
        # message, as it may contain ``user:password@host`` if the user typoed
        # a wrong scheme on a credentialed URI (m-URI-1).
        scheme = uri.split("://", 1)[0] if "://" in uri else uri[:30]
        raise ValueError(
            f"MONGO_URI must use mongodb:// or mongodb+srv:// scheme; got scheme {scheme!r}"
        )
    return AsyncIOMotorClient(uri, serverSelectionTimeoutMS=server_selection_timeout_ms)


# ---------------------------------------------------------------------------- read-only proxy
# Closes the in-process Mongo-write surface: every write method on the wrapped
# database/collection raises before any byte hits the wire. This is in addition
# to the deployment-layer expectation that MONGO_URI authenticates as a Mongo
# user with the `read` role only.

class MongoWriteForbiddenError(PermissionError):
    """Raised when an agent attempts to mutate Mongo via the lib client.

    Subclasses :class:`PermissionError` so callers can write
    ``except PermissionError:`` and catch this naturally — the stdlib semantic
    class for "operation refused due to insufficient privileges".
    """


_FORBIDDEN_METHODS: frozenset[str] = frozenset({
    "insert_one", "insert_many",
    "update_one", "update_many",
    "delete_one", "delete_many",
    "replace_one", "bulk_write",
    "find_one_and_update", "find_one_and_delete", "find_one_and_replace",
    "create_index", "create_indexes",
    "drop_index", "drop_indexes",
    "drop", "rename",
    "create_collection",
    # Forbid `with_options` outright: it returns a fresh Collection that is
    # not wrapped by this proxy, exposing all write methods. Rarely a
    # legitimate read concern; if needed in the future, add a wrapping path.
    "with_options",
})


# Aggregation pipeline stages that perform writes (silent write paths via the
# read-named `aggregate` API). Pipelines containing any of these keys must be
# rejected before delegation. See B-MONGO-3.
_WRITE_AGG_STAGES: frozenset[str] = frozenset({"$out", "$merge"})


def _check_aggregate_pipeline(pipeline: Any) -> None:
    """Raise MongoWriteForbiddenError if `pipeline` contains a write stage.

    `pipeline` is expected to be an iterable of stage dicts. Non-dict entries
    are skipped (the underlying driver will surface its own TypeError on
    bad input — we don't shadow that).
    """
    for stage in (pipeline or []):
        if not isinstance(stage, dict):
            continue
        for key in stage:
            if key in _WRITE_AGG_STAGES:
                raise MongoWriteForbiddenError(
                    f"aggregate stage {key!r} forbidden via lib.mongo (writes)"
                )


class _ReadOnlyCollection:
    """Wrap a Motor / pymongo collection so write methods raise MongoWriteForbiddenError.

    Read methods (`find`, `find_one`, `count_documents`, `distinct`,
    `estimated_document_count`, `list_indexes`, ...) pass through unchanged.

    `aggregate` is a read API by convention but its pipeline can include
    `$out` / `$merge` write stages — we wrap it and inspect the pipeline
    arg before delegation.
    """

    __slots__ = ("_coll",)

    def __init__(self, coll: Any) -> None:
        self._coll = coll

    def __getattr__(self, name: str) -> Any:
        if name in _FORBIDDEN_METHODS:
            raise MongoWriteForbiddenError(
                f"Mongo write '{name}' is forbidden via lib.mongo. "
                "The backtester is read-only by design."
            )
        if name == "aggregate":
            underlying = getattr(self._coll, "aggregate")

            def _safe_aggregate(pipeline: Any = None, *args: Any, **kwargs: Any) -> Any:
                _check_aggregate_pipeline(pipeline)
                return underlying(pipeline, *args, **kwargs)

            return _safe_aggregate
        return getattr(self._coll, name)

    def __getitem__(self, name: str) -> Any:
        # Sub-collection access (rare; defensive).
        return _ReadOnlyCollection(self._coll[name])


class _ReadOnlyDatabase:
    """Wrap a Motor / pymongo database so write methods raise MongoWriteForbiddenError.

    Collection lookups (via attribute or item access) return `_ReadOnlyCollection`,
    so chained calls like ``db['foo'].insert_one(...)`` and ``db.foo.insert_one(...)``
    are both blocked.

    The ``client`` attribute is intentionally NOT exposed: returning the unwrapped
    `AsyncIOMotorClient` would let callers re-derive an unrestricted db. Use
    :func:`lib.mongo.create_client` directly if a raw client is genuinely needed.
    """

    __slots__ = ("_db",)
    # Set of database-level write methods that we additionally block. The
    # collection-level set is reused for collection wrapping; the db-level set
    # adds drop_collection / drop_database explicitly.
    #
    # `command` / `run_command` are the raw admin-channel: Mongo's
    # ``{"insert": ...}`` / ``{"update": ...}`` / ``{"createIndexes": ...}`` etc.
    # all execute via these. Forbidden outright; if a read-only command is ever
    # needed (e.g. serverStatus), a `safe_command(...)` allowlist helper would
    # be the right venue, not exposing the raw channel.
    #
    # `with_options` returns a fresh Database object that is NOT wrapped —
    # forbidden outright (M-MONGO-1).
    #
    # `watch` opens a change stream — read-shaped but unbounded resource;
    # forbidden defensively.
    #
    # `client` returns the unwrapped AsyncIOMotorClient (B-MONGO-1) — full bypass.
    _DB_FORBIDDEN: frozenset[str] = frozenset({
        "drop_collection", "drop_database", "create_collection",
        "command", "run_command",
        "watch", "with_options",
        "client",
    })

    def __init__(self, db: Any) -> None:
        self._db = db

    def __getitem__(self, name: str) -> _ReadOnlyCollection:
        return _ReadOnlyCollection(self._db[name])

    def __getattr__(self, name: str) -> Any:
        if name in _FORBIDDEN_METHODS or name in self._DB_FORBIDDEN:
            raise MongoWriteForbiddenError(
                f"Mongo write '{name}' is forbidden via lib.mongo. "
                "The backtester is read-only by design."
            )
        attr = getattr(self._db, name)
        # Dynamic Motor / pymongo collection attribute access (e.g. ``db.bars``)
        # returns a Collection object. Wrap it so writes through that path are
        # also blocked. Detection order:
        #   1. isinstance against real Motor / pymongo Collection classes
        #      (cheap, exact, robust against future class-name changes).
        #   2. type-name suffix match — covers test fakes that don't subclass
        #      the real classes.
        if _COLLECTION_CLASSES and isinstance(attr, _COLLECTION_CLASSES):
            return _ReadOnlyCollection(attr)
        if type(attr).__name__.endswith("Collection"):
            return _ReadOnlyCollection(attr)
        return attr


def get_db(client: AsyncIOMotorClient, db_name: str | None = None) -> _ReadOnlyDatabase:
    """Return the named database wrapped in a read-only proxy.

    Defaults to env-resolved MONGO_DB_NAME. Calls to write methods on the
    returned database (or any collection retrieved from it) raise
    `MongoWriteForbiddenError` before any IO occurs.
    """
    name = db_name or resolve_env().get("MONGO_DB_NAME", DEFAULT_DB)
    return _ReadOnlyDatabase(client[name])


_PERSISTENT_LOOP: asyncio.AbstractEventLoop | None = None


def _get_persistent_loop() -> asyncio.AbstractEventLoop:
    """Return a process-wide event loop, recreating it if it has been closed.

    Motor's ``AsyncIOMotorClient`` caches its IO loop on first use. Under
    Python 3.14 ``asyncio.run`` fully closes the loop on exit, so a second
    ``sync_run`` call against the same client raises ``Event loop is closed``.
    Holding a single loop alive for the lifetime of the process keeps Motor's
    cached loop reference valid across repeat sync calls.
    """
    global _PERSISTENT_LOOP
    if _PERSISTENT_LOOP is None or _PERSISTENT_LOOP.is_closed():
        _PERSISTENT_LOOP = asyncio.new_event_loop()
    return _PERSISTENT_LOOP


def sync_run(coro: Awaitable[T]) -> T:
    """Run an awaitable to completion. Reuses the running loop under Jupyter (with nest_asyncio); else uses a persistent process-wide loop.

    Two execution modes:

    1. **No running loop (CLI / scripts / nbclient):** uses one process-wide
       event loop (lazily created). Motor's cached loop reference stays valid
       across repeat sync calls. See workspaces/short-10delta-put-spx/PROBLEMS.md
       for the original Python 3.14 + Motor failure mode.
    2. **Running loop present (Jupyter / IPython kernel):** runs the coroutine
       on the already-running loop via ``run_until_complete``. This requires
       ``nest_asyncio.apply()`` to have been called (the canonical bootstrap
       cell injected by ``lib.compile.compile_workspace`` does this). Without
       nest_asyncio, ``run_until_complete`` raises and we re-raise with an
       actionable message instead of letting the cryptic stdlib error through.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None and running.is_running():
        try:
            return running.run_until_complete(coro)  # type: ignore[arg-type]
        except RuntimeError as e:
            raise RuntimeError(
                "sync_run was called from inside a running event loop "
                "(typically a Jupyter / IPython kernel) without nest_asyncio applied. "
                "Either run `import nest_asyncio; nest_asyncio.apply()` first, "
                "or call the underlying async function with `await` instead of "
                "the *_sync wrapper."
            ) from e
    loop = _get_persistent_loop()
    return loop.run_until_complete(coro)  # type: ignore[arg-type]


class _SyncDB:
    """Wrap an AsyncIOMotorDatabase so that data_load coroutines can be called sync.

    Usage: ``db = sync_db(); bars = db.load_index_bars("SPX")``. Each attribute lookup
    auto-resolves to the matching `_sync` function in lib.data_load if the underlying
    Motor DB does not own the attribute. The wrapped database is itself a
    `_ReadOnlyDatabase` (returned by `get_db`), so any chained collection access
    (``sync_db()["bars"].insert_one(...)`` or ``sync_db().bars.insert_one(...)``)
    raises `MongoWriteForbiddenError`.
    """

    def __init__(self, db: _ReadOnlyDatabase):
        self._db = db

    def __getattr__(self, name: str) -> Any:
        # Fall back to sync data_load helpers.
        from . import data_load as _dl

        sync_name = name + "_sync" if not name.endswith("_sync") else name
        fn = getattr(_dl, sync_name, None)
        if fn is None:
            return getattr(self._db, name)

        def _bound(*args: Any, **kwargs: Any) -> Any:
            return fn(self._db, *args, **kwargs)

        return _bound

    def __getitem__(self, name: str) -> _ReadOnlyCollection:
        # Delegate to the underlying read-only database; returns a
        # `_ReadOnlyCollection` so write methods raise on access.
        return self._db[name]


def sync_db(env_path: Path | None = None) -> _SyncDB:
    """Return a sync-callable wrapper around the default database (read-only)."""
    client = create_client(env_path)
    return _SyncDB(get_db(client))
