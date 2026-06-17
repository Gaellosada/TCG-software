"""Integration tests: full CRUD round-trip per persisted kind against the
REAL ``tcg_app_data`` PostgreSQL schema.

Each test uses a unique ``_test-persistence-<uuid>`` id prefix so parallel
runs don't collide, exercises create → get → list → update → archive →
list (post-archive), and DELETEs every probe row in teardown (try/finally)
so NO residue is left.

Skipped automatically when the app-data credentials (``APP_DB_USER`` /
``APP_DB_PASSWORD``) are not configured.

Soft-delete oracle (uniform model)
----------------------------------
``archive`` sets the ``category`` projection column to ``'DELETED'`` for
EVERY kind. After archiving:
  * the doc disappears from every list query,
  * an archived indicator reads back with ``deleted is True``.
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
    ConcurrentUpdateError,
    DuplicateIdError,
    WriteRepository,
    load_app_db_config,
)
from tcg.persistence._pg import DEFAULT_SCHEMA
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

# Table names keyed by the singular type — for teardown DELETEs.
_TABLES = {
    "indicator": "indicators",
    "signal": "signals",
    "portfolio": "portfolios",
    "basket": "baskets",
}


@pytest.fixture
async def repo_with_cleanup():
    """Yield a real PG-backed ``WriteRepository`` and tear down every probe
    row this test wrote (by exact id, per table). The id prefix is unique
    per test so concurrent runs do not collide.
    """
    pool = AppDbConnectionPool(**load_app_db_config())
    await pool.connect()
    repo = WriteRepository(pool)
    prefix = f"_test-persistence-{uuid.uuid4().hex[:12]}"
    # Track (type, id) so teardown deletes from the right table.
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
        # Raw delete by id so an aborted test still cleans up — never used
        # in the assertion path (tests go through the public repo surface).
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    for doc_type, doc_id in created:
                        table = _TABLES[doc_type]
                        await cur.execute(
                            f"DELETE FROM {DEFAULT_SCHEMA}.{table} WHERE id = %s",
                            (doc_id,),
                        )
        finally:
            await pool.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_indicator_roundtrip(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("indicator", "ind")
    now = _now()
    doc = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="RSI-14",
        definition={"period": 14, "field": "close"},
        created_at=now,
        updated_at=now,
    )

    # 1. create
    stored = await repo.create(doc)
    assert isinstance(stored, IndicatorDoc)
    assert stored.id == doc_id
    assert stored.name == "RSI-14"
    assert stored.deleted is False

    # 2. get_by_id
    fetched = await repo.get_by_id("indicator", doc_id)
    assert isinstance(fetched, IndicatorDoc)
    assert fetched == stored

    # 3. list_by_type — present + active.
    all_active = await repo.list_by_type("indicator")
    assert any(d.id == doc_id for d in all_active)

    # 4. update — change name + definition (CAS with the stored token).
    updated_input = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="RSI-21",
        definition={"period": 21, "field": "close"},
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )
    after = await repo.update(updated_input, expected_updated_at=stored.updated_at)
    assert after.name == "RSI-21"
    assert after.definition == {"period": 21, "field": "close"}
    assert after.updated_at >= stored.updated_at
    # The returned CAS token must equal the stored value (round-trips).
    refetched = await repo.get_by_id("indicator", doc_id)
    assert refetched.updated_at == after.updated_at

    # 5. archive — soft delete (category → 'DELETED'); indicator deleted=True.
    await repo.archive("indicator", doc_id)
    archived = await repo.get_by_id("indicator", doc_id)
    assert isinstance(archived, IndicatorDoc)
    assert archived.deleted is True

    # 6. list_by_type must NOT return archived indicators.
    after_archive = await repo.list_by_type("indicator")
    assert all(d.id != doc_id for d in after_archive)


async def test_signal_roundtrip(repo_with_cleanup) -> None:
    """Full round-trip with NON-EMPTY content for every editable field."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("signal", "sig")
    now = _now()
    inputs = (
        {
            "id": "X",
            "instrument": {
                "type": "spot",
                "collection": "spot_daily",
                "instrument_id": "AAPL",
            },
        },
    )
    rules = {
        "entries": [
            {
                "id": "blk-1",
                "name": "Entry 1",
                "input_id": "X",
                "weight": 50.0,
                "conditions": [
                    {
                        "op": ">",
                        "left": {
                            "kind": "instrument",
                            "input_id": "X",
                            "field": "close",
                        },
                    }
                ],
            },
        ],
        "exits": [],
        "resets": [],
    }
    settings = {"dont_repeat": True}
    doc = SignalDoc(
        id=doc_id,
        type="signal",
        name="momentum-v1",
        category=Category.DEV,
        created_at=now,
        updated_at=now,
        inputs=inputs,
        rules=rules,
        settings=settings,
        description="Buy AAPL when close rises.",
    )

    stored = await repo.create(doc)
    assert isinstance(stored, SignalDoc)
    assert stored.inputs == inputs
    assert stored.rules == rules
    assert stored.settings == settings
    assert stored.description == "Buy AAPL when close rises."

    fetched = await repo.get_by_id("signal", doc_id)
    assert fetched == stored

    dev_list = await repo.list_by_type_and_category("signal", Category.DEV)
    assert any(d.id == doc_id for d in dev_list)

    # update: DEV → PROD, edit rules.
    promoted = SignalDoc(
        id=doc_id,
        type="signal",
        name="momentum-v1-promoted",
        category=Category.PROD,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        inputs=stored.inputs,
        rules={**rules, "resets": [{"id": "r1"}]},
        settings=stored.settings,
        description="Promoted to PROD.",
    )
    after = await repo.update(promoted, expected_updated_at=stored.updated_at)
    assert after.category == Category.PROD
    assert after.description == "Promoted to PROD."

    # ARCHIVE category is a VISIBLE user category (not soft-delete).
    to_archive_cat = SignalDoc(
        id=doc_id,
        type="signal",
        name=after.name,
        category=Category.ARCHIVE,
        created_at=after.created_at,
        updated_at=after.updated_at,
        inputs=after.inputs,
        rules=after.rules,
        settings=after.settings,
        description=after.description,
    )
    after2 = await repo.update(to_archive_cat, expected_updated_at=after.updated_at)
    assert after2.category == Category.ARCHIVE
    archive_list = await repo.list_by_type_and_category("signal", Category.ARCHIVE)
    assert any(d.id == doc_id for d in archive_list), "ARCHIVE must be visible"

    # archive() (DELETE) → soft delete: hidden from every category list.
    await repo.archive("signal", doc_id)
    for cat in Category:
        lst = await repo.list_by_type_and_category("signal", cat)
        assert all(d.id != doc_id for d in lst), f"deleted doc visible in {cat}"


