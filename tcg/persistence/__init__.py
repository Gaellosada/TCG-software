"""Scoped write layer for indicators / signals / portfolios.

Only this package may construct the Motor client bound to
``MONGO_APP_WRITE_URI`` or instantiate ``WriteRepository``. See
``docs/persistence.md`` for the safety model.
"""

from tcg.persistence._client import build_write_client
from tcg.persistence.repository import WriteRepository

__all__ = ["WriteRepository", "build_write_client"]
