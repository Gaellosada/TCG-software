"""Integration tests: full ``BasketDoc`` CRUD round-trip against the REAL
``tcg_app_data`` PostgreSQL schema.

Mirrors :mod:`tests.integration.test_persistence_roundtrip`. Skipped
automatically when the app-data credentials are not configured.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tcg.persistence import (
    AppDbConnectionPool,
    WriteRepository,
    load_app_db_config,
)
from tcg.persistence._pg import DEFAULT_SCHEMA
from tcg.types.persistence import BasketDoc, Category


def _app_db_creds_present() -> bool:
    env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    user = os.environ.get("APP_DB_USER") or env.get("APP_DB_USER")
    password = os.environ.get("APP_DB_PASSWORD") or env.get("APP_DB_PASSWORD")
    return bool(user and password)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _app_db_creds_present(),
        reason="APP_DB_USER / APP_DB_PASSWORD not configured",
    ),
]


@pytest.fixture
async def repo_with_cleanup():
    pool = AppDbConnectionPool(**load_app_db_config())
    await pool.connect()
    repo = WriteRepository(pool)
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
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    for doc_id in created_ids:
                        await cur.execute(
                            f"DELETE FROM {DEFAULT_SCHEMA}.baskets WHERE id = %s",
                            (doc_id,),
                        )
        finally:
            await pool.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_basket_roundtrip(repo_with_cleanup) -> None:
    """Full create → get → list → update → archive cycle for BasketDoc."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("basket")
    now = _now()
    legs = (
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "SPY"},
            "weight": 0.6,
        },
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "QQQ"},
            "weight": 0.4,
        },
    )
    doc = BasketDoc(
        id=doc_id,
        type="basket",
        name="Integration Basket",
        category=Category.RESEARCH,
        asset_class="equity",
        created_at=now,
        updated_at=now,
        legs=legs,
    )

    # 1. create
    stored = await repo.create(doc)
    assert isinstance(stored, BasketDoc)
    assert stored.id == doc_id
    assert stored.legs == legs
    assert stored.asset_class == "equity"

    # 2. get_by_id
    fetched = await repo.get_by_id("basket", doc_id)
    assert fetched == stored

    # 3. list_by_type_and_category
    all_research = await repo.list_by_type_and_category("basket", Category.RESEARCH)
    assert any(d.id == doc_id for d in all_research)

    # 4. update — change name + legs to a continuous-future basket (CAS).
    new_legs = (
        {
            "instrument": {
                "type": "continuous",
                "collection": "FUT_VIX",
                "adjustment": "ratio",
                "cycle": "HMUZ",
                "rollOffset": 0,
                "strategy": "front_month",
            },
            "weight": 1.0,
        },
    )
    updated_input = BasketDoc(
        id=doc_id,
        type="basket",
        name="Updated Basket",
        category=Category.DEV,
        asset_class="future",
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        legs=new_legs,
    )
    after = await repo.update(updated_input, expected_updated_at=stored.updated_at)
    assert after.name == "Updated Basket"
    assert after.category == Category.DEV
    assert after.asset_class == "future"
    assert after.legs == new_legs

    # 5. archive — soft delete (category → 'DELETED'): hidden everywhere.
    await repo.archive("basket", doc_id)
    for cat in Category:
        lst = await repo.list_by_type_and_category("basket", cat)
        assert all(d.id != doc_id for d in lst)
