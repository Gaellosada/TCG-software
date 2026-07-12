"""Unit tests for composed portfolios (``type="portfolio"`` legs).

A composed portfolio reuses a saved PURE portfolio as a leg: the frontend
inlines the child's resolved spec into ``legs[...]["portfolio"]`` and the
backend computes that child to an equity curve, injecting it as a synthetic
price series (mirrors a signal leg).

These tests are BACKEND-AUTHORITATIVE: composed request bodies are built by
hand, no frontend. The mock ``get_aligned_prices`` returns a controlled
``PriceSeries`` per requested leg LABEL, so the parent and each child are
independently steerable — the child is computed over the same date grid as its
standalone counterpart, which is the crux of criterion A1-1.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.data.service import DefaultMarketDataService
from tcg.types.market import PriceSeries


# ── Fixed date grid (10 trading days, Jan 2024) ────────────────────────

DATES = [
    20240102,
    20240103,
    20240104,
    20240105,
    20240108,
    20240109,
    20240110,
    20240111,
    20240112,
    20240115,
]

# Per-LABEL close series the mock serves. Two building blocks trending
# differently so a weighted combination is non-degenerate.
CLOSES_BY_LABEL: dict[str, list[float]] = {
    "up": [100.0, 101.0, 102.5, 103.0, 105.0, 104.0, 106.0, 108.0, 107.0, 110.0],
    "down": [200.0, 199.0, 198.0, 197.5, 196.0, 197.0, 195.0, 193.0, 194.0, 190.0],
    "flat": [50.0] * 10,
}


def _price_series(close_vals: list[float]) -> PriceSeries:
    n = len(DATES)
    d = np.array(DATES, dtype=np.int64)
    c = np.array(close_vals, dtype=np.float64)
    return PriceSeries(
        dates=d,
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


@pytest.fixture
def mock_app():
    from fastapi import FastAPI
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.core.api.errors import tcg_error_handler
    from tcg.types.errors import TCGError

    common_dates = np.array(DATES, dtype=np.int64)

    async def _aligned(legs_spec):
        # Serve one PriceSeries per requested leg LABEL. Unknown labels fall
        # back to a simple ramp so a mislabeled test surfaces loudly, not via
        # a KeyError deep in the engine.
        series = {}
        for label in legs_spec:
            closes = CLOSES_BY_LABEL.get(label, [100.0 + i for i in range(len(DATES))])
            series[label] = _price_series(closes)
        return common_dates, series

    svc = MagicMock()
    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = svc
    app.state.app_db_repo = object()
    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Helpers ────────────────────────────────────────────────────────────


def _instrument_leg(label: str) -> dict:
    """A pure instrument leg whose label drives the mock's close series."""
    return {"type": "instrument", "collection": "INDEX", "symbol": label}


def _pure_body(
    labels: list[str], weights: list[float], rebalance: str = "none"
) -> dict:
    return {
        "legs": {lbl: _instrument_leg(lbl) for lbl in labels},
        "weights": {lbl: w for lbl, w in zip(labels, weights)},
        "rebalance": rebalance,
        "return_type": "normal",
    }


def _normalized(values: list[float]) -> np.ndarray:
    arr = np.array(values, dtype=np.float64)
    return arr / arr[0]


async def _equity(client: AsyncClient, body: dict) -> list[float]:
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["portfolio_equity"]


# ── A1-1: single portfolio leg == standalone child ─────────────────────


