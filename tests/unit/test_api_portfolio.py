"""Unit tests for POST /api/portfolio/compute endpoint.

Tests validation, leg conversion, and response structure using a mocked
MarketDataService that returns controlled PriceSeries data.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.data._mongo.registry import CollectionRegistry
from tcg.types.market import PriceSeries


# ── Helpers ────────────────────────────────────────────────────────────


def _price_series(dates: list[int], close_vals: list[float]) -> PriceSeries:
    n = len(dates)
    d = np.array(dates, dtype=np.int64)
    c = np.array(close_vals, dtype=np.float64)
    return PriceSeries(
        dates=d,
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


# Common dates: 10 days in Jan 2024
DATES = [
    20240102, 20240103, 20240104, 20240105, 20240108,
    20240109, 20240110, 20240111, 20240112, 20240115,
]

# Two legs: one trending up, one flat
SPX_CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0]
VIX_CLOSES = [20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0]


@pytest.fixture
def mock_app():
    """Build a FastAPI app with a fully mocked MarketDataService."""
    from fastapi import FastAPI
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.core.api.errors import tcg_error_handler
    from tcg.types.errors import TCGError

    registry = CollectionRegistry(["INDEX", "FUT_VIX", "FUT_SP_500", "ETF"])

    common_dates = np.array(DATES, dtype=np.int64)
    aligned_series = {
        "SPX": _price_series(DATES, SPX_CLOSES),
        "VIX Futures": _price_series(DATES, VIX_CLOSES),
    }

    svc = MagicMock()
    svc._registry = registry
    svc.get_aligned_prices = AsyncMock(return_value=(common_dates, aligned_series))

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = svc

    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Valid request ──────────────────────────────────────────────────────


VALID_BODY = {
    "legs": {
        "SPX": {"type": "instrument", "collection": "INDEX", "symbol": "SP500"},
        "VIX Futures": {
            "type": "continuous",
            "collection": "FUT_VIX",
            "strategy": "front_month",
            "adjustment": "none",
            "cycle": "FGHJKMNQUVXZ",
        },
    },
    "weights": {"SPX": 60, "VIX Futures": 40},
    "rebalance": "monthly",
    "return_type": "normal",
    "start": "2024-01-01",
    "end": "2024-12-31",
}


class TestPortfolioCompute:

    async def test_success_returns_200(self, client: AsyncClient):
        resp = await client.post("/api/portfolio/compute", json=VALID_BODY)
        assert resp.status_code == 200

    async def test_response_structure(self, client: AsyncClient):
        resp = await client.post("/api/portfolio/compute", json=VALID_BODY)
        body = resp.json()

        # Top-level keys
        expected_keys = {
            "dates", "portfolio_equity", "leg_equities",
            "metrics", "leg_metrics",
            "monthly_returns", "yearly_returns", "date_range",
            "full_date_range", "rebalance", "return_type",
        }
        assert expected_keys <= set(body.keys())

        # Dates are ISO strings
        assert isinstance(body["dates"], list)
        assert len(body["dates"]) == len(DATES)
        assert body["dates"][0] == "2024-01-02"

        # Equity curves are lists of floats
        assert isinstance(body["portfolio_equity"], list)
        assert len(body["portfolio_equity"]) == len(DATES)

        # Leg equities present for each leg
        assert "SPX" in body["leg_equities"]
        assert "VIX Futures" in body["leg_equities"]

        # Full date range covers entire dataset
        assert "start" in body["full_date_range"]
        assert "end" in body["full_date_range"]

        # Metrics are dicts with expected fields
        assert "total_return" in body["metrics"]
        assert "sharpe_ratio" in body["metrics"]
        assert "max_drawdown" in body["metrics"]

        # Leg metrics per leg
        assert "SPX" in body["leg_metrics"]
        assert "total_return" in body["leg_metrics"]["SPX"]

        # Date range
        assert body["date_range"]["start"] == "2024-01-02"
        assert body["date_range"]["end"] == "2024-01-15"

        # Metadata echoed
        assert body["rebalance"] == "monthly"
        assert body["return_type"] == "normal"

    async def test_aggregated_returns_present(self, client: AsyncClient):
        resp = await client.post("/api/portfolio/compute", json=VALID_BODY)
        body = resp.json()

        assert isinstance(body["monthly_returns"], list)
        assert isinstance(body["yearly_returns"], list)
        # With 10 days in Jan 2024 we should have 1 monthly bucket
        assert len(body["monthly_returns"]) >= 1
        # And 1 yearly bucket
        assert len(body["yearly_returns"]) >= 1
        # Each bucket has a period key
        assert "period" in body["monthly_returns"][0]
        assert "portfolio" in body["monthly_returns"][0]


# ── Validation errors ──────────────────────────────────────────────────


class TestPortfolioValidation:

    async def test_empty_legs(self, client: AsyncClient):
        body = {**VALID_BODY, "legs": {}}
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert resp.json()["error_type"] == "validation_error"

    async def test_missing_weight_for_leg(self, client: AsyncClient):
        body = {**VALID_BODY, "weights": {"SPX": 60}}  # Missing "VIX Futures"
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert "weights missing" in resp.json()["message"]

    async def test_invalid_rebalance(self, client: AsyncClient):
        body = {**VALID_BODY, "rebalance": "biweekly"}
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert "rebalance" in resp.json()["message"].lower()

    async def test_invalid_return_type(self, client: AsyncClient):
        body = {**VALID_BODY, "return_type": "geometric"}
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert "return_type" in resp.json()["message"]

    async def test_invalid_date_format(self, client: AsyncClient):
        body = {**VALID_BODY, "start": "not-a-date"}
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert "date" in resp.json()["message"].lower()

    async def test_invalid_leg_type(self, client: AsyncClient):
        body = {
            **VALID_BODY,
            "legs": {"BAD": {"type": "option", "collection": "OPT_VIX"}},
            "weights": {"BAD": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 422  # Pydantic validation error

    async def test_instrument_leg_missing_symbol(self, client: AsyncClient):
        body = {
            **VALID_BODY,
            "legs": {
                "SPX": {"type": "instrument", "collection": "INDEX"},
                "VIX Futures": VALID_BODY["legs"]["VIX Futures"],
            },
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert "symbol" in resp.json()["message"].lower()

    async def test_continuous_leg_missing_strategy(self, client: AsyncClient):
        body = {
            **VALID_BODY,
            "legs": {
                "SPX": VALID_BODY["legs"]["SPX"],
                "VIX Futures": {"type": "continuous", "collection": "FUT_VIX"},
            },
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert "strategy" in resp.json()["message"].lower()

    async def test_unknown_collection_for_instrument(self, client: AsyncClient):
        body = {
            **VALID_BODY,
            "legs": {
                "BAD": {"type": "instrument", "collection": "UNKNOWN_COL", "symbol": "X"},
                "VIX Futures": VALID_BODY["legs"]["VIX Futures"],
            },
            "weights": {"BAD": 60, "VIX Futures": 40},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        assert "asset class" in resp.json()["message"].lower()


# ── int_to_iso helper ──────────────────────────────────────────────────


class TestIntToIso:

    def test_standard_date(self):
        from tcg.data._utils import int_to_iso
        assert int_to_iso(20240115) == "2024-01-15"

    def test_single_digit_month_and_day(self):
        from tcg.data._utils import int_to_iso
        assert int_to_iso(20000101) == "2000-01-01"

    def test_end_of_year(self):
        from tcg.data._utils import int_to_iso
        assert int_to_iso(20231231) == "2023-12-31"
