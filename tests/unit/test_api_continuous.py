"""Unit tests for the continuous futures API endpoints.

Mocks the MarketDataService to test endpoint logic in isolation.
Uses httpx AsyncClient with ASGITransport (same pattern as integration tests).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.app import create_app
from tcg.types.errors import DataNotFoundError
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    PriceSeries,
    RollStrategy,
)


def _make_continuous_series(
    collection: str = "FUT_VIX",
    strategy: RollStrategy = RollStrategy.FRONT_MONTH,
    adjustment: AdjustmentMethod = AdjustmentMethod.NONE,
    cycle: str | None = None,
) -> ContinuousSeries:
    """Build a small ContinuousSeries for testing."""
    prices = PriceSeries(
        dates=np.array([20150102, 20150105, 20150106], dtype=np.int64),
        open=np.array([10.0, 11.0, 12.0]),
        high=np.array([10.5, 11.5, 12.5]),
        low=np.array([9.5, 10.5, 11.5]),
        close=np.array([10.2, 11.2, 12.2]),
        volume=np.array([100.0, 200.0, 300.0]),
    )
    return ContinuousSeries(
        collection=collection,
        roll_config=ContinuousRollConfig(
            strategy=strategy, adjustment=adjustment, cycle=cycle
        ),
        prices=prices,
        roll_dates=(20150105,),
        contracts=("VX_F15", "VX_G15"),
    )


@pytest.fixture
async def client():
    """Create a test client with a mocked MarketDataService on app.state."""
    app = create_app()

    # Build a mock that satisfies both MarketDataService protocol
    # and the DefaultMarketDataService.get_available_cycles extension.
    mock_svc = AsyncMock()
    mock_svc.get_continuous = AsyncMock(return_value=_make_continuous_series())
    mock_svc.get_available_cycles = AsyncMock(
        return_value=["FGHJKMNQUVXZ", "HMUZ"]
    )
    mock_svc.list_collections = AsyncMock(return_value=["FUT_VIX", "INDEX"])
    mock_svc.list_instruments = AsyncMock()
    mock_svc.get_prices = AsyncMock()

    # Bypass lifespan (no MongoDB needed) by injecting the mock directly.
    app.state.market_data = mock_svc

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_svc(client: AsyncClient):
    """Access the mock service from the app state (set by the client fixture)."""
    # The transport wraps the app -- reach into it to get the mock.
    app = client._transport.app  # type: ignore[attr-defined]
    return app.state.market_data


# ---------------------------------------------------------------------------
# GET /api/data/continuous/{collection}
# ---------------------------------------------------------------------------


async def test_continuous_series_success(client: AsyncClient, mock_svc):
    """Default parameters return 200 with expected JSON shape."""
    resp = await client.get("/api/data/continuous/FUT_VIX")
    assert resp.status_code == 200

    body = resp.json()
    assert body["collection"] == "FUT_VIX"
    assert body["strategy"] == "front_month"
    assert body["adjustment"] == "none"
    assert body["cycle"] is None
    assert body["roll_dates"] == [20150105]
    assert body["contracts"] == ["VX_F15", "VX_G15"]
    assert body["dates"] == [20150102, 20150105, 20150106]
    assert len(body["open"]) == 3
    assert len(body["high"]) == 3
    assert len(body["low"]) == 3
    assert len(body["close"]) == 3
    assert len(body["volume"]) == 3

    # Verify service was called with correct config
    mock_svc.get_continuous.assert_awaited_once()
    call_args = mock_svc.get_continuous.call_args
    assert call_args[0][0] == "FUT_VIX"
    config = call_args[0][1]
    assert isinstance(config, ContinuousRollConfig)
    assert config.strategy == RollStrategy.FRONT_MONTH
    assert config.adjustment == AdjustmentMethod.NONE
    assert config.cycle is None


async def test_continuous_series_with_params(client: AsyncClient, mock_svc):
    """Explicit query parameters are parsed and forwarded correctly."""
    mock_svc.get_continuous.return_value = _make_continuous_series(
        cycle="HMUZ", adjustment=AdjustmentMethod.RATIO
    )

    resp = await client.get(
        "/api/data/continuous/FUT_VIX",
        params={
            "strategy": "front_month",
            "adjustment": "ratio",
            "cycle": "HMUZ",
            "start": "2015-01-01",
            "end": "2015-12-31",
        },
    )
    assert resp.status_code == 200

    call_args = mock_svc.get_continuous.call_args
    config = call_args[0][1]
    assert config.adjustment == AdjustmentMethod.RATIO
    assert config.cycle == "HMUZ"
    # Date args are keyword
    from datetime import date

    assert call_args[1]["start"] == date(2015, 1, 1)
    assert call_args[1]["end"] == date(2015, 12, 31)


async def test_continuous_series_invalid_strategy(client: AsyncClient):
    """Invalid strategy string returns 400 with validation_error."""
    resp = await client.get(
        "/api/data/continuous/FUT_VIX",
        params={"strategy": "bogus_strategy"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert "bogus_strategy" in body["message"]


async def test_continuous_series_invalid_adjustment(client: AsyncClient):
    """Invalid adjustment string returns 400 with validation_error."""
    resp = await client.get(
        "/api/data/continuous/FUT_VIX",
        params={"adjustment": "invalid_adj"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert "invalid_adj" in body["message"]


async def test_continuous_series_invalid_date(client: AsyncClient):
    """Malformed date returns 400 with validation_error."""
    resp = await client.get(
        "/api/data/continuous/FUT_VIX",
        params={"start": "not-a-date"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"


async def test_continuous_series_not_found(client: AsyncClient, mock_svc):
    """Service returning None triggers 404."""
    mock_svc.get_continuous.return_value = None

    resp = await client.get("/api/data/continuous/FUT_NONEXISTENT")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_type"] == "data_not_found"


async def test_continuous_series_service_raises_not_found(
    client: AsyncClient, mock_svc
):
    """Service raising DataNotFoundError triggers 404."""
    mock_svc.get_continuous.side_effect = DataNotFoundError(
        "Collection 'BAD' not found in registry"
    )

    resp = await client.get("/api/data/continuous/BAD")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_type"] == "data_not_found"


# ---------------------------------------------------------------------------
# GET /api/data/continuous/{collection}/cycles
# ---------------------------------------------------------------------------


async def test_cycles_endpoint_success(client: AsyncClient, mock_svc):
    """Cycles endpoint returns list of available cycles."""
    resp = await client.get("/api/data/continuous/FUT_VIX/cycles")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"cycles": ["FGHJKMNQUVXZ", "HMUZ"]}
    mock_svc.get_available_cycles.assert_awaited_once_with("FUT_VIX")


async def test_cycles_endpoint_not_found(client: AsyncClient, mock_svc):
    """Cycles endpoint returns 404 when collection doesn't exist."""
    mock_svc.get_available_cycles.side_effect = DataNotFoundError(
        "Collection 'NOPE' not found in registry"
    )

    resp = await client.get("/api/data/continuous/NOPE/cycles")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_type"] == "data_not_found"


# ---------------------------------------------------------------------------
# Route ordering: continuous routes must not be swallowed by /{collection}
# ---------------------------------------------------------------------------


async def test_continuous_route_not_captured_by_collection_catchall(
    client: AsyncClient, mock_svc,
):
    """Verify /continuous/FUT_VIX resolves to the continuous endpoint,
    not /{collection} with collection='continuous'."""
    resp = await client.get("/api/data/continuous/FUT_VIX")
    assert resp.status_code == 200

    # The continuous endpoint was called, not list_instruments
    mock_svc.get_continuous.assert_awaited()
    mock_svc.list_instruments.assert_not_awaited()
