"""Router-level tests for /api/data-v2 (mocked service, no live dwh).

Verifies endpoint shapes, route ordering (/continuous/* and /series/* are not
captured by /objects/{object_id}), and that a delta criterion surfaces a 400.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.app import create_app
from tcg.types.errors import ValidationError
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    OptionsContinuousV2,
    PriceSeries,
    RollStrategy,
)


def _continuous():
    prices = PriceSeries(
        dates=np.array([20240102, 20240103], dtype=np.int64),
        open=np.array([10.0, 11.0]),
        high=np.array([10.5, 11.5]),
        low=np.array([9.5, 10.5]),
        close=np.array([10.2, 11.2]),
        volume=np.array([100.0, 200.0]),
    )
    return ContinuousSeries(
        collection="FUT_SP_500",
        roll_config=ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.NONE
        ),
        prices=prices,
        roll_dates=(20240103,),
        contracts=("ESH4", "ESM4"),
    )


@pytest.fixture
async def client():
    app = create_app()
    mock = AsyncMock()
    mock.list_objects = AsyncMock(
        return_value=[
            {
                "object_id": 6,
                "kind": "future",
                "symbol": "FUT_SP_500",
                "name": "S&P 500 E-mini",
                "cycle": "quarterly",
                "underlying_object_id": 5,
            }
        ]
    )
    mock.get_object_detail = AsyncMock(
        return_value={
            "object": {"object_id": 6, "kind": "future", "symbol": "FUT_SP_500"},
            "contracts": [
                {
                    "contract_id": 87,
                    "contract_code": "ESM0.20100618",
                    "expiration": "2010-06-18",
                    "strike": None,
                    "option_type": None,
                    "multiplier": 50.0,
                }
            ],
            "series": [
                {
                    "serie_id": 76,
                    "contract_id": 71,
                    "type": "bar",
                    "freq": "daily",
                    "source": "DATABENTO",
                }
            ],
        }
    )
    mock.get_series = AsyncMock(
        return_value={
            "serie_id": 5,
            "type": "bar",
            "fields": ["open", "high", "low", "close", "volume", "open_interest"],
            "points": {"ts": [20240102], "close": [42.0]},
        }
    )
    mock.get_continuous_future = AsyncMock(return_value=_continuous())
    mock.get_future_cycles = AsyncMock(return_value=["quarterly"])
    mock.get_continuous_options = AsyncMock(
        return_value=OptionsContinuousV2(
            object_id=7,
            criterion="strike",
            option_type="put",
            dates=(20240618,),
            values=(0.25,),
            roll_dates=(),
            contracts=("EW3M4 P5000.20240621",),
            contract_codes=("EW3M4 P5000.20240621",),
        )
    )
    app.state.market_data_v2 = mock

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_list_objects(client):
    resp = await client.get("/api/data-v2/objects")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["symbol"] == "FUT_SP_500"
    assert body[0]["underlying_object_id"] == 5


async def test_object_detail(client):
    resp = await client.get("/api/data-v2/objects/6")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"]["symbol"] == "FUT_SP_500"
    assert body["contracts"][0]["multiplier"] == 50.0


async def test_series_route_not_captured_by_object_id(client):
    # /series/{id} must resolve to the series handler, not /objects/{id}.
    resp = await client.get("/api/data-v2/series/5")
    assert resp.status_code == 200
    assert resp.json()["type"] == "bar"


async def test_continuous_futures_route_ordering(client):
    # /continuous/futures/{id} and its /cycles sub-route resolve correctly.
    resp = await client.get("/api/data-v2/continuous/futures/6")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contracts"] == ["ESH4", "ESM4"]
    assert body["prices"]["close"] == [10.2, 11.2]
    assert body["close"] == [10.2, 11.2]  # flat mirror

    resp2 = await client.get("/api/data-v2/continuous/futures/6/cycles")
    assert resp2.status_code == 200
    assert resp2.json()["cycles"] == ["quarterly"]


async def test_continuous_options_strike(client):
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "strike", "target": 5000, "option_type": "put"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["points"]["ts"] == [20240618]
    assert body["points"]["value"] == [0.25]
    # per-date contract codes serialized alongside ts/value (1:1)
    assert body["points"]["contract"] == ["EW3M4 P5000.20240621"]
    assert body["contracts"] == ["EW3M4 P5000.20240621"]


async def test_continuous_options_delta_returns_400(client):
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.market_data_v2.get_continuous_options = AsyncMock(
        side_effect=ValidationError("Delta unavailable: greeks empty in v2")
    )
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "delta", "target": 0.1, "option_type": "put"},
    )
    assert resp.status_code == 400
    assert resp.json()["error_type"] == "validation_error"


async def test_continuous_options_bad_roll_returns_400(client):
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "strike", "target": 5000, "roll": "monthly"},
    )
    assert resp.status_code == 400
