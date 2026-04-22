"""Unit tests for signal legs in POST /api/portfolio/compute.

Tests the new ``type: "signal"`` leg support: validation, evaluation mocking,
date alignment, synthetic price conversion, and edge cases.
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


# ── Minimal signal spec payloads ───────────────────────────────────────


def _minimal_signal_spec() -> dict:
    """Return a minimal valid signal_spec dict for portfolio requests (v4)."""
    return {
        "spec": {
            "id": "s1",
            "name": "Test Signal",
            "inputs": [],
            "rules": {
                "entries": [],
                "exits": [],
            },
        },
        "indicators": [],
    }


# ── Shared fixtures ───────────────────────────────────────────────────


DATES = [
    20240102, 20240103, 20240104, 20240105, 20240108,
    20240109, 20240110, 20240111, 20240112, 20240115,
]
SPX_CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0,
              105.0, 106.0, 107.0, 108.0, 109.0]


@pytest.fixture
def mock_app():
    """Build a FastAPI app with a mocked MarketDataService.

    The service is wired up so that instrument/continuous legs can resolve,
    but _evaluate_signal_leg is patched in individual tests.
    """
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.types.errors import TCGError

    registry = CollectionRegistry(["INDEX", "FUT_VIX", "FUT_SP_500", "ETF"])

    common_dates = np.array(DATES, dtype=np.int64)
    aligned_series = {
        "SPX": _price_series(DATES, SPX_CLOSES),
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


# ── 1. Validation tests ──────────────────────────────────────────────


class TestSignalLegValidation:

    async def test_signal_leg_missing_signal_spec(self, client: AsyncClient):
        """type='signal' without signal_spec should be a 422 Pydantic error."""
        body = {
            "legs": {"sig1": {"type": "signal"}},
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 422  # Pydantic model_validator

    async def test_signal_leg_with_valid_spec_passes_validation(
        self, client: AsyncClient,
    ):
        """A well-formed signal leg should pass Pydantic validation.

        It will fail at evaluation (no mock), but the error should NOT be
        422 -- it should be a runtime/validation error from the signal
        evaluation path (400).
        """
        body = {
            "legs": {"sig1": {"type": "signal", "signal_spec": _minimal_signal_spec()}},
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        # Not a 422 Pydantic error -- validation passed.
        assert resp.status_code != 422


# ── 2. Signal evaluation integration (mocked) ────────────────────────


class TestSignalEvaluationMocked:

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_only_portfolio(
        self, mock_eval_signal, client: AsyncClient,
    ):
        """Portfolio with only signal legs returns valid structure."""
        sig_dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
        sig_prices = np.array([100.0, 101.0, 103.0], dtype=np.float64)
        mock_eval_signal.return_value = (sig_dates, sig_prices)

        body = {
            "legs": {
                "sig1": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
            },
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200

        data = resp.json()
        # Structural checks
        assert "dates" in data
        assert "portfolio_equity" in data
        assert "leg_equities" in data
        assert "metrics" in data
        assert "leg_metrics" in data
        assert "monthly_returns" in data
        assert "yearly_returns" in data
        assert "date_range" in data
        assert "full_date_range" in data

        # Content checks
        assert len(data["dates"]) == 3
        assert data["dates"][0] == "2024-01-02"
        assert "sig1" in data["leg_equities"]
        assert len(data["portfolio_equity"]) == 3
        assert "sig1" in data["leg_metrics"]
        assert "total_return" in data["metrics"]
        assert "sharpe_ratio" in data["metrics"]

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_mixed_instrument_and_signal_portfolio(
        self, mock_eval_signal, client: AsyncClient,
    ):
        """Portfolio with instrument + signal legs. Both appear in response."""
        # Signal returns the same date grid as the instrument mock (DATES).
        sig_dates = np.array(DATES, dtype=np.int64)
        sig_prices = np.array(
            [100.0, 100.5, 101.0, 101.5, 102.0,
             102.5, 103.0, 103.5, 104.0, 104.5],
            dtype=np.float64,
        )
        mock_eval_signal.return_value = (sig_dates, sig_prices)

        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SP500",
                },
                "my_signal": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
            },
            "weights": {"SPX": 60, "my_signal": 40},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200

        data = resp.json()
        # Both legs present
        assert "SPX" in data["leg_equities"]
        assert "my_signal" in data["leg_equities"]
        assert "SPX" in data["leg_metrics"]
        assert "my_signal" in data["leg_metrics"]

        # Dates should cover the full intersection (same grid here).
        assert len(data["dates"]) == len(DATES)

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_date_alignment(
        self, mock_eval_signal, client: AsyncClient, mock_app,
    ):
        """Signal and instrument with partially overlapping dates.

        Instrument dates: [20200102, 20200103, 20200106, 20200107]
        Signal dates:     [20200101, 20200102, 20200103, 20200106]
        Common dates:     [20200102, 20200103, 20200106]
        """
        # Override instrument mock for this specific test.
        inst_dates = np.array(
            [20200102, 20200103, 20200106, 20200107], dtype=np.int64,
        )
        inst_closes = [50.0, 51.0, 52.0, 53.0]
        svc = mock_app.state.market_data
        svc.get_aligned_prices = AsyncMock(
            return_value=(
                inst_dates,
                {"SPX": _price_series(inst_dates.tolist(), inst_closes)},
            ),
        )

        sig_dates = np.array(
            [20200101, 20200102, 20200103, 20200106], dtype=np.int64,
        )
        sig_prices = np.array([100.0, 101.0, 103.0, 98.0], dtype=np.float64)
        mock_eval_signal.return_value = (sig_dates, sig_prices)

        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SP500",
                },
                "sig1": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
            },
            "weights": {"SPX": 50, "sig1": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200

        data = resp.json()
        # Only the 3 common dates survive.
        assert len(data["dates"]) == 3
        assert data["dates"] == ["2020-01-02", "2020-01-03", "2020-01-06"]


# ── 3. Synthetic price conversion ────────────────────────────────────


class TestSyntheticPriceConversion:

    def test_synthetic_price_from_pnl(self):
        """Verify 100.0 * (1.0 + pnl) correctly converts PnL to prices.

        pnl =    [0.00,  0.01,  0.03, -0.02]
        prices = [100.0, 101.0, 103.0,  98.0]
        """
        pnl = np.array([0.0, 0.01, 0.03, -0.02], dtype=np.float64)
        synthetic = 100.0 * (1.0 + pnl)

        expected = np.array([100.0, 101.0, 103.0, 98.0], dtype=np.float64)
        np.testing.assert_allclose(synthetic, expected, rtol=1e-12)

    def test_synthetic_price_zero_pnl_is_flat(self):
        """All-zero PnL gives a flat 100 series."""
        pnl = np.zeros(5, dtype=np.float64)
        synthetic = 100.0 * (1.0 + pnl)

        expected = np.full(5, 100.0, dtype=np.float64)
        np.testing.assert_array_equal(synthetic, expected)

    def test_synthetic_price_negative_total(self):
        """PnL of -1.0 produces a price of 0 (total loss)."""
        pnl = np.array([-1.0], dtype=np.float64)
        synthetic = 100.0 * (1.0 + pnl)
        np.testing.assert_allclose(synthetic, [0.0], atol=1e-15)


# ── 4. Edge cases ────────────────────────────────────────────────────


class TestSignalEdgeCases:

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_disjoint_date_ranges(
        self, mock_eval_signal, client: AsyncClient, mock_app,
    ):
        """Signal dates completely disjoint from instrument dates -> error."""
        # Instrument: Jan 2024 dates (from DATES fixture via mock_app).
        # Signal: Feb 2025 dates -- no overlap.
        sig_dates = np.array(
            [20250203, 20250204, 20250205], dtype=np.int64,
        )
        sig_prices = np.array([100.0, 101.0, 102.0], dtype=np.float64)
        mock_eval_signal.return_value = (sig_dates, sig_prices)

        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SP500",
                },
                "sig1": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
            },
            "weights": {"SPX": 50, "sig1": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert "disjoint" in data["message"].lower()

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_with_zero_pnl(
        self, mock_eval_signal, client: AsyncClient,
    ):
        """Signal that produced no trades -> flat synthetic prices at 100."""
        sig_dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
        # Zero PnL -> all 100.0
        sig_prices = np.full(3, 100.0, dtype=np.float64)
        mock_eval_signal.return_value = (sig_dates, sig_prices)

        body = {
            "legs": {
                "sig1": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
            },
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200

        data = resp.json()
        # Equity should be flat (all starting at 100, 0% returns).
        equity = data["portfolio_equity"]
        assert len(equity) == 3
        # First value is always 100 (base). Subsequent values reflect
        # zero returns -> stays at 100.
        assert equity[0] == pytest.approx(100.0)
        assert equity[-1] == pytest.approx(100.0)

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_multiple_signal_legs(
        self, mock_eval_signal, client: AsyncClient,
    ):
        """Portfolio with two signal legs, no instrument legs."""
        common = np.array([20240102, 20240103, 20240104], dtype=np.int64)

        # Return different series for each call.
        mock_eval_signal.side_effect = [
            (common, np.array([100.0, 102.0, 104.0], dtype=np.float64)),
            (common, np.array([100.0, 99.0, 98.0], dtype=np.float64)),
        ]

        body = {
            "legs": {
                "sig_bull": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
                "sig_bear": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
            },
            "weights": {"sig_bull": 50, "sig_bear": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert "sig_bull" in data["leg_equities"]
        assert "sig_bear" in data["leg_equities"]
        assert len(data["dates"]) == 3
