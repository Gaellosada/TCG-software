"""Integration test: app-data persistence survives a process restart.

This is the cross-process durability regression test for PR #58. The
app-data store is PostgreSQL (schema ``tcg_app_data``) and the pool runs
``autocommit=True`` (``tcg/persistence/_pg.py``), so every CRUD statement
commits immediately and the pool holds NO in-memory cache — the database
is the only store. A real app restart therefore drops the in-process pool
but keeps the rows. We simulate that restart IN-PROCESS:

  1. Open pool #1 + ``WriteRepository``; write one doc of EACH kind
     (indicator / signal / portfolio / basket), including a LOCKED one, a
     NON-DEFAULT-category one, and a SOFT-DELETED one.
  2. Fully ``close()`` pool #1 (drops every connection — the "stop").
  3. Open a SECOND, FRESH pool #2 + a new ``WriteRepository`` (the
     "relaunch"); assert each record reads back IDENTICAL: payload, type,
     category, ``locked`` flag, and soft-delete state.

Soft-delete oracle (uniform model — see ``WriteRepository.archive`` and
``tcg.types.persistence``): ``archive`` sets the ``category`` PROJECTION
column to ``'DELETED'`` (and, for indicators only, flips
``payload.deleted = true``). It does NOT rewrite ``payload.category`` for
signals/portfolios/baskets. ``from_pg_row`` reconstructs the dataclass
from the PAYLOAD, ignoring the projection columns, so after a restart a
soft-deleted doc:
  * is HIDDEN from every list query (projection ``category='DELETED'``
    is excluded), yet
  * is still reachable via ``get_by_id`` with its ORIGINAL payload
    category intact (non-indicator) / ``deleted is True`` (indicator).
That dual fact — gone from lists, state preserved on direct fetch — is
what "the soft-delete survived the restart" means here.

Skipped automatically when the app-data credentials (``APP_DB_USER`` /
``APP_DB_PASSWORD``) are not configured, and (like every integration
test) only collected under ``--run-integration``.

Determinism / no residue: every probe id uses a stable, unique
``_test-durability-<uuid>`` prefix (the uuid is generated once at module
import, NOT per call, so the two pools address the SAME ids). A teardown
deletes every probe row by exact id from its table via a THIRD short-lived
connection, so neither pool needs to outlive the assertions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

# Shared across the four persistence integration modules — see conftest.
from conftest import _app_db_creds_present

from tcg.persistence import (
    AppDbConnectionPool,
    WriteRepository,
    load_app_db_config,
)
from tcg.persistence._pg import DEFAULT_SCHEMA
from tcg.types.persistence import (
    BasketDoc,
    Category,
    IndicatorDoc,
    PortfolioDoc,
    SignalDoc,
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _app_db_creds_present(),
        reason="APP_DB_USER / APP_DB_PASSWORD not configured",
    ),
]

# Singular type → plural table name (matches WriteRepository._TABLE_BY_TYPE),
# used only for raw teardown DELETEs.
_TABLES = {
    "indicator": "indicators",
    "signal": "signals",
    "portfolio": "portfolios",
    "basket": "baskets",
}

# One stable prefix per test process so pool #1 and pool #2 address the
# SAME rows. Generated at import time (not per call) — deterministic
# across the two pools within a run, unique across parallel runs.
_PREFIX = f"_test-durability-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _delete_probe_rows(ids_by_type: list[tuple[str, str]]) -> None:
    """Hard-delete every probe row by exact id, via a fresh short-lived pool.

    Runs in a ``finally`` so an aborted assertion still cleans up. Uses raw
    DELETE (never the public archive path) precisely because the point of
    the test is to leave the shared store pristine, including the rows we
    soft-deleted (archive only hides them — it does not remove them).
    """
    pool = AppDbConnectionPool(**load_app_db_config())
    await pool.connect()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for doc_type, doc_id in ids_by_type:
                    await cur.execute(
                        f"DELETE FROM {DEFAULT_SCHEMA}.{_TABLES[doc_type]} "
                        "WHERE id = %s",
                        (doc_id,),
                    )
    finally:
        await pool.close()


async def test_persistence_survives_restart() -> None:
    """A FRESH pool reads back what a PRIOR (now-closed) pool wrote.

    Covers all four kinds plus the three state dimensions that must
    survive a restart: a LOCKED doc, a NON-DEFAULT-category doc, and a
    SOFT-DELETED doc.
    """
    now = _now()

    # Probe ids (stable across both pools). Track (type, id) for teardown.
    ind_id = f"{_PREFIX}-indicator"  # soft-deleted (DELETE path)
    sig_id = f"{_PREFIX}-signal-locked"  # locked + DEV
    ptf_id = f"{_PREFIX}-portfolio-archived"  # ARCHIVE (visible, non-default)
    bsk_id = f"{_PREFIX}-basket"  # plain RESEARCH basket
    created: list[tuple[str, str]] = [
        ("indicator", ind_id),
        ("signal", sig_id),
        ("portfolio", ptf_id),
        ("basket", bsk_id),
    ]

    # Rich, non-trivial payloads so the round-trip proves CONTENT survives,
    # not just the keys.
    indicator_def = {"period": 14, "field": "close", "params": {"smoothing": "wilder"}}
    signal_inputs = (
        {
            "id": "X",
            "instrument": {
                "type": "spot",
                "collection": "spot_daily",
                "instrument_id": "AAPL",
            },
        },
    )
    signal_rules = {
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
            }
        ],
        "exits": [],
        "resets": [],
    }
    signal_settings = {"dont_repeat": True}
    portfolio_legs = (
        {"label": "SPY", "type": "instrument", "symbol": "SPY", "weight": 60},
        {"label": "AGG", "type": "instrument", "symbol": "AGG", "weight": 40},
    )
    basket_legs = (
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "SPY"},
            "weight": 0.6,
        },
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "QQQ"},
            "weight": 0.4,
        },
    )

    # Expected post-write snapshots, captured from pool #1's stored copies.
    expected_indicator: IndicatorDoc
    expected_signal: SignalDoc
    expected_portfolio: PortfolioDoc
    expected_basket: BasketDoc

    try:
        # ----------------------------------------------------------------
        # POOL #1 — write everything, then close (the "stop").
        # ----------------------------------------------------------------
        pool1 = AppDbConnectionPool(**load_app_db_config())
        await pool1.connect()
        repo1 = WriteRepository(pool1)

        # indicator — created active, then SOFT-DELETED (archive).
        expected_indicator = await repo1.create(
            IndicatorDoc(
                id=ind_id,
                type="indicator",
                name="RSI-14",
                definition=indicator_def,
                created_at=now,
                updated_at=now,
            )
        )
        assert expected_indicator.deleted is False

        # signal — DEV, then LOCKED.
        created_signal = await repo1.create(
            SignalDoc(
                id=sig_id,
                type="signal",
                name="momentum-v1",
                category=Category.DEV,
                created_at=now,
                updated_at=now,
                inputs=signal_inputs,
                rules=signal_rules,
                settings=signal_settings,
                description="Buy AAPL when close rises.",
            )
        )
        assert created_signal.locked is False

        # portfolio — NON-DEFAULT category (ARCHIVE is a visible category,
        # NOT soft-delete).
        expected_portfolio = await repo1.create(
            PortfolioDoc(
                id=ptf_id,
                type="portfolio",
                name="60-40",
                category=Category.ARCHIVE,
                created_at=now,
                updated_at=now,
                legs=portfolio_legs,
                rebalance="monthly",
            )
        )
        assert expected_portfolio.category == Category.ARCHIVE

        # basket — RESEARCH.
        expected_basket = await repo1.create(
            BasketDoc(
                id=bsk_id,
                type="basket",
                name="Integration Basket",
                category=Category.RESEARCH,
                asset_class="equity",
                created_at=now,
                updated_at=now,
                legs=basket_legs,
            )
        )

        # Lock the signal (the LOCKED dimension). set_locked returns the
        # re-read doc — capture it as the post-write expectation.
        expected_signal = await repo1.set_locked("signal", sig_id, locked=True)
        assert expected_signal.locked is True

        # Soft-delete the indicator (the SOFT-DELETED dimension). After
        # this it must vanish from lists but remain fetchable with
        # deleted=True. Re-read so our expectation carries the post-archive
        # state (deleted flag + bumped updated_at).
        await repo1.archive("indicator", ind_id)
        archived_indicator = await repo1.get_by_id("indicator", ind_id)
        assert isinstance(archived_indicator, IndicatorDoc)
        assert archived_indicator.deleted is True
        expected_indicator = archived_indicator

        # Stop: fully drop pool #1's connections.
        await pool1.close()
        assert pool1.is_open is False

        # ----------------------------------------------------------------
        # POOL #2 — FRESH pool + repo (the "relaunch"). Reads only.
        # ----------------------------------------------------------------
        pool2 = AppDbConnectionPool(**load_app_db_config())
        # The "restart" premise made executable: pool #2 is a genuinely
        # distinct object, NOT pool #1 reused — so a read-back proves the
        # DATABASE persisted the rows, not an in-process pool cache.
        assert pool2 is not pool1
        await pool2.connect()
        repo2 = WriteRepository(pool2)
        try:
            # --- indicator: SOFT-DELETED state survived ------------------
            ind_back = await repo2.get_by_id("indicator", ind_id)
            assert isinstance(ind_back, IndicatorDoc)
            # Identical object equality: payload content + type + timestamps
            # + locked + deleted all match the pre-restart stored copy.
            assert ind_back == expected_indicator
            assert ind_back.definition == indicator_def
            assert ind_back.deleted is True  # soft-delete state preserved
            assert ind_back.locked is False
            # Hidden from the active list despite still existing.
            active = await repo2.list_by_type("indicator")
            assert all(d.id != ind_id for d in active), (
                "soft-deleted indicator must stay hidden from list_by_type "
                "after a restart"
            )

            # --- signal: LOCKED + DEV category survived ------------------
            sig_back = await repo2.get_by_id("signal", sig_id)
            assert isinstance(sig_back, SignalDoc)
            assert sig_back == expected_signal
            assert sig_back.locked is True  # lock flag survived
            assert sig_back.category == Category.DEV
            assert sig_back.inputs == signal_inputs  # opaque payload survived
            assert sig_back.rules == signal_rules
            assert sig_back.settings == signal_settings
            assert sig_back.description == "Buy AAPL when close rises."
            # Visible in its real category list across the restart.
            dev_signals = await repo2.list_by_type_and_category("signal", Category.DEV)
            assert any(d.id == sig_id for d in dev_signals)

            # --- portfolio: NON-DEFAULT (ARCHIVE) category survived ------
            ptf_back = await repo2.get_by_id("portfolio", ptf_id)
            assert isinstance(ptf_back, PortfolioDoc)
            assert ptf_back == expected_portfolio
            assert ptf_back.category == Category.ARCHIVE  # non-default survived
            assert ptf_back.legs == portfolio_legs
            assert ptf_back.rebalance == "monthly"
            assert ptf_back.locked is False
            # ARCHIVE is a VISIBLE category (not soft-delete): listed there,
            # and NOT in RESEARCH.
            archive_ptfs = await repo2.list_by_type_and_category(
                "portfolio", Category.ARCHIVE
            )
            assert any(d.id == ptf_id for d in archive_ptfs), (
                "ARCHIVE portfolio must remain visible after a restart"
            )
            research_ptfs = await repo2.list_by_type_and_category(
                "portfolio", Category.RESEARCH
            )
            assert all(d.id != ptf_id for d in research_ptfs)

            # --- basket: plain RESEARCH survived -------------------------
            bsk_back = await repo2.get_by_id("basket", bsk_id)
            assert isinstance(bsk_back, BasketDoc)
            assert bsk_back == expected_basket
            assert bsk_back.category == Category.RESEARCH
            assert bsk_back.asset_class == "equity"
            assert bsk_back.legs == basket_legs
            research_baskets = await repo2.list_by_type_and_category(
                "basket", Category.RESEARCH
            )
            assert any(d.id == bsk_id for d in research_baskets)
        finally:
            await pool2.close()
    finally:
        # Hard-remove every probe row (incl. the soft-deleted one, which
        # archive only hides) so the shared store is left pristine.
        await _delete_probe_rows(created)
