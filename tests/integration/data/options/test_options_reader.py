"""Integration tests for ``MongoOptionsDataReader`` against real Mongo.

Gated by ``--run-integration`` (see top-level ``tests/integration/conftest.py``)
AND by ``MONGO_URI`` being set, mirroring ``test_market_data.py``.

Live Mongo is currently unreachable from the dev WSL host (PROBLEMS.md
P-2026-04-26-01); these will run once VPN access is restored.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.data import create_services
from tcg.data._mongo.registry import CollectionRegistry
from tcg.data.options.reader import MongoOptionsDataReader

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_env = dotenv_values(Path(__file__).resolve().parents[3] / ".env")
MONGO_URI = os.getenv("MONGO_URI") or _env.get("MONGO_URI", "")
MONGO_DB = (
    os.getenv("MONGO_DB_NAME") or _env.get("MONGO_DB_NAME", "tcg-instrument")
)

_skip_reason = "MONGO_URI not set -- skipping integration tests"


@pytest.fixture
async def options_reader():
    """Build a live ``MongoOptionsDataReader`` from real Mongo."""
    if not MONGO_URI:
        pytest.skip(_skip_reason)

    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]
    try:
        names = await db.list_collection_names()
    except Exception as exc:
        pytest.skip(f"MongoDB not reachable: {exc}")

    registry = CollectionRegistry(names)
    reader = MongoOptionsDataReader(db, registry)
    yield reader
    client.close()


@pytest.fixture
async def market_data_service():
    """Live ``DefaultMarketDataService`` to exercise the delegated methods."""
    if not MONGO_URI:
        pytest.skip(_skip_reason)

    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]
    try:
        await db.list_collection_names()
    except Exception as exc:
        pytest.skip(f"MongoDB not reachable: {exc}")

    services = await create_services(db)
    yield services["market_data"]
    client.close()


# ---------------------------------------------------------------------------
# Tests — query_chain
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sp500_chain_2024_03_15(options_reader):
    """SP500 chain on a liquid date should yield ≥100 rows with IV stored."""
    rows = await options_reader.query_chain(
        "OPT_SP_500",
        date(2024, 3, 15),
        "both",
        date(2024, 3, 15),
        date(2024, 6, 15),
    )
    assert len(rows) >= 100, f"Expected ≥100 chain rows, got {len(rows)}"
    iv_count = sum(1 for _c, r in rows if r.iv_stored is not None)
    ratio = iv_count / len(rows)
    assert ratio >= 0.99, f"Expected ≥99% with iv_stored, got {ratio:.3f}"
    for contract, _row in rows:
        assert contract.provider == "IVOLATILITY"


@pytest.mark.integration
async def test_vix_chain_no_greeks(options_reader):
    """OPT_VIX surfaces quotes but never Greeks — Phase 1 gating."""
    rows = await options_reader.query_chain(
        "OPT_VIX",
        date(2024, 3, 19),
        "both",
        date(2024, 3, 1),
        date(2024, 6, 30),
    )
    assert len(rows) > 0, "Expected at least one OPT_VIX row on a liquid date"
    for _contract, row in rows:
        assert row.delta_stored is None
        assert row.iv_stored is None
        assert row.gamma_stored is None
        assert row.theta_stored is None
        assert row.vega_stored is None


@pytest.mark.integration
async def test_btc_chain_internal_with_greeks(options_reader):
    """OPT_BTC uses the INTERNAL provider and carries Greeks."""
    rows = await options_reader.query_chain(
        "OPT_BTC",
        date(2024, 3, 15),
        "both",
        date(2024, 3, 1),
        date(2024, 6, 30),
    )
    assert len(rows) > 0
    has_internal = any(
        contract.provider == "INTERNAL" and row.delta_stored is not None
        for contract, row in rows
    )
    assert has_internal, "Expected at least one INTERNAL row with delta_stored"


# ---------------------------------------------------------------------------
# Tests — list_roots
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_roots_returns_ten(options_reader):
    """The legacy DB has 10 OPT_* collections per DB §6 (excluding crypto)."""
    roots = await options_reader.list_roots()
    # DB §6 names 10 OPT_* collections.
    assert len(roots) == 10, f"Expected 10 roots, got {len(roots)}: {[r.collection for r in roots]}"
    for info in roots:
        if info.expiration_first is not None and info.expiration_last is not None:
            assert info.expiration_first <= info.expiration_last
        assert info.collection.startswith("OPT_")


# ---------------------------------------------------------------------------
# Tests — get_contract
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_contract_sorted(options_reader):
    """Pull one contract from a chain query, then re-fetch it via get_contract."""
    rows = await options_reader.query_chain(
        "OPT_SP_500",
        date(2024, 3, 15),
        "both",
        date(2024, 3, 15),
        date(2024, 6, 15),
    )
    assert rows, "No SP500 rows on 2024-03-15 — re-check date"
    contract, _ = rows[0]
    series = await options_reader.get_contract("OPT_SP_500", contract.contract_id)
    assert series.contract.contract_id == contract.contract_id
    dates = [r.date for r in series.rows]
    assert dates == sorted(dates), "Rows must be chronologically sorted"


# ---------------------------------------------------------------------------
# Tests — service-level delegation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_service_query_options_chain(market_data_service):
    """The DefaultMarketDataService must surface the options methods."""
    rows = await market_data_service.query_options_chain(
        "OPT_SP_500",
        date(2024, 3, 15),
        "both",
        date(2024, 3, 15),
        date(2024, 6, 15),
    )
    assert isinstance(rows, list)
    assert len(rows) > 0


@pytest.mark.integration
async def test_service_list_option_roots(market_data_service):
    roots = await market_data_service.list_option_roots()
    assert len(roots) >= 1
