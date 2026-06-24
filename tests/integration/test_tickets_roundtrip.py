"""Integration test: full ticket CRUD round-trip against the REAL
``tcg_app_data.tickets`` table.

Mirrors ``test_persistence_roundtrip.py``'s gating:
  * collected only with ``--run-integration``,
  * module-level ``skipif`` when ``APP_DB_USER`` / ``APP_DB_PASSWORD`` are
    not configured.

ADDITIONALLY: the ``tickets`` table is brand-new and is applied MANUALLY
(``sql/tickets.sql``) — it may not yet exist in a given environment. The
fixture probes for it once and ``pytest.skip``s cleanly (rather than
failing) when it is absent, so this test never goes red just because the
DDL hasn't been run.

Exercises create → list (newest-first) → update → HARD delete →
list-empty, and hard-deletes any probe rows in teardown so no residue is
left.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

# Shared across the persistence integration modules — see conftest.
from conftest import _app_db_creds_present

from tcg.persistence import AppDbConnectionPool, WriteRepository, load_app_db_config
from tcg.types.persistence import TicketDoc


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _app_db_creds_present(),
        reason="APP_DB_USER / APP_DB_PASSWORD not configured",
    ),
]


@pytest.fixture
async def repo_with_cleanup():
    """Yield a real PG-backed ``WriteRepository`` plus a recorder of probe
    ticket ids, and HARD-delete each in teardown.

    Skips the test cleanly if the ``tickets`` table does not exist yet
    (DDL applied manually). The marker text is unique per test so parallel
    runs never collide.
    """
    pool = AppDbConnectionPool(**load_app_db_config())
    await pool.connect()
    repo = WriteRepository(pool)
    marker = f"_test-ticket-{uuid.uuid4().hex[:12]}"
    created_ids: list[str] = []

    # Probe once: if the table is missing, skip rather than fail.
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT 1 FROM {pool.schema}.tickets LIMIT 1")
    except psycopg.errors.UndefinedTable:
        await pool.close()
        pytest.skip(
            f"{pool.schema}.tickets table not present "
            "(run sql/tickets.sql to enable this test)"
        )

    class _Repo:
        def __init__(self) -> None:
            self.inner = repo
            self.marker = marker

        def track(self, ticket_id: str) -> str:
            created_ids.append(ticket_id)
            return ticket_id

    try:
        yield _Repo()
    finally:
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    for tid in created_ids:
                        await cur.execute(
                            f"DELETE FROM {pool.schema}.tickets WHERE id = %s",
                            (tid,),
                        )
        finally:
            await pool.close()


async def test_ticket_full_roundtrip(repo_with_cleanup) -> None:
    repo = repo_with_cleanup.inner
    marker = repo_with_cleanup.marker

    # 1. create two tickets (marker-tagged so we can find ours in the list).
    a = await repo.create_ticket(f"{marker} alpha")
    repo_with_cleanup.track(a.id)
    b = await repo.create_ticket(f"{marker} beta")
    repo_with_cleanup.track(b.id)

    assert isinstance(a, TicketDoc)
    assert a.id and b.id and a.id != b.id
    assert a.text == f"{marker} alpha"

    # 2. list — newest-first; b (created later) precedes a among ours.
    listing = await repo.list_tickets()
    ours = [t for t in listing if t.text.startswith(marker)]
    assert [t.id for t in ours] == [b.id, a.id], "expected created_at DESC ordering"

    # 3. update — in-place text replacement; created_at preserved.
    updated = await repo.update_ticket(a.id, f"{marker} alpha-edited")
    assert updated.id == a.id
    assert updated.text == f"{marker} alpha-edited"
    assert updated.created_at == a.created_at

    # 4. update a missing id → KeyError (router maps to 404).
    with pytest.raises(KeyError):
        await repo.update_ticket(f"{marker}-missing", "nope")

    # 5. HARD delete a, then b.
    await repo.delete_ticket(a.id)
    await repo.delete_ticket(b.id)

    # 6. second delete of a now-gone row → KeyError (hard delete: truly gone).
    with pytest.raises(KeyError):
        await repo.delete_ticket(a.id)

    # 7. list — none of ours remain.
    after = await repo.list_tickets()
    assert all(not t.text.startswith(marker) for t in after)