class TestComposedEqualsStandalone:
    async def test_single_leg_matches_standalone(self, client: AsyncClient):
        # Child = a real 2-instrument pure portfolio.
        child = _pure_body(["up", "down"], [60.0, 40.0], rebalance="none")
        child.update(start="2024-01-01", end="2024-12-31")

        standalone = await _equity(client, child)

        composed = {
            "legs": {
                "block": {
                    "type": "portfolio",
                    "portfolio_id": "child-doc-1",
                    "portfolio": child,
                }
            },
            "weights": {"block": 100.0},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        parent = await _equity(client, composed)

        # Same length, and identical up to the base level (both start at 100
        # but compare relative to be robust to any base convention).
        assert len(parent) == len(standalone)
        np.testing.assert_allclose(
            _normalized(parent), _normalized(standalone), rtol=1e-6, atol=0.0
        )


# ── A1-2: two portfolio legs combine correctly ─────────────────────────


class TestTwoPortfolioLegs:
    async def test_two_legs_weighted_combination(self, client: AsyncClient):
        # Two distinct pure children.
        child_a = _pure_body(["up", "flat"], [100.0, 0.0], rebalance="none")
        child_b = _pure_body(["down"], [100.0], rebalance="none")

        composed = {
            "legs": {
                "A": {"type": "portfolio", "portfolio_id": "a", "portfolio": child_a},
                "B": {"type": "portfolio", "portfolio_id": "b", "portfolio": child_b},
            },
            "weights": {"A": 50.0, "B": 50.0},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=composed)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Both legs surface as per-leg equities.
        assert set(body["leg_equities"].keys()) == {"A", "B"}

        # Independent oracle: build each child equity standalone, take daily
        # normal returns, blend 50/50 (rebalance none → buy-and-hold with equal
        # initial weights), and compound. Must match the composed equity.
        eq_a = np.array(await _equity(client, {**child_a, "start": "2024-01-01"}))
        eq_b = np.array(await _equity(client, {**child_b, "start": "2024-01-01"}))
        r_a = np.diff(eq_a) / eq_a[:-1]
        r_b = np.diff(eq_b) / eq_b[:-1]

        # Buy-and-hold two equal-weight sleeves: value = 0.5*eqA/eqA0 +
        # 0.5*eqB/eqB0, normalized. (No rebalancing between them.)
        blended = 0.5 * (eq_a / eq_a[0]) + 0.5 * (eq_b / eq_b[0])
        parent = np.array(body["portfolio_equity"], dtype=np.float64)
        np.testing.assert_allclose(
            parent / parent[0], blended / blended[0], rtol=1e-6, atol=0.0
        )
        # Sanity: the two per-leg return streams are genuinely different.
        assert not np.allclose(r_a, r_b)


# ── Mixed parent: instrument leg + portfolio leg (composed ⊇ pure) ─────


class TestMixedParent:
    async def test_instrument_and_portfolio_leg_together(self, client: AsyncClient):
        # A composed portfolio holding BOTH a raw instrument leg and a
        # portfolio building block. Regression: ``_parse_legs`` must skip the
        # portfolio leg (else it is misparsed as a continuous leg → 400).
        child = _pure_body(["up", "down"], [50.0, 50.0], rebalance="none")
        composed = {
            "legs": {
                "block": {"type": "portfolio", "portfolio_id": "c", "portfolio": child},
                "spot": _instrument_leg("flat"),
            },
            "weights": {"block": 70.0, "spot": 30.0},
            "rebalance": "monthly",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=composed)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body["leg_equities"].keys()) == {"block", "spot"}
        assert len(body["portfolio_equity"]) == len(DATES)


# ── A1-4: depth-1 guard (child contains a portfolio leg) ───────────────


class TestDepthOneGuard:
    async def test_nested_portfolio_leg_rejected(self, client: AsyncClient):
        grandchild = _pure_body(["up"], [100.0])
        # child itself contains a portfolio leg → depth 2, must be rejected.
        child_with_portfolio = {
            "legs": {
                "inner": {
                    "type": "portfolio",
                    "portfolio_id": "gc",
                    "portfolio": grandchild,
                }
            },
            "weights": {"inner": 100.0},
            "rebalance": "none",
            "return_type": "normal",
        }
        composed = {
            "legs": {
                "block": {
                    "type": "portfolio",
                    "portfolio_id": "child",
                    "portfolio": child_with_portfolio,
                }
            },
            "weights": {"block": 100.0},
            "rebalance": "none",
            "return_type": "normal",
        }
        resp = await client.post("/api/portfolio/compute", json=composed)
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error_type"] == "validation_error"
        assert "depth-1" in body["message"]


# ── A1-5: broken / empty reference → 400 (never 500) ───────────────────


class TestBrokenReference:
    async def test_missing_portfolio_field(self, client: AsyncClient):
        composed = {
            "legs": {
                "block": {"type": "portfolio", "portfolio_id": "missing"},
            },
            "weights": {"block": 100.0},
            "rebalance": "none",
            "return_type": "normal",
        }
        resp = await client.post("/api/portfolio/compute", json=composed)
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error_type"] == "validation_error"
        assert "no legs or could not be resolved" in body["message"]

    async def test_empty_child_legs(self, client: AsyncClient):
        composed = {
            "legs": {
                "block": {
                    "type": "portfolio",
                    "portfolio_id": "empty",
                    "portfolio": {
                        "legs": {},
                        "weights": {},
                        "rebalance": "none",
                        "return_type": "normal",
                    },
                }
            },
            "weights": {"block": 100.0},
            "rebalance": "none",
            "return_type": "normal",
        }
        resp = await client.post("/api/portfolio/compute", json=composed)
        assert resp.status_code == 400, resp.text
        assert "no legs or could not be resolved" in resp.json()["message"]


# ── A leg whose type is unknown is still rejected (schema guard) ────────


class TestTypeValidation:
    async def test_portfolio_is_a_valid_leg_type(self, client: AsyncClient):
        # Sanity: constructing a portfolio leg does not trip the type validator.
        child = _pure_body(["up"], [100.0])
        composed = {
            "legs": {
                "block": {"type": "portfolio", "portfolio_id": "x", "portfolio": child}
            },
            "weights": {"block": 100.0},
            "rebalance": "none",
            "return_type": "normal",
        }
        resp = await client.post("/api/portfolio/compute", json=composed)
        assert resp.status_code == 200, resp.text
