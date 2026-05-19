"""Integration tests: prove the server-side Mongo role rejects every
operation outside ``tcg-app-data.2026-app-data``.

The write namespace was migrated out of the legacy ``tcg-instrument``
database into its own dedicated ``tcg-app-data`` database. The scoped
user holds privileges on ``tcg-app-data.2026-app-data`` ONLY — every
collection in ``tcg-instrument`` (the read-only data layer's DB) and
every other collection in ``tcg-app-data`` must reject the scoped user
with ``OperationFailure`` code 13. ``listCollections`` on either DB
must also be rejected.

These tests are the live half of the two-layer isolation guarantee
(the other half is the static API-surface tests in
``tests/unit/test_persistence_api_surface.py``). If a future role
edit accidentally widens the privileges, this suite goes red.

Skipped automatically when ``MONGO_APP_WRITE_URI`` is unset.
"""

from __future__ import annotations

import os
import uuid

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


# Collections in the legacy read-only data DB. The scoped user must be
# rejected on EVERY one of them — the role lives in ``tcg-app-data`` and
# has zero privileges on ``tcg-instrument``. We include the legacy
# ``2026-app-data`` namespace explicitly: even after the user deletes the
# stale collection, the role must already deny it (defense in depth).
_LEGACY_DB_NAME = "tcg-instrument"
_LEGACY_DB_FORBIDDEN_COLLECTIONS = (
    "options",
    "instruments",
    "prices",
    "spot_daily",
    "2026-app-data",  # legacy write namespace — must remain rejected
)

# Other collections in the NEW write DB. Only ``2026-app-data`` is
# authorised — every sibling collection must be rejected.
_WRITE_DB_NAME = "tcg-app-data"
_WRITE_DB_FORBIDDEN_COLLECTIONS = (
    "other",
    "scratch",
    "system.users",
)

_ALLOWED_COLLECTION = "2026-app-data"


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


@pytest.mark.parametrize("collection_name", _LEGACY_DB_FORBIDDEN_COLLECTIONS)
async def test_find_on_legacy_db_collection_raises_unauthorized(
    scoped_client: AsyncIOMotorClient, collection_name: str
) -> None:
    """Reading from any collection in the legacy ``tcg-instrument`` DB
    must fail at the server with OperationFailure code 13 — the scoped
    user has zero privileges in that database."""
    db = scoped_client[_LEGACY_DB_NAME]
    with pytest.raises(OperationFailure) as excinfo:
        await db[collection_name].find_one({})
    assert excinfo.value.code == 13, (
        f"expected Mongo error code 13 (Unauthorized) for "
        f"{_LEGACY_DB_NAME}.{collection_name!r}, got {excinfo.value.code} "
        f"({excinfo.value.details})"
    )


@pytest.mark.parametrize("collection_name", _WRITE_DB_FORBIDDEN_COLLECTIONS)
async def test_find_on_write_db_sibling_collection_raises_unauthorized(
    scoped_client: AsyncIOMotorClient, collection_name: str
) -> None:
    """Reading from any sibling collection in ``tcg-app-data`` (other
    than ``2026-app-data``) must fail at the server with code 13 — the
    role is scoped to a single collection inside its own DB."""
    db = scoped_client[_WRITE_DB_NAME]
    with pytest.raises(OperationFailure) as excinfo:
        await db[collection_name].find_one({})
    assert excinfo.value.code == 13, (
        f"expected Mongo error code 13 (Unauthorized) for "
        f"{_WRITE_DB_NAME}.{collection_name!r}, got {excinfo.value.code} "
        f"({excinfo.value.details})"
    )


async def test_list_collections_on_legacy_db_is_unauthorized(
    scoped_client: AsyncIOMotorClient,
) -> None:
    """Enumerating collections in ``tcg-instrument`` requires
    ``listCollections``, which the scoped role does not grant."""
    db = scoped_client[_LEGACY_DB_NAME]
    with pytest.raises(OperationFailure) as excinfo:
        cursor = await db.list_collections()
        await cursor.to_list(None)
    assert excinfo.value.code == 13, (
        f"expected listCollections on {_LEGACY_DB_NAME} to raise code "
        f"13, got {excinfo.value.code} ({excinfo.value.details})"
    )


async def test_list_collections_on_write_db_is_unauthorized(
    scoped_client: AsyncIOMotorClient,
) -> None:
    """Even on its own DB, the scoped user cannot enumerate collections
    — the role grants ``listIndexes`` on the bound collection but not
    ``listCollections`` on the database."""
    db = scoped_client[_WRITE_DB_NAME]
    with pytest.raises(OperationFailure) as excinfo:
        cursor = await db.list_collections()
        await cursor.to_list(None)
    assert excinfo.value.code == 13, (
        f"expected listCollections on {_WRITE_DB_NAME} to raise code "
        f"13, got {excinfo.value.code} ({excinfo.value.details})"
    )


async def test_allowed_collection_is_readable(
    scoped_client: AsyncIOMotorClient,
) -> None:
    """Positive control: ``find_one`` on the authorised collection
    must succeed (even if the collection is empty — None is fine).
    Proves the failures above are *targeted*, not a global block."""
    db = scoped_client[_WRITE_DB_NAME]
    # If this raises, the role is broken in the opposite direction.
    await db[_ALLOWED_COLLECTION].find_one({})


async def test_allowed_collection_insert_and_delete_succeed(
    scoped_client: AsyncIOMotorClient,
) -> None:
    """Positive control: full insert/find/delete cycle on the
    authorised collection. Uses a unique ``_test-scope-<uuid>`` probe id
    so concurrent runs do not collide."""
    db = scoped_client[_WRITE_DB_NAME]
    coll = db[_ALLOWED_COLLECTION]
    probe_id = f"_test-scope-{uuid.uuid4().hex[:12]}"
    try:
        await coll.insert_one({"_id": probe_id, "type": "_probe"})
        fetched = await coll.find_one({"_id": probe_id})
        assert fetched is not None
        assert fetched["_id"] == probe_id
    finally:
        await coll.delete_one({"_id": probe_id})
