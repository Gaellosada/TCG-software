from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MongoConfig:
    """Minimal MongoDB configuration. Loaded from .env.

    Collection names are NOT configured -- they are discovered
    dynamically from MongoDB at startup and classified by prefix.

    Target database: ``tcg-instrument`` (the legacy instrument/price database).
    The other legacy databases are out of scope for now.
    """
    uri: str
    db_name: str = "tcg-instrument"
