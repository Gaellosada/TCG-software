"""Integration tests for the core API endpoints against live MongoDB.

These tests create a real FastAPI app, trigger its lifespan (which connects
to MongoDB), and exercise the three data endpoints via httpx.AsyncClient.

Skipped unless ``--run-integration`` is passed and ``MONGO_URI`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient

from tcg.core.app import create_app

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
MONGO_URI = os.getenv("MONGO_URI") or _env.get("MONGO_URI", "")


@pytest.fixture
async def client():
    """Create a test client with the full app lifespan triggered."""
    if not MONGO_URI:
        pytest.skip("MONGO_URI not set -- skipping API integration tests")

    app = create_app()

    # Manually trigger the lifespan context manager so that
    # app.state.market_data is populated before requests.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_collections(client: AsyncClient):
    """GET /api/data/collections returns 200 with a non-empty list."""
    resp = await client.get("/api/data/collections")
    assert resp.status_code == 200
    body = resp.json()
    assert "collections" in body
    assert len(body["collections"]) > 0


@pytest.mark.integration
async def test_list_collections_with_asset_class_filter(client: AsyncClient):
    """GET /api/data/collections?asset_class=index returns filtered results."""
    resp = await client.get("/api/data/collections", params={"asset_class": "index"})
    assert resp.status_code == 200
    body = resp.json()
    assert "INDEX" in body["collections"]


@pytest.mark.integration
async def test_list_instruments(client: AsyncClient):
    """GET /api/data/INDEX returns 200 with paginated instruments."""
    resp = await client.get("/api/data/INDEX", params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert "skip" in body
    assert "limit" in body
    assert len(body["items"]) > 0
    # Verify instrument structure
    item = body["items"][0]
    assert "symbol" in item
    assert "asset_class" in item
    assert "collection" in item
    assert "exchange" in item


@pytest.mark.integration
async def test_list_instruments_nonexistent_collection(client: AsyncClient):
    """GET /api/data/NONEXISTENT_COLLECTION returns 404."""
    resp = await client.get("/api/data/NONEXISTENT_COLLECTION_XYZ")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_type"] == "data_not_found"
    assert "message" in body


@pytest.mark.integration
async def test_get_prices(client: AsyncClient):
    """GET /api/data/INDEX/IND_SP_500 returns 200 with OHLCV arrays."""
    resp = await client.get("/api/data/INDEX/IND_SP_500")
    if resp.status_code == 404:
        pytest.skip("IND_SP_500 not found -- legacy DB may use different IDs")

    assert resp.status_code == 200
    body = resp.json()
    for field in ("dates", "open", "high", "low", "close", "volume"):
        assert field in body, f"Missing field: {field}"
        assert isinstance(body[field], list), f"{field} should be a list"
        assert len(body[field]) > 0, f"{field} should not be empty"

    # All arrays same length
    lengths = {len(body[f]) for f in ("dates", "open", "high", "low", "close", "volume")}
    assert len(lengths) == 1, f"Array lengths differ: {lengths}"


@pytest.mark.integration
async def test_get_prices_nonexistent_instrument(client: AsyncClient):
    """GET /api/data/INDEX/NONEXISTENT returns 404 with error_type."""
    resp = await client.get("/api/data/INDEX/DEFINITELY_NOT_REAL_XYZ")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_type"] == "data_not_found"
    assert "message" in body


@pytest.mark.integration
async def test_get_prices_with_date_range(client: AsyncClient):
    """GET /api/data/INDEX/IND_SP_500?start=...&end=... filters by date."""
    resp = await client.get(
        "/api/data/INDEX/IND_SP_500",
        params={"start": "2020-01-01", "end": "2020-12-31"},
    )
    if resp.status_code == 404:
        pytest.skip("IND_SP_500 not found")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["dates"]) > 0
    # All dates should be in 2020 (YYYYMMDD int format)
    for d in body["dates"]:
        assert 20200101 <= d <= 20201231, f"Date {d} outside expected range"