async def test_portfolio_roundtrip(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("portfolio", "ptf")
    now = _now()
    legs = (
        {"label": "SPY", "type": "instrument", "symbol": "SPY", "weight": 60},
        {"label": "AGG", "type": "instrument", "symbol": "AGG", "weight": 40},
    )
    doc = PortfolioDoc(
        id=doc_id,
        type="portfolio",
        name="60-40",
        category=Category.RESEARCH,
        created_at=now,
        updated_at=now,
        legs=legs,
        rebalance="monthly",
    )

    stored = await repo.create(doc)
    assert isinstance(stored, PortfolioDoc)
    assert stored.legs == legs
    assert stored.rebalance == "monthly"

    fetched = await repo.get_by_id("portfolio", doc_id)
    assert fetched == stored

    research_list = await repo.list_by_type_and_category("portfolio", Category.RESEARCH)
    assert any(d.id == doc_id for d in research_list)

    # archive → soft delete: hidden everywhere.
    await repo.archive("portfolio", doc_id)
    for cat in Category:
        lst = await repo.list_by_type_and_category("portfolio", cat)
        assert all(d.id != doc_id for d in lst)


async def test_update_on_missing_doc_raises_keyerror(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("indicator", "missing")
    now = _now()
    phantom = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="never-created",
        definition={},
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(KeyError):
        await repo.update(phantom)


async def test_archive_on_missing_doc_raises_keyerror(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("indicator", "missing-archive")
    with pytest.raises(KeyError):
        await repo.archive("indicator", doc_id)


async def test_get_by_id_returns_none_when_missing(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("indicator", "never")
    assert await repo.get_by_id("indicator", doc_id) is None


async def test_create_duplicate_id_raises_duplicate_id_error(
    repo_with_cleanup,
) -> None:
    """Inserting twice with the same id must raise ``DuplicateIdError`` so
    the API maps it to 409 rather than letting an unhandled 500 leak."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("indicator", "dup")
    now = _now()
    doc = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="orig",
        definition={},
        created_at=now,
        updated_at=now,
    )
    await repo.create(doc)
    with pytest.raises(DuplicateIdError):
        await repo.create(doc)


async def test_update_with_stale_expected_updated_at_raises_concurrent(
    repo_with_cleanup,
) -> None:
    """A second writer whose ``expected_updated_at`` no longer matches must
    raise :class:`ConcurrentUpdateError` rather than silently overwrite."""
    from dataclasses import replace as _replace

    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("indicator", "cas")
    now = _now()
    doc = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="v0",
        definition={},
        created_at=now,
        updated_at=now,
    )
    stored = await repo.create(doc)

    after_a = await repo.update(
        _replace(stored, name="v1-by-A"),
        expected_updated_at=stored.updated_at,
    )
    assert after_a.name == "v1-by-A"

    with pytest.raises(ConcurrentUpdateError):
        await repo.update(
            _replace(stored, name="v1-by-B"),
            expected_updated_at=stored.updated_at,
        )

    refetched = await repo.get_by_id("indicator", doc_id)
    assert refetched.name == "v1-by-A"


async def test_get_by_id_does_not_cross_types(repo_with_cleanup) -> None:
    """A signal with id X must not be visible via ``get_by_id('indicator', X)``."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("signal", "cross")
    now = _now()
    sig = SignalDoc(
        id=doc_id,
        type="signal",
        name="x",
        category=Category.DEV,
        created_at=now,
        updated_at=now,
    )
    await repo.create(sig)
    assert await repo.get_by_id("indicator", doc_id) is None
    assert isinstance(await repo.get_by_id("signal", doc_id), SignalDoc)
