"""Integration tests for MarketDataService against live MongoDB.

These tests require a running MongoDB instance with the legacy ``tcg-instrument``
database. They are skipped unless ``MONGO_URI`` is set in the environment or
in the project ``.env`` file.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.data import create_services
from tcg.types.market import AssetClass, PriceSeries

# ---------------------------------------------------------------------------
# Configuration from .env
# ---------------------------------------------------------------------------

_env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
MONGO_URI = os.getenv("MONGO_URI") or _env.get("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB_NAME") or _env.get("MONGO_DB_NAME", "tcg-instrument")

_skip_reason = "MONGO_URI not set -- skipping integration tests"


@pytest.fixture
async def market_data():
    """Build a live MarketDataService from the real MongoDB."""
    if not MONGO_URI:
        pytest.skip(_skip_reason)

    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    # Verify connectivity
    try:
        await db.list_collection_names()
    except Exception as exc:
        pytest.skip(f"MongoDB not reachable: {exc}")

    services = await create_services(db)
    yield services["market_data"]

    client.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_collections_returns_non_empty(market_data):
    collections = await market_data.list_collections()
    assert len(collections) > 0, "Expected at least one collection"


@pytest.mark.integration
async def test_list_collections_filter_by_index(market_data):
    collections = await market_data.list_collections(AssetClass.INDEX)
    # Legacy DB should have an INDEX collection
    assert "INDEX" in collections


@pytest.mark.integration
async def test_list_instruments_index(market_data):
    result = await market_data.list_instruments("INDEX", limit=10)
    assert result.total > 0, "Expected instruments in INDEX collection"
    assert len(result.items) > 0
    assert len(result.items) <= 10
    # All instruments should be classified as INDEX
    for inst in result.items:
        assert inst.asset_class == AssetClass.INDEX
        assert inst.collection == "INDEX"


@pytest.mark.integration
async def test_get_prices_known_instrument(market_data):
    """Fetch prices for IND_SP_500 -- a well-known index in the legacy DB."""
    series = await market_data.get_prices("INDEX", "IND_SP_500")
    if series is None:
        pytest.skip("IND_SP_500 not found in INDEX collection -- legacy DB may use different IDs")

    assert isinstance(series, PriceSeries)
    assert len(series) > 0
    # Dates should be sorted ascending
    assert all(
        series.dates[i] <= series.dates[i + 1]
        for i in range(len(series) - 1)
    ), "Dates not sorted"
    # No NaN in close prices (sanitization contract)
    import numpy as np

    assert not np.any(np.isnan(series.close)), "Found NaN in close prices"


@pytest.mark.integration
async def test_get_prices_nonexistent_returns_none(market_data):
    result = await market_data.get_prices("INDEX", "DEFINITELY_NOT_A_REAL_INSTRUMENT_XYZ")
    assert result is None


@pytest.mark.integration
async def test_get_prices_date_range_filtering(market_data):
    """Fetch prices with a date range and verify filtering."""
    # First get full series to check we have data
    full = await market_data.get_prices("INDEX", "IND_SP_500")
    if full is None or len(full) < 10:
        pytest.skip("Need IND_SP_500 with at least 10 bars for date range test")

    # Pick a date range from the middle of the series
    mid = len(full) // 2
    start_int = int(full.dates[mid])
    end_int = int(full.dates[mid + 5]) if mid + 5 < len(full) else int(full.dates[-1])

    start_date = date(start_int // 10000, (start_int % 10000) // 100, start_int % 100)
    end_date = date(end_int // 10000, (end_int % 10000) // 100, end_int % 100)

    filtered = await market_data.get_prices(
        "INDEX", "IND_SP_500", start=start_date, end=end_date
    )
    assert filtered is not None
    assert len(filtered) <= len(full)
    assert all(d >= start_int for d in filtered.dates)
    assert all(d <= end_int for d in filtered.dates)


@pytest.mark.integration
async def test_cache_hit_same_object(market_data):
    """Second call for the same instrument should return cached object."""
    series1 = await market_data.get_prices("INDEX", "IND_SP_500")
    if series1 is None:
        pytest.skip("IND_SP_500 not found in INDEX collection")

    series2 = await market_data.get_prices("INDEX", "IND_SP_500")
    assert series2 is series1, "Expected same object from cache"
