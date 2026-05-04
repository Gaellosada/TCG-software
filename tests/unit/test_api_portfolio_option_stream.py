"""Tests for POST /api/portfolio/compute with option_stream legs.

Covers:
- Mid-stream leg (price-like) participates in equity curve
- Level-stream leg (iv/greeks) goes to tracking_series
- Mixed price + level legs
- All-NaN rejection
- NaN forward-fill
- Level-only portfolio rejected
- Date requirement for option_stream legs
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.portfolio import router as portfolio_router
from tcg.data._mongo.registry import CollectionRegistry
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries


# ── Helpers ────────────────────────────────────────────────────────────

DATES = [20240102, 20240103, 20240104, 20240105, 20240108]
SPX_CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0]


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


def _fake_materialise_result(values, dates=None):
    """Build a synthetic materialise result for a single leg."""
    d = np.array(dates or DATES, dtype=np.int64)
    v = np.array(values, dtype=np.float64)
    diagnostics: list[str | None] = [None] * len(values)
    return {"_leg": (d, v, diagnostics)}


OPT_MID_LEG = {
    "type": "option_stream",
    "collection": "OPT_SP_500",
    "option_type": "C",
    "cycle": None,
    "maturity": {"kind": "next_third_friday", "offset_months": 0},
    "selection": {
        "kind": "by_delta",
        "target": 0.25,
        "tolerance": 0.1,
        "strict": False,
    },
    "stream": "mid",
}

OPT_IV_LEG = {
    **OPT_MID_LEG,
    "stream": "iv",
}

SPX_LEG = {
    "type": "instrument",
    "collection": "INDEX",
    "symbol": "SP500",
}


@pytest.fixture
def mock_app(monkeypatch):
    """FastAPI app with mocked data service and option materialisation."""
    registry = CollectionRegistry(["INDEX", "OPT_SP_500"])

    common_dates = np.array(DATES, dtype=np.int64)
    aligned_series = {
        "SPX": _price_series(DATES, SPX_CLOSES),
    }

    svc = MagicMock()
    svc._registry = registry
    svc.get_aligned_prices = AsyncMock(return_value=(common_dates, aligned_series))

    # Default materialise mock — returns mid price data
    async def fake_materialise(
        refs_with_labels, *, svc, start_date, end_date, progress_callback=None
    ):
        label = refs_with_labels[0][0]
        ref = refs_with_labels[0][1]
        values = (
            [5.0, 5.1, 5.2, 5.3, 5.4]
            if ref.stream == "mid"
            else [0.20, 0.21, 0.22, 0.23, 0.24]
        )
        d = np.array(DATES, dtype=np.int64)
        v = np.array(values, dtype=np.float64)
        diagnostics: list[str | None] = [None] * len(values)
        return {label: (d, v, diagnostics)}

    monkeypatch.setattr(
        "tcg.core.api.portfolio.materialise_option_streams",
        fake_materialise,
    )

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


# ── Tests ──────────────────────────────────────────────────────────────


class TestPortfolioOptionStream:
    async def test_mid_leg_in_equity_curve(self, client):
        """Mid-stream option leg participates in the portfolio equity curve."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": OPT_MID_LEG,
            },
            "weights": {"SPX": 60, "OPT_MID": 40},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "portfolio_equity" in data
        assert len(data["portfolio_equity"]) > 0
        assert "OPT_MID" in data["leg_equities"]
        assert "metrics" in data

    async def test_iv_leg_in_tracking_series(self, client):
        """IV-stream option leg goes to tracking_series, not equity curve."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_IV": OPT_IV_LEG,
            },
            "weights": {"SPX": 100, "OPT_IV": 100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "tracking_series" in data
        assert "OPT_IV" in data["tracking_series"]
        ts = data["tracking_series"]["OPT_IV"]
        assert ts["stream"] == "iv"
        assert ts["stream_mode"] == "level"
        assert "metrics" in ts
        metrics = ts["metrics"]
        assert metrics["mean"] is not None
        assert metrics["first"] is not None
        assert metrics["last"] is not None
        assert metrics["change"] is not None
        # IV leg should NOT be in equity curve
        assert "OPT_IV" not in data["leg_equities"]

    async def test_mixed_price_and_level_legs(self, client):
        """Mid leg in equity curve, iv leg in tracking_series, spot in equity curve."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": OPT_MID_LEG,
                "OPT_IV": OPT_IV_LEG,
            },
            "weights": {"SPX": 50, "OPT_MID": 30, "OPT_IV": 20},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Price legs in equity curve
        assert "SPX" in data["leg_equities"]
        assert "OPT_MID" in data["leg_equities"]
        # Level leg in tracking_series
        assert "OPT_IV" in data["tracking_series"]
        assert "OPT_IV" not in data["leg_equities"]

    async def test_all_nan_rejected(self, client, monkeypatch):
        """All-NaN option stream values raises validation error."""

        async def nan_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label = refs_with_labels[0][0]
            d = np.array(DATES, dtype=np.int64)
            v = np.full(len(DATES), np.nan, dtype=np.float64)
            return {label: (d, v, [None] * len(DATES))}

        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            nan_materialise,
        )
        body = {
            "legs": {"OPT_MID": OPT_MID_LEG},
            "weights": {"OPT_MID": 100},
            "rebalance": "none",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code in (400, 422), resp.text

    async def test_nan_forward_fill(self, client, monkeypatch):
        """NaN gaps in mid-stream values are forward-filled."""

        async def gap_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label = refs_with_labels[0][0]
            d = np.array(DATES, dtype=np.int64)
            v = np.array([5.0, np.nan, np.nan, 5.3, 5.4], dtype=np.float64)
            return {label: (d, v, [None] * len(DATES))}

        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            gap_materialise,
        )
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": OPT_MID_LEG,
            },
            "weights": {"SPX": 50, "OPT_MID": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Equity curve should have no NaN (forward-fill applied)
        equity = data["portfolio_equity"]
        assert all(
            v is not None and not (isinstance(v, float) and np.isnan(v)) for v in equity
        )

    async def test_level_only_rejected(self, client):
        """Portfolio with only level (non-price) legs is rejected."""
        body = {
            "legs": {"OPT_IV": OPT_IV_LEG},
            "weights": {"OPT_IV": 100},
            "rebalance": "none",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code in (400, 422), resp.text

    async def test_level_metrics_structure(self, client):
        """Tracking series level metrics have the expected fields."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_IV": OPT_IV_LEG,
            },
            "weights": {"SPX": 100, "OPT_IV": 100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["tracking_series"]["OPT_IV"]["metrics"]
        expected_keys = {"mean", "std", "min", "max", "first", "last", "change"}
        assert expected_keys <= set(metrics.keys())
