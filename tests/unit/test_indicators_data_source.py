"""Per-instrument v1/v2 selection on the indicators route.

Indicators gain v2 support for the first time under per-instrument selection:
each series ref reads from the warehouse matching its own ``data_source`` (or the
per-run default). This proves each labeled series is routed to its own warehouse
and that an all-v1 compute never touches the v2 service (Sign 1).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.indicators import router as indicators_router
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries

DATES = np.array([20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64)


def _price_series() -> PriceSeries:
    c = np.array([100.0 + i for i in range(DATES.shape[0])], dtype=np.float64)
    return PriceSeries(
        dates=DATES,
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(DATES.shape[0], 1000.0, dtype=np.float64),
    )


def _stub(tag: str) -> MagicMock:
    svc = MagicMock()
    svc.tag = tag
    svc.seen_prices = []

    async def _prices(collection, instrument_id, *, start=None, end=None, provider=None):
        svc.seen_prices.append((collection, instrument_id))
        return _price_series()

    svc.get_prices = AsyncMock(side_effect=_prices)
    return svc


@pytest.fixture
def services():
    return {"v1": _stub("v1"), "v2": _stub("v2")}


@pytest.fixture
async def client(services):
    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(indicators_router)
    app.state.market_data = services["v1"]
    app.state.market_data_v2_compat = services["v2"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


_CODE = "def compute(series):\n    return series['x'] + series['y']\n"


async def test_each_series_routes_to_its_own_source(client, services):
    body = {
        "code": _CODE,
        "params": {},
        "series": {
            "x": {"type": "spot", "collection": "INDEX", "instrument_id": "V1SYM", "data_source": "v1"},
            "y": {"type": "spot", "collection": "INDEX", "instrument_id": "V2SYM", "data_source": "v2"},
        },
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    resp = await client.post("/api/indicators/compute", json=body)
    assert resp.status_code == 200, resp.text
    assert ("INDEX", "V1SYM") in services["v1"].seen_prices
    assert ("INDEX", "V2SYM") in services["v2"].seen_prices
    assert ("INDEX", "V2SYM") not in services["v1"].seen_prices
    assert ("INDEX", "V1SYM") not in services["v2"].seen_prices


async def test_all_v1_indicator_never_touches_v2(client, services):
    body = {
        "code": _CODE,
        "params": {},
        "series": {
            "x": {"type": "spot", "collection": "INDEX", "instrument_id": "AAA"},
            "y": {"type": "spot", "collection": "INDEX", "instrument_id": "BBB"},
        },
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    resp = await client.post("/api/indicators/compute", json=body)
    assert resp.status_code == 200, resp.text
    assert services["v2"].get_prices.await_count == 0
    assert services["v1"].get_prices.await_count == 2
