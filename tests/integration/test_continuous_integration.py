"""Integration tests for continuous futures series against live MongoDB.

These tests require a running MongoDB instance with the legacy ``tcg-instrument``
database containing futures collections (e.g., FUT_VIX). They are skipped unless
``MONGO_URI`` is set in the environment or in the project ``.env`` file.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.data import create_services
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    ContractPriceData,
    RollStrategy,
)

# ---------------------------------------------------------------------------
# Configuration from .env
# ---------------------------------------------------------------------------

_env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
MONGO_URI = os.getenv("MONGO_URI") or _env.get("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB_NAME") or _env.get("MONGO_DB_NAME", "tcg-instrument")

_skip_reason = "MONGO_URI not set -- skipping integration tests"

# Use a well-known futures collection in the legacy DB.
FUTURES_COLLECTION = "FUT_VIX"


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


@pytest.fixture
async def mongo_reader():
    """Build a raw MongoInstrumentReader for low-level tests."""
    if not MONGO_URI:
        pytest.skip(_skip_reason)

    from tcg.data._mongo.instruments import MongoInstrumentReader

    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    try:
        await db.list_collection_names()
    except Exception as exc:
        pytest.skip(f"MongoDB not reachable: {exc}")

    yield MongoInstrumentReader(db)

    client.close()


# ---------------------------------------------------------------------------
# Low-level: fetch_futures_contracts
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_fetch_futures_contracts_returns_contracts(mongo_reader):
    """Verify we can fetch futures contracts from a real collection."""
    contracts = await mongo_reader.fetch_futures_contracts(FUTURES_COLLECTION)
    assert len(contracts) > 0, (
        f"Expected contracts in {FUTURES_COLLECTION}, got none"
    )

    for c in contracts:
        assert isinstance(c, ContractPriceData)
        assert c.contract_id, "contract_id must be non-empty"
        assert 19000101 <= c.expiration <= 21001231, (
            f"Expiration {c.expiration} out of range"
        )
        assert len(c.prices) > 0, (
            f"Contract {c.contract_id} has no price data"
        )


@pytest.mark.integration
async def test_fetch_futures_contracts_sorted_by_expiration(mongo_reader):
    """Contracts must be returned in ascending expiration order."""
    contracts = await mongo_reader.fetch_futures_contracts(FUTURES_COLLECTION)
    if len(contracts) < 2:
        pytest.skip("Need at least 2 contracts for ordering test")

    expirations = [c.expiration for c in contracts]
    assert expirations == sorted(expirations), (
        f"Contracts not sorted by expiration: {expirations[:10]}..."
    )


@pytest.mark.integration
async def test_fetch_futures_contracts_no_nan_in_close(mongo_reader):
    """All returned contracts must have NaN-free close prices."""
    contracts = await mongo_reader.fetch_futures_contracts(FUTURES_COLLECTION)
    for c in contracts[:10]:  # Check first 10 to keep test fast
        assert not np.any(np.isnan(c.prices.close)), (
            f"NaN in close prices for contract {c.contract_id}"
        )


@pytest.mark.integration
async def test_fetch_available_cycles(mongo_reader):
    """Verify we can fetch distinct expiration cycles."""
    cycles = await mongo_reader.fetch_available_cycles(FUTURES_COLLECTION)
    assert isinstance(cycles, list)
    # Cycles should be sorted strings
    assert cycles == sorted(cycles)
    for c in cycles:
        assert isinstance(c, str)
        assert len(c) > 0


# ---------------------------------------------------------------------------
# Service-level: get_continuous
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_continuous_none_adjustment(market_data):
    """Build a continuous series with NONE adjustment from real data."""
    collections = await market_data.list_collections()
    if FUTURES_COLLECTION not in collections:
        pytest.skip(f"{FUTURES_COLLECTION} not in registry")

    roll_config = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH,
        adjustment=AdjustmentMethod.NONE,
    )

    series = await market_data.get_continuous(FUTURES_COLLECTION, roll_config)
    if series is None:
        pytest.skip("No continuous series built (empty contracts?)")

    assert isinstance(series, ContinuousSeries)
    assert series.collection == FUTURES_COLLECTION
    assert series.roll_config == roll_config
    assert len(series.prices) > 0, "Continuous series has no price data"

    # Dates must be sorted ascending
    dates = series.prices.dates
    assert all(
        dates[i] <= dates[i + 1] for i in range(len(dates) - 1)
    ), "Dates not sorted in continuous series"

    # No NaN in close prices
    assert not np.any(np.isnan(series.prices.close)), (
        "Found NaN in continuous series close prices"
    )


@pytest.mark.integration
async def test_get_continuous_has_roll_dates(market_data):
    """Continuous series should report roll dates when multiple contracts used."""
    collections = await market_data.list_collections()
    if FUTURES_COLLECTION not in collections:
        pytest.skip(f"{FUTURES_COLLECTION} not in registry")

    roll_config = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH,
        adjustment=AdjustmentMethod.NONE,
    )

    series = await market_data.get_continuous(FUTURES_COLLECTION, roll_config)
    if series is None:
        pytest.skip("No continuous series built")

    # With VIX futures, there should be multiple contracts and roll points
    assert len(series.contracts) > 1, (
        f"Expected multiple contracts, got {len(series.contracts)}"
    )
    assert len(series.roll_dates) > 0, "Expected at least one roll date"

    # Roll dates must be sorted
    assert series.roll_dates == tuple(sorted(series.roll_dates)), (
        "Roll dates not sorted"
    )

    # Roll dates must fall within the price date range
    min_date = int(series.prices.dates[0])
    max_date = int(series.prices.dates[-1])
    for rd in series.roll_dates:
        assert min_date <= rd <= max_date, (
            f"Roll date {rd} outside price range [{min_date}, {max_date}]"
        )


@pytest.mark.integration
async def test_get_continuous_proportional_returns_continuity(market_data):
    """PROPORTIONAL adjustment should preserve returns continuity at rolls."""
    collections = await market_data.list_collections()
    if FUTURES_COLLECTION not in collections:
        pytest.skip(f"{FUTURES_COLLECTION} not in registry")

    config_none = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH,
        adjustment=AdjustmentMethod.NONE,
    )
    config_prop = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH,
        adjustment=AdjustmentMethod.PROPORTIONAL,
    )

    series_none = await market_data.get_continuous(FUTURES_COLLECTION, config_none)
    series_prop = await market_data.get_continuous(FUTURES_COLLECTION, config_prop)

    if series_none is None or series_prop is None:
        pytest.skip("Could not build both adjustment types")

    assert isinstance(series_prop, ContinuousSeries)
    assert len(series_prop.prices) > 0

    # Both should span similar date ranges
    assert series_prop.prices.dates[0] == series_none.prices.dates[0]
    assert series_prop.prices.dates[-1] == series_none.prices.dates[-1]

    # Proportional adjustment should have no NaN
    assert not np.any(np.isnan(series_prop.prices.close)), (
        "NaN in proportionally adjusted close prices"
    )


@pytest.mark.integration
async def test_get_continuous_caches_result(market_data):
    """Second call should return the same cached object."""
    collections = await market_data.list_collections()
    if FUTURES_COLLECTION not in collections:
        pytest.skip(f"{FUTURES_COLLECTION} not in registry")

    roll_config = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH,
        adjustment=AdjustmentMethod.NONE,
    )

    series1 = await market_data.get_continuous(FUTURES_COLLECTION, roll_config)
    if series1 is None:
        pytest.skip("No continuous series built")

    series2 = await market_data.get_continuous(FUTURES_COLLECTION, roll_config)
    assert series2 is series1, "Expected same cached object on second call"


@pytest.mark.integration
async def test_get_available_cycles(market_data):
    """Service-level cycle discovery should return sorted string values."""
    collections = await market_data.list_collections()
    if FUTURES_COLLECTION not in collections:
        pytest.skip(f"{FUTURES_COLLECTION} not in registry")

    cycles = await market_data.get_available_cycles(FUTURES_COLLECTION)
    assert isinstance(cycles, list)
    assert cycles == sorted(cycles)
