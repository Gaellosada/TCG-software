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

import pymongo.errors

from tcg.core.config import load_config
from tcg.persistence import (
    ConcurrentUpdateError,
    WriteRepository,
    build_write_client,
)
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
    cfg = load_config()
    client = build_write_client()
    repo = WriteRepository(
        client,
        db_name=cfg.app_write_db_name,
        collection_name=cfg.app_write_collection,
    )
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
    """Full round-trip with NON-EMPTY content for every editable field.

    Regression coverage for the bug where the persistence wiring only
    stored ``name + category`` — rules/inputs/description survived
    only in localStorage, not the database.
    """
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("sig")
    now = _now()
    inputs = (
        {"id": "X", "instrument": {"type": "spot", "collection": "spot_daily", "instrument_id": "AAPL"}},
    )
    rules = {
        "entries": [
            {"id": "blk-1", "name": "Entry 1", "input_id": "X", "weight": 50.0,
             "conditions": [{"op": ">", "left": {"kind": "instrument", "input_id": "X", "field": "close"}}]},
        ],
        "exits": [
            {"id": "blk-2", "name": "Exit 1", "target_entry_block_name": "Entry 1",
             "conditions": [{"op": "<", "left": {"kind": "constant", "value": 0}}]},
        ],
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
    # Crucial: every editable field round-trips intact.
    assert stored.inputs == inputs
    assert stored.rules == rules
    assert stored.settings == settings
    assert stored.description == "Buy AAPL when close rises."

    fetched = await repo.get_by_id("signal", doc_id)
    assert fetched == stored
    assert isinstance(fetched, SignalDoc)
    assert fetched.inputs == inputs
    assert fetched.rules == rules
    assert fetched.settings == settings
    assert fetched.description == "Buy AAPL when close rises."

    dev_list = await repo.list_by_type_and_category("signal", Category.DEV)
    assert any(d.id == doc_id for d in dev_list)

    # update: promote DEV → PROD and edit the rules.
    new_rules = {
        **rules,
        "entries": rules["entries"] + [
            {"id": "blk-3", "name": "Entry 2", "input_id": "X", "weight": 25.0, "conditions": []},
        ],
    }
    promoted = SignalDoc(
        id=doc_id,
        type="signal",
        name="momentum-v1-promoted",
        category=Category.PROD,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        inputs=stored.inputs,
        rules=new_rules,
        settings=stored.settings,
        description="Promoted to PROD.",
    )
    after = await repo.update(promoted)
    assert after.category == Category.PROD
    assert len(after.rules["entries"]) == 2
    assert after.description == "Promoted to PROD."

    # Re-fetch to make sure the update landed.
    refetched = await repo.get_by_id("signal", doc_id)
    assert isinstance(refetched, SignalDoc)
    assert len(refetched.rules["entries"]) == 2
    assert refetched.description == "Promoted to PROD."

    # archive: category → ARCHIVE.
    await repo.archive("signal", doc_id)
    archived = await repo.get_by_id("signal", doc_id)
    assert isinstance(archived, SignalDoc)
    assert archived.category == Category.ARCHIVE
    # Archive must NOT wipe the editable content.
    assert archived.rules == new_rules

    # The DEV list no longer contains us; the ARCHIVE list does.
    dev_after = await repo.list_by_type_and_category("signal", Category.DEV)
    assert all(d.id != doc_id for d in dev_after)
    arch_after = await repo.list_by_type_and_category(
        "signal", Category.ARCHIVE
    )
    assert any(d.id == doc_id for d in arch_after)


async def test_portfolio_roundtrip(repo_with_cleanup) -> None:
    """Full round-trip with NON-EMPTY content for every editable field."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("ptf")
    now = _now()
    legs = (
        {"label": "SPY", "type": "instrument", "collection": "spot_daily",
         "symbol": "SPY", "weight": 60},
        {"label": "AGG", "type": "instrument", "collection": "spot_daily",
         "symbol": "AGG", "weight": 40},
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
    assert isinstance(fetched, PortfolioDoc)
    assert fetched.legs == legs
    assert fetched.rebalance == "monthly"

    research_list = await repo.list_by_type_and_category(
        "portfolio", Category.RESEARCH
    )
    assert any(d.id == doc_id for d in research_list)

    # update: swap to a different rebalance freq + add a leg, move to DEV.
    new_legs = legs + (
        {"label": "BIL", "type": "instrument", "collection": "spot_daily",
         "symbol": "BIL", "weight": 10},
    )
    rev = PortfolioDoc(
        id=doc_id,
        type="portfolio",
        name="60-40-quarterly",
        category=Category.DEV,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        legs=new_legs,
        rebalance="quarterly",
    )
    after = await repo.update(rev)
    assert after.rebalance == "quarterly"
    assert after.category == Category.DEV
    assert len(after.legs) == 3

    # Re-fetch confirms persistence of editable content.
    refetched = await repo.get_by_id("portfolio", doc_id)
    assert isinstance(refetched, PortfolioDoc)
    assert refetched.legs == new_legs
    assert refetched.rebalance == "quarterly"

    # archive.
    await repo.archive("portfolio", doc_id)
    archived = await repo.get_by_id("portfolio", doc_id)
    assert isinstance(archived, PortfolioDoc)
    assert archived.category == Category.ARCHIVE
    assert archived.legs == new_legs  # archive preserves content


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


async def test_create_duplicate_id_raises_duplicate_key_error(
    repo_with_cleanup,
) -> None:
    """B1 regression — inserting twice with the same ``_id`` must raise
    ``pymongo.errors.DuplicateKeyError`` so the API layer can map it
    to 409 rather than letting an unhandled 500 leak out."""
    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("dup")
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
    # Second insert with same id MUST raise (not silently upsert / 500).
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        await repo.create(doc)


async def test_update_with_stale_expected_updated_at_raises_concurrent(
    repo_with_cleanup,
) -> None:
    """M4 regression — when two writers read the same doc, the second
    PUT (whose ``expected_updated_at`` no longer matches) must raise
    :class:`ConcurrentUpdateError` rather than silently overwrite the
    first writer's edits."""
    from dataclasses import replace as _replace

    repo = repo_with_cleanup.inner
    doc_id = repo_with_cleanup.id("cas")
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

    # Writer A reads then writes — succeeds, ``updated_at`` advances.
    after_a = await repo.update(
        _replace(stored, name="v1-by-A"),
        expected_updated_at=stored.updated_at,
    )
    assert after_a.name == "v1-by-A"

    # Writer B has the stale ``stored.updated_at`` — its CAS must miss
    # and raise.
    with pytest.raises(ConcurrentUpdateError):
        await repo.update(
            _replace(stored, name="v1-by-B"),
            expected_updated_at=stored.updated_at,
        )

    # And the doc must still reflect Writer A's value.
    refetched = await repo.get_by_id("indicator", doc_id)
    assert isinstance(refetched, IndicatorDoc)
    assert refetched.name == "v1-by-A"


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
        category=Category.DEV,
        created_at=now,
        updated_at=now,
    )
    await repo.create(sig)
    fetched_as_indicator = await repo.get_by_id("indicator", doc_id)
    assert fetched_as_indicator is None
    fetched_as_signal = await repo.get_by_id("signal", doc_id)
    assert isinstance(fetched_as_signal, SignalDoc)
