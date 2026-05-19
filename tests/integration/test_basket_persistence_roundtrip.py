"""Integration tests: full ``BasketDoc`` CRUD round-trip against Mongo.

Mirrors :mod:`tests.integration.test_persistence_roundtrip`.  Skipped
automatically when ``MONGO_APP_WRITE_URI`` is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tcg.core.config import load_config
from tcg.persistence import WriteRepository, build_write_client
from tcg.types.persistence import BasketDoc, Category


_WRITE_URI = os.environ.get("MONGO_APP_WRITE_URI") or dotenv_values(
    Path(__file__).resolve().parents[2] / ".env"
).get("MONGO_APP_WRITE_URI")


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _WRITE_URI,
        reason="MONGO_APP_WRITE_URI not configured",
    ),
]


@pytest.fixture
async def repo_with_cleanup():
    cfg = load_config()
    client = build_write_client()
    repo = WriteRepository(
        client,
        db_name=cfg.app_write_db_name,
        collection_name=cfg.app_write_collection,
    )
    prefix = f"_test-basket-{uuid.uuid4().hex[:12]}"
    created_ids: list[str] = []

    class _Repo:
        def __init__(self) -> None:
            self.inner = repo
            self.prefix = prefix

        def id(self, suffix: str) -> str:
            full = f"{prefix}-{suffix}"
            created_ids.append(full)
            return full

    try:
        yield _Repo()
    finally:
        coll = repo._coll
        if created_ids:
            await coll.delete_many({"_id": {"$in": created_ids}})
        client.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_basket_roundtrip(repo_with_cleanup) -> None:
    """Full create → get → list → update → archive cycle for BasketDoc."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("basket")
    now = _now()
    legs = (
        {"instrument_id": "SPY", "collection": "ETF", "weight": 0.6},
        {"instrument_id": "QQQ", "collection": "ETF", "weight": 0.4},
    )
    doc = BasketDoc(
        id=doc_id,
        type="basket",
        name="Integration Basket",
        category=Category.RESEARCH,
        created_at=now,
        updated_at=now,
        legs=legs,
    )

    # 1. create
    stored = await repo.create(doc)
    assert isinstance(stored, BasketDoc)
    assert stored.id == doc_id
    assert stored.legs == legs

    # 2. get_by_id
    fetched = await repo.get_by_id("basket", doc_id)
    assert isinstance(fetched, BasketDoc)
    assert fetched == stored

    # 3. list_by_type_and_category — our doc must be present.
    all_research = await repo.list_by_type_and_category(
        "basket", Category.RESEARCH
    )
    assert any(d.id == doc_id for d in all_research)

    # 4. update — change name + legs.
    new_legs = (
        {"instrument_id": "SPY", "collection": "ETF", "weight": 1.0},
    )
    updated_input = BasketDoc(
        id=doc_id,
        type="basket",
        name="Updated Basket",
        category=Category.DEV,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        legs=new_legs,
    )
    after = await repo.update(updated_input)
    assert isinstance(after, BasketDoc)
    assert after.name == "Updated Basket"
    assert after.category == Category.DEV
    assert after.legs == new_legs

    # 5. archive — sets category=ARCHIVE.
    await repo.archive("basket", doc_id)
    archived = await repo.get_by_id("basket", doc_id)
    assert isinstance(archived, BasketDoc)
    assert archived.category == Category.ARCHIVE
