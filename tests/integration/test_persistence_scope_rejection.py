"""Integration tests: prove the server-side Mongo role rejects every
operation outside ``tcg-instrument.2026-app-data``.

These tests are the live half of the two-layer isolation guarantee
(the other half is the static API-surface tests in
``tests/unit/test_persistence_api_surface.py``). If a future role
edit accidentally widens the privileges, this suite goes red.

Skipped automatically when ``MONGO_APP_WRITE_URI`` is unset.
"""

from __future__ import annotations

import os

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import OperationFailure


_WRITE_URI = os.environ.get("MONGO_APP_WRITE_URI")
if not _WRITE_URI:
    # Fall back to the .env file (matches the loader logic).
    from pathlib import Path

    from dotenv import dotenv_values

    _ENV_PATH = (
        Path(__file__).resolve().parents[2] / ".env"
    )
    _WRITE_URI = dotenv_values(_ENV_PATH).get("MONGO_APP_WRITE_URI")


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _WRITE_URI,
        reason="MONGO_APP_WRITE_URI not configured",
    ),
]


# Collections the scoped user MUST NOT be able to read or write. The
# four listed here are the existing read-side collections used by the
# data layer; any of them succeeding would prove the role is broken.
_FORBIDDEN_COLLECTIONS = ("options", "instruments", "prices", "spot_daily")


@pytest.fixture
async def scoped_client() -> AsyncIOMotorClient:
    """Yield a Motor client built from the scoped write URI.

    Closed in teardown — keeps connection pool tidy across tests.
    """
    client = AsyncIOMotorClient(_WRITE_URI, serverSelectionTimeoutMS=15_000)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("collection_name", _FORBIDDEN_COLLECTIONS)
async def test_find_on_other_collection_raises_unauthorized(
    scoped_client: AsyncIOMotorClient, collection_name: str
) -> None:
    """Reading from any collection other than 2026-app-data must
    fail at the server with OperationFailure code 13."""
    db = scoped_client["tcg-instrument"]
    with pytest.raises(OperationFailure) as excinfo:
        await db[collection_name].find_one({})
    assert excinfo.value.code == 13, (
        f"expected Mongo error code 13 (Unauthorized) for "
        f"collection={collection_name!r}, got {excinfo.value.code} "
        f"({excinfo.value.details})"
    )


async def test_list_collections_is_unauthorized(
    scoped_client: AsyncIOMotorClient,
) -> None:
    """Enumerating the collection namespace requires listCollections,
    which the scoped role does not grant.

    Motor returns a coroutine that resolves to an AsyncIOMotorLatentCommandCursor
    — the auth check fires either on the await itself or on the first
    ``to_list`` call. We accept either.
    """
    db = scoped_client["tcg-instrument"]
    with pytest.raises(OperationFailure) as excinfo:
        cursor = await db.list_collections()
        await cursor.to_list(None)
    assert excinfo.value.code == 13, (
        f"expected listCollections to raise code 13, got "
        f"{excinfo.value.code} ({excinfo.value.details})"
    )


async def test_allowed_collection_is_readable(
    scoped_client: AsyncIOMotorClient,
) -> None:
    """Positive control: ``find_one`` on the authorised collection
    must succeed (even if the collection is empty — None is fine).
    Proves the failures above are *targeted*, not a global block."""
    db = scoped_client["tcg-instrument"]
    # If this raises, the role is broken in the opposite direction.
    await db["2026-app-data"].find_one({})
