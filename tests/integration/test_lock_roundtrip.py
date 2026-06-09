"""Integration tests: lock guard round-trip against a live MongoDB.

Each test persists a probe doc with a unique ``_test-lock-<uuid>`` id
prefix, exercises the lock/unlock cycle against the REAL WriteRepository,
and cleans up probe docs in teardown.

Skipped automatically when ``MONGO_APP_WRITE_URI`` is unset (mirrors the
exact gating strategy used in ``test_persistence_roundtrip.py``).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace as _replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tcg.core.config import load_config
from tcg.persistence import (
    WriteRepository,
    build_write_client,
)
from tcg.persistence.repository import LockedError
from tcg.types.persistence import (
    Category,
    IndicatorDoc,
    PortfolioDoc,
    SignalDoc,
)


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
    """Yield a ``WriteRepository`` and tear down every probe doc this
    test wrote.  The ID prefix is unique per test so concurrent runs
    do not collide.
    """
    cfg = load_config()
    client = build_write_client()
    repo = WriteRepository(
        client,
        db_name=cfg.app_write_db_name,
        collection_name=cfg.app_write_collection,
    )
    prefix = f"_test-lock-{uuid.uuid4().hex[:12]}"
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


async def test_indicator_lock_guard(repo_with_cleanup) -> None:
    """Lock guard full cycle for an IndicatorDoc.

    Sequence:
    1. Create doc.
    2. set_locked(True) → lock it.
    3. update raises LockedError (guard reads STORED state, not payload).
    4. archive raises LockedError.
    5. get_by_id still returns the doc (reads are unaffected).
    6. list_by_type still includes the doc.
    7. set_locked(False) → unlock it.
    8. update now succeeds.
    """
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("ind")
    now = _now()
    doc = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="EMA-20",
        definition={"period": 20, "field": "close"},
        created_at=now,
        updated_at=now,
    )

    # 1. create
    stored = await repo.create(doc)
    assert isinstance(stored, IndicatorDoc)
    assert stored.locked is False

    # 2. lock
    locked_doc = await repo.set_locked("indicator", doc_id, locked=True)
    assert locked_doc.locked is True

    # 3. update must raise LockedError regardless of payload's locked flag
    update_payload = _replace(stored, name="EMA-20-mutated")
    with pytest.raises(LockedError):
        await repo.update(update_payload)

    # 4. archive must raise LockedError
    with pytest.raises(LockedError):
        await repo.archive("indicator", doc_id)

    # 5. get_by_id returns the doc (reads are unaffected by the lock)
    fetched = await repo.get_by_id("indicator", doc_id)
    assert isinstance(fetched, IndicatorDoc)
    assert fetched.locked is True
    assert fetched.name == "EMA-20"  # mutation was blocked

    # 6. list_by_type still includes the locked doc
    active_list = await repo.list_by_type("indicator")
    assert any(d.id == doc_id for d in active_list)

    # 7. unlock
    unlocked_doc = await repo.set_locked("indicator", doc_id, locked=False)
    assert unlocked_doc.locked is False

    # 8. update succeeds after unlock
    after_unlock = await repo.update(
        _replace(fetched, name="EMA-20-mutated", locked=False)
    )
    assert after_unlock.name == "EMA-20-mutated"


async def test_signal_lock_guard(repo_with_cleanup) -> None:
    """Lock guard for a SignalDoc — also covers category-change rejection.

    A category change goes through ``update``; it must also be blocked
    by the lock guard (category changes are handled by the same
    ``update`` path).
    """
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("sig")
    now = _now()
    doc = SignalDoc(
        id=doc_id,
        type="signal",
        name="momentum-lock-test",
        category=Category.DEV,
        created_at=now,
        updated_at=now,
    )

    stored = await repo.create(doc)
    assert isinstance(stored, SignalDoc)
    assert stored.locked is False

    # Lock it.
    await repo.set_locked("signal", doc_id, locked=True)

    # update (including a category change) must raise LockedError.
    promote_payload = _replace(stored, category=Category.PROD)
    with pytest.raises(LockedError):
        await repo.update(promote_payload)

    # archive must raise LockedError.
    with pytest.raises(LockedError):
        await repo.archive("signal", doc_id)

    # Reads unaffected — locked doc is still visible.
    fetched = await repo.get_by_id("signal", doc_id)
    assert isinstance(fetched, SignalDoc)
    assert fetched.locked is True
    assert fetched.category == Category.DEV  # category change was blocked

    dev_list = await repo.list_by_type_and_category("signal", Category.DEV)
    assert any(d.id == doc_id for d in dev_list)

    # Unlock, then archive succeeds.
    await repo.set_locked("signal", doc_id, locked=False)
    await repo.archive("signal", doc_id)
    archived = await repo.get_by_id("signal", doc_id)
    assert isinstance(archived, SignalDoc)
    assert archived.category == Category.ARCHIVE


async def test_portfolio_lock_guard(repo_with_cleanup) -> None:
    """Lock guard for a PortfolioDoc — mirrors indicator/signal coverage."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("ptf")
    now = _now()
    legs = (
        {
            "label": "SPY",
            "type": "instrument",
            "collection": "spot_daily",
            "symbol": "SPY",
            "weight": 100,
        },
    )
    doc = PortfolioDoc(
        id=doc_id,
        type="portfolio",
        name="lock-test-ptf",
        category=Category.RESEARCH,
        created_at=now,
        updated_at=now,
        legs=legs,
        rebalance="monthly",
    )

    stored = await repo.create(doc)
    assert isinstance(stored, PortfolioDoc)
    assert stored.locked is False

    # Lock it.
    await repo.set_locked("portfolio", doc_id, locked=True)

    # update (including a category change) must raise LockedError.
    with pytest.raises(LockedError):
        await repo.update(_replace(stored, category=Category.DEV))

    # archive must raise LockedError.
    with pytest.raises(LockedError):
        await repo.archive("portfolio", doc_id)

    # Reads unaffected.
    fetched = await repo.get_by_id("portfolio", doc_id)
    assert isinstance(fetched, PortfolioDoc)
    assert fetched.locked is True
    assert fetched.category == Category.RESEARCH  # change was blocked

    research_list = await repo.list_by_type_and_category("portfolio", Category.RESEARCH)
    assert any(d.id == doc_id for d in research_list)

    # Unlock, then update succeeds.
    await repo.set_locked("portfolio", doc_id, locked=False)
    after_unlock = await repo.update(
        _replace(fetched, name="lock-test-ptf-updated", locked=False)
    )
    assert after_unlock.name == "lock-test-ptf-updated"
    assert after_unlock.locked is False
