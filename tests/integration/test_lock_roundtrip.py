"""Integration tests: lock guard round-trip against the REAL ``tcg_app_data``
PostgreSQL schema.

Each test persists probe docs with a unique ``_test-lock-<uuid>`` id prefix,
exercises the lock/unlock cycle against the real ``WriteRepository``, and
DELETEs the probe rows in teardown (try/finally) — no residue.

Skipped automatically when the app-data credentials are not configured.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace as _replace
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
from tcg.persistence.repository import LockedError
from tcg.types.persistence import (
    Category,
    IndicatorDoc,
    PortfolioDoc,
    SignalDoc,
)


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

_TABLES = {
    "indicator": "indicators",
    "signal": "signals",
    "portfolio": "portfolios",
}


@pytest.fixture
async def repo_with_cleanup():
    pool = AppDbConnectionPool(**load_app_db_config())
    await pool.connect()
    repo = WriteRepository(pool)
    prefix = f"_test-lock-{uuid.uuid4().hex[:12]}"
    created: list[tuple[str, str]] = []

    class _Repo:
        def __init__(self) -> None:
            self.inner = repo
            self.prefix = prefix

        def id(self, doc_type: str, suffix: str) -> str:
            full = f"{prefix}-{suffix}"
            created.append((doc_type, full))
            return full

    try:
        yield _Repo()
    finally:
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    for doc_type, doc_id in created:
                        await cur.execute(
                            f"DELETE FROM {DEFAULT_SCHEMA}.{_TABLES[doc_type]} "
                            "WHERE id = %s",
                            (doc_id,),
                        )
        finally:
            await pool.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_indicator_lock_guard(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("indicator", "ind")
    now = _now()
    doc = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="EMA-20",
        definition={"period": 20},
        created_at=now,
        updated_at=now,
    )
    stored = await repo.create(doc)
    assert stored.locked is False

    locked_doc = await repo.set_locked("indicator", doc_id, locked=True)
    assert locked_doc.locked is True

    # update must raise LockedError regardless of payload's locked flag.
    with pytest.raises(LockedError):
        await repo.update(_replace(stored, name="EMA-20-mutated"))

    # archive must raise LockedError.
    with pytest.raises(LockedError):
        await repo.archive("indicator", doc_id)

    # reads unaffected, mutation was blocked.
    fetched = await repo.get_by_id("indicator", doc_id)
    assert fetched.locked is True
    assert fetched.name == "EMA-20"
    assert any(d.id == doc_id for d in await repo.list_by_type("indicator"))

    # unlock, then update succeeds.
    await repo.set_locked("indicator", doc_id, locked=False)
    after = await repo.update(_replace(fetched, name="EMA-20-mutated", locked=False))
    assert after.name == "EMA-20-mutated"


async def test_signal_lock_guard(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("signal", "sig")
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
    assert stored.locked is False

    await repo.set_locked("signal", doc_id, locked=True)

    # update (incl. a category change) must raise LockedError.
    with pytest.raises(LockedError):
        await repo.update(_replace(stored, category=Category.PROD))
    with pytest.raises(LockedError):
        await repo.archive("signal", doc_id)

    fetched = await repo.get_by_id("signal", doc_id)
    assert fetched.locked is True
    assert fetched.category == Category.DEV  # change was blocked
    assert any(
        d.id == doc_id
        for d in await repo.list_by_type_and_category("signal", Category.DEV)
    )

    # unlock, then archive (soft delete) succeeds + hides the doc.
    await repo.set_locked("signal", doc_id, locked=False)
    await repo.archive("signal", doc_id)
    for cat in Category:
        lst = await repo.list_by_type_and_category("signal", cat)
        assert all(d.id != doc_id for d in lst)


async def test_portfolio_lock_guard(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("portfolio", "ptf")
    now = _now()
    doc = PortfolioDoc(
        id=doc_id,
        type="portfolio",
        name="lock-test-ptf",
        category=Category.RESEARCH,
        created_at=now,
        updated_at=now,
        legs=({"label": "SPY", "symbol": "SPY", "weight": 100},),
        rebalance="monthly",
    )
    stored = await repo.create(doc)
    assert stored.locked is False

    await repo.set_locked("portfolio", doc_id, locked=True)

    with pytest.raises(LockedError):
        await repo.update(_replace(stored, category=Category.DEV))
    with pytest.raises(LockedError):
        await repo.archive("portfolio", doc_id)

    fetched = await repo.get_by_id("portfolio", doc_id)
    assert fetched.locked is True
    assert fetched.category == Category.RESEARCH

    await repo.set_locked("portfolio", doc_id, locked=False)
    after = await repo.update(
        _replace(fetched, name="lock-test-ptf-updated", locked=False)
    )
    assert after.name == "lock-test-ptf-updated"
    assert after.locked is False


async def test_set_locked_rejects_basket(repo_with_cleanup) -> None:
    """Baskets are not lockable — ``set_locked`` rejects the type at runtime
    (and the baskets table has no ``locked`` column)."""
    repo = repo_with_cleanup.inner
    with pytest.raises(ValueError):
        await repo.set_locked("basket", "whatever", locked=True)
