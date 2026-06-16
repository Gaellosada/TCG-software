from __future__ import annotations

import re
from dataclasses import dataclass

_USERINFO_RE = re.compile(r"://[^@]*@")


@dataclass(frozen=True, repr=False)
class MongoConfig:
    """Minimal MongoDB configuration. Loaded from .env.

    Collection names are NOT configured for the read-side -- they are
    discovered dynamically from MongoDB at startup and classified by prefix.

    Target database for reads: ``tcg-instrument`` (the legacy instrument/price
    database). The write-side targets ``tcg-app-data`` by default, overridable
    via ``MONGO_APP_WRITE_DB_NAME`` and ``MONGO_APP_WRITE_COLLECTION``.
    """

    uri: str
    db_name: str = "tcg-instrument"
    app_write_db_name: str = "tcg-app-data"
    app_write_collection: str = "2026-app-data"

    def __repr__(self) -> str:
        safe_uri = _USERINFO_RE.sub("://***:***@", self.uri)
        return f"MongoConfig(uri={safe_uri!r}, db_name={self.db_name!r})"


@dataclass(frozen=True, repr=False)
class DwhConfig:
    """PostgreSQL ``dwh`` warehouse configuration for market-data reads.

    Loaded from ``.env`` (``DWH_*`` keys). This replaces the Mongo
    ``tcg-instrument`` read path; the write-side (``tcg.persistence``)
    keeps its own Mongo ``MongoConfig`` and is unaffected.

    The schema is fixed (``tcg_instruments``) and not configurable — the
    backfill loader writes there and the read layer mirrors it.

    Read-only is enforced at the connection level (see
    ``tcg.data._sql.connection``): ``default_transaction_read_only=on`` plus
    a ``statement_timeout``. ``sslmode`` defaults to ``require`` so a
    tunneled localhost and an in-VPC host both connect with zero code
    change. ``min_size``/``max_size`` keep a small pool (single-user
    desktop app).
    """

    host: str
    user: str
    password: str
    port: int = 5432
    dbname: str = "dwh"
    sslmode: str = "require"
    connect_timeout: int = 15
    statement_timeout_ms: int = 60_000
    min_size: int = 1
    max_size: int = 4

    def __repr__(self) -> str:
        # Never echo the password — repr lands in logs / tracebacks.
        return (
            f"DwhConfig(host={self.host!r}, port={self.port}, "
            f"dbname={self.dbname!r}, user={self.user!r}, "
            f"password='***', sslmode={self.sslmode!r})"
        )
