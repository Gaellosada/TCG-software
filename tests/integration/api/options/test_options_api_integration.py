"""Integration tests for the options router against real Mongo.

Gated by ``--run-integration`` (top-level ``tests/integration/conftest.py``)
AND by ``MONGO_URI`` being set (mirrors ``tests/integration/data/options/
test_options_reader.py``).

Live Mongo is currently unreachable from the dev WSL host (PROBLEMS.md
P-2026-04-26-01); these will run once VPN access is restored.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.core.app import create_app
from tcg.data import create_services


pytestmark = pytest.mark.integration


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
async def live_client():
    """Build a TestClient with a live ``MarketDataService`` from real Mongo."""
    if not MONGO_URI:
        pytest.skip(_skip_reason)

    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]
    try:
        await db.list_collection_names()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB not reachable: {exc}")

    services = await create_services(db)
    app = create_app()
    app.state.market_data = services["market_data"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    client.close()


# ---------------------------------------------------------------------------
# /roots
# ---------------------------------------------------------------------------


async def test_roots_returns_all_expected_options(live_client: AsyncClient):
    """``/api/options/roots`` returns the 10 expected OPT_* roots.

    The legacy schema has 10 OPT_* collections (DB_SCHEMA_FINDINGS §6).
    """
    resp = await live_client.get("/api/options/roots")
    assert resp.status_code == 200
    body = resp.json()
    roots = {r["collection"] for r in body["roots"]}
    expected = {
        "OPT_SP_500",
        "OPT_NASDAQ_100",
        "OPT_GOLD",
        "OPT_BTC",
        "OPT_ETH",
        "OPT_VIX",
        "OPT_T_NOTE_10_Y",
        "OPT_T_BOND",
        "OPT_EURUSD",
        "OPT_JPYUSD",
    }
    # Allow extras (registry-driven), but require all expected roots.
    missing = expected - roots
    assert not missing, f"Missing OPT_* roots: {missing}"


# ---------------------------------------------------------------------------
# /chain
# ---------------------------------------------------------------------------


async def test_chain_sp500_2024_03_15_has_rows(live_client: AsyncClient):
    """A reference chain query returns at least 100 rows (broad SP_500 chain)."""
    resp = await live_client.get(
        "/api/options/chain",
        params={
            "root": "OPT_SP_500",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-03-15",
            "expiration_max": "2024-12-31",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) >= 100


async def test_chain_opt_vix_compute_missing_returns_block_reason(
    live_client: AsyncClient,
):
    """OPT_VIX with ``compute_missing=true`` → all Greeks missing with
    ``error_code="missing_forward_vix_curve"`` (guardrail #6).
    """
    resp = await live_client.get(
        "/api/options/chain",
        params={
            "root": "OPT_VIX",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-03-15",
            "expiration_max": "2024-06-30",
            "compute_missing": "true",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    if not body["rows"]:
        pytest.skip("OPT_VIX chain empty for the chosen date")
    for row in body["rows"]:
        for greek in ("iv", "delta", "gamma", "theta", "vega"):
            cr = row[greek]
            assert cr["source"] == "missing"
            assert cr["error_code"] == "missing_forward_vix_curve"
