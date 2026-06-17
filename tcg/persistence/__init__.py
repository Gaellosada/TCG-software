"""Scoped write layer for indicators / signals / portfolios / baskets.

Only this package constructs the read-WRITE PostgreSQL pool bound to the
``tcg_app_data`` schema (role ``tcg_app_rw``) or instantiates
``WriteRepository``. See ``docs/persistence.md`` for the safety model.
"""

from tcg.persistence._pg import AppDbConnectionPool, load_app_db_config
from tcg.persistence.repository import (
    ConcurrentUpdateError,
    DocumentTooLargeError,
    DuplicateIdError,
    LockedError,
    WriteRepository,
)

__all__ = [
    "WriteRepository",
    "AppDbConnectionPool",
    "load_app_db_config",
    "ConcurrentUpdateError",
    "DocumentTooLargeError",
    "DuplicateIdError",
    "LockedError",
]
