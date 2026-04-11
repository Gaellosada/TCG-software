"""Motor client factory.

Creates a configured ``AsyncIOMotorDatabase`` from a ``MongoConfig``.
"""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from tcg.types.config import MongoConfig


async def create_mongo_client(config: MongoConfig) -> AsyncIOMotorDatabase:
    """Build an async Motor database handle.

    The client is created eagerly but the actual TCP connection is
    established lazily on first operation. ``serverSelectionTimeoutMS``
    prevents indefinite hangs if the server is unreachable.
    """
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        config.uri,
        serverSelectionTimeoutMS=5000,
    )
    return client[config.db_name]
