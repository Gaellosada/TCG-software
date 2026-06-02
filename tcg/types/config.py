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
