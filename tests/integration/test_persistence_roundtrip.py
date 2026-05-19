"""Integration tests: full CRUD round-trip per persisted type.

Each test creates a doc with a unique ``_test-persistence-<uuid>`` id
prefix so parallel runs don't collide, then exercises
get → list → update → archive → list (post-archive) and finally
deletes the probe docs in teardown.

Skipped automatically when ``MONGO_APP_WRITE_URI`` is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tcg.persistence import WriteRepository, build_write_client
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
    test wrote. The ID prefix is unique per test so concurrent runs
    do not collide.
    """
    # Use the production factory so we exercise the same tz_aware /
    # timeout settings as the live wiring.
    client = build_write_client()
    repo = WriteRepository(client)
    # Probe ID prefix — caller appends suffixes.
    prefix = f"_test-persistence-{uuid.uuid4().hex[:12]}"
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
        # Tidy up: raw delete by _id so an aborted test still cleans.
        # We use the underlying collection handle through ``repo._coll``
        # only here, in teardown — never in the assertion path. Tests
        # operate strictly through the public WriteRepository surface.
        coll = repo._coll
        if created_ids:
            await coll.delete_many({"_id": {"$in": created_ids}})
        client.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_indicator_roundtrip(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("ind")
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
    # Repository stamps timestamps server-side; they should be very recent.
    assert (stored.updated_at - now).total_seconds() < 30

    # 2. get_by_id
    fetched = await repo.get_by_id("indicator", doc_id)
    assert isinstance(fetched, IndicatorDoc)
    assert fetched == stored

    # 3. list_by_type — our doc must be present (and active).
    all_active = await repo.list_by_type("indicator")
    assert any(d.id == doc_id for d in all_active)

    # 4. update — change the name + definition.
    updated_input = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name="RSI-21",
        definition={"period": 21, "field": "close"},
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )
    after = await repo.update(updated_input)
    assert after.name == "RSI-21"
    assert after.definition == {"period": 21, "field": "close"}
    assert after.updated_at >= stored.updated_at

    # 5. archive — sets deleted=True.
    await repo.archive("indicator", doc_id)
    archived = await repo.get_by_id("indicator", doc_id)
    assert isinstance(archived, IndicatorDoc)
    assert archived.deleted is True

    # 6. list_by_type must NOT return archived indicators.
    after_archive = await repo.list_by_type("indicator")
    assert all(d.id != doc_id for d in after_archive)


async def test_signal_roundtrip(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("sig")
    now = _now()
    doc = SignalDoc(
        id=doc_id,
        type="signal",
        name="momentum-v1",
        blocks=[
            {"id": "entry", "weight": 1.0, "conditions": [{"k": "rsi<30"}]},
        ],
        category=Category.DEV,
        created_at=now,
        updated_at=now,
    )

    stored = await repo.create(doc)
    assert isinstance(stored, SignalDoc)

    fetched = await repo.get_by_id("signal", doc_id)
    assert fetched == stored

    dev_list = await repo.list_by_type_and_category("signal", Category.DEV)
    assert any(d.id == doc_id for d in dev_list)

    # update: promote DEV → PROD and add a block.
    promoted = SignalDoc(
        id=doc_id,
        type="signal",
        name="momentum-v1-promoted",
        blocks=stored.blocks + [{"id": "exit", "weight": 1.0}],
        category=Category.PROD,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )
    after = await repo.update(promoted)
    assert after.category == Category.PROD
    assert len(after.blocks) == 2

    # archive: category → ARCHIVE.
    await repo.archive("signal", doc_id)
    archived = await repo.get_by_id("signal", doc_id)
    assert isinstance(archived, SignalDoc)
    assert archived.category == Category.ARCHIVE

    # The DEV list no longer contains us; the ARCHIVE list does.
    dev_after = await repo.list_by_type_and_category("signal", Category.DEV)
    assert all(d.id != doc_id for d in dev_after)
    arch_after = await repo.list_by_type_and_category(
        "signal", Category.ARCHIVE
    )
    assert any(d.id == doc_id for d in arch_after)


async def test_portfolio_roundtrip(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("ptf")
    now = _now()
    doc = PortfolioDoc(
        id=doc_id,
        type="portfolio",
        name="60-40",
        instruments=[
            {"symbol": "SPY", "weight": 0.6},
            {"symbol": "AGG", "weight": 0.4},
        ],
        rebalance={"freq": "monthly"},
        category=Category.RESEARCH,
        created_at=now,
        updated_at=now,
    )

    stored = await repo.create(doc)
    assert isinstance(stored, PortfolioDoc)

    fetched = await repo.get_by_id("portfolio", doc_id)
    assert fetched == stored

    research_list = await repo.list_by_type_and_category(
        "portfolio", Category.RESEARCH
    )
    assert any(d.id == doc_id for d in research_list)

    # update: swap to a different rebalance freq, move to DEV.
    rev = PortfolioDoc(
        id=doc_id,
        type="portfolio",
        name="60-40-quarterly",
        instruments=stored.instruments,
        rebalance={"freq": "quarterly"},
        category=Category.DEV,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )
    after = await repo.update(rev)
    assert after.rebalance == {"freq": "quarterly"}
    assert after.category == Category.DEV

    # archive.
    await repo.archive("portfolio", doc_id)
    archived = await repo.get_by_id("portfolio", doc_id)
    assert isinstance(archived, PortfolioDoc)
    assert archived.category == Category.ARCHIVE


async def test_update_on_missing_doc_raises_keyerror(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("missing")
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
    doc_id = repo_with_cleanup.id("missing-archive")
    with pytest.raises(KeyError):
        await repo.archive("indicator", doc_id)


async def test_get_by_id_returns_none_when_missing(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("never")
    result = await repo.get_by_id("indicator", doc_id)
    assert result is None


async def test_get_by_id_does_not_cross_types(repo_with_cleanup) -> None:
    """Inserting a signal with id X must not be visible via
    ``get_by_id('indicator', X)``."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("cross")
    now = _now()
    sig = SignalDoc(
        id=doc_id,
        type="signal",
        name="x",
        blocks=[],
        category=Category.DEV,
        created_at=now,
        updated_at=now,
    )
    await repo.create(sig)
    fetched_as_indicator = await repo.get_by_id("indicator", doc_id)
    assert fetched_as_indicator is None
    fetched_as_signal = await repo.get_by_id("signal", doc_id)
    assert isinstance(fetched_as_signal, SignalDoc)
