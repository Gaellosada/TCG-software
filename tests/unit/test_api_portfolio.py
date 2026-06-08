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
            "dates",
            "portfolio_equity",
            "leg_equities",
            "metrics",
            "leg_metrics",
            "monthly_returns",
            "yearly_returns",
            "date_range",
            "full_date_range",
            "rebalance",
            "return_type",
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

    async def test_metrics_nan_is_sanitized_to_null_in_json(self, client: AsyncClient):
        """#6 regression: a NaN inside the ``metrics`` block must be
        emitted as JSON ``null`` — NOT a bare ``NaN`` token (invalid per
        RFC-8259, which the browser's strict ``res.json()`` rejects).

        We force a NaN metric by patching ``compute_metrics`` (as bound in
        the portfolio router) to return a suite with a NaN field.
        """
        from tcg.types.metrics import MetricsSuite

        nan_suite = MetricsSuite(
            total_return=float("nan"),
            annualized_return=0.0,
            sharpe_ratio=float("inf"),
            max_drawdown=0.0,
            calmar_ratio=0.0,
            cvar_5=0.0,
            time_underwater_days=0,
            annualized_volatility=0.0,
            sortino_ratio=0.0,
            num_trades=0,
            win_rate=None,
        )
        with patch("tcg.core.api.portfolio.compute_metrics", return_value=nan_suite):
            resp = await client.post("/api/portfolio/compute", json=VALID_BODY)
        assert resp.status_code == 200, resp.text

        # The RAW response text must not carry bare non-finite JSON tokens.
        raw = resp.text
        assert "NaN" not in raw, f"bare NaN leaked into JSON: {raw[:400]}"
        assert "Infinity" not in raw, f"bare Infinity leaked into JSON: {raw[:400]}"

        body = resp.json()
        assert body["metrics"]["total_return"] is None
        assert body["metrics"]["sharpe_ratio"] is None


# ── Non-finite equity blocks must serialize as null (RFC-8259) ───────────


# Reuse VALID_BODY's two legs (SPX + "VIX Futures") so leg labels line up
# with whatever aligned series we inject. We override get_aligned_prices to
# return pathological closes that NaN/inf-poison the equity curves, then
# assert the RAW response text carries no bare non-finite JSON token. These
# drive the ACTUAL compute + serialization path (mirroring the #6 raw-text
# test) — a leg with a run of NaN closes (an all-NaN leg) and a leg with a
# zero-price bar. We use rebalance="daily" so both the daily portfolio_equity
# /leg_equities AND the buy-and-hold raw_leg_equities blocks are exercised.
class TestNonFiniteEquityBlocksSanitized:
    """BLOCKING regression: ``portfolio_equity`` / ``leg_equities`` /
    ``raw_leg_equities`` were serialized via ``.tolist()`` WITHOUT
    ``sanitize_json_floats``, and ``raw_leg_equities`` (always buy-and-hold)
    NaN/inf-poisons. Starlette's ``JSONResponse`` renders with
    ``json.dumps(allow_nan=False)`` → a bare NaN/inf raises and the endpoint
    500s. No serialized float block may emit NaN/Infinity, anywhere.
    """

    def _inject(self, mock_app, dates: list[int], spx, vix) -> None:
        svc = mock_app.state.market_data
        common = np.array(dates, dtype=np.int64)
        aligned = {
            "SPX": _price_series(dates, spx),
            "VIX Futures": _price_series(dates, vix),
        }
        svc.get_aligned_prices = AsyncMock(return_value=(common, aligned))

    @staticmethod
    def _assert_no_bare_nonfinite(resp) -> None:
        # If a non-finite float reached the JSON renderer the endpoint would
        # 500; assert success first so the reason is unambiguous.
        assert resp.status_code == 200, resp.text
        raw = resp.text
        assert "NaN" not in raw, f"bare NaN leaked into JSON: {raw[:400]}"
        assert "Infinity" not in raw, f"bare Infinity leaked into JSON: {raw[:400]}"
        # "-Infinity" is a substring of "Infinity"; assert explicitly anyway.
        assert "-Infinity" not in raw, f"bare -Infinity leaked: {raw[:400]}"

    async def test_case_a_all_nan_leg(self, mock_app, client: AsyncClient):
        """Case A: one leg is all-NaN after its first bar. Buy-and-hold
        (``raw_leg_equities``) cumprods NaN to the end of the curve."""
        dates = DATES
        spx = [100.0 + i for i in range(len(dates))]
        # "VIX Futures" leg: first bar then all-NaN → every return is NaN.
        vix = [100.0] + [float("nan")] * (len(dates) - 1)
        self._inject(mock_app, dates, spx, vix)

        body = {
            **VALID_BODY,
            "rebalance": "daily",
            "weights": {"SPX": 50, "VIX Futures": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        self._assert_no_bare_nonfinite(resp)

        parsed = resp.json()
        # raw_leg_equities is ALWAYS buy-and-hold; the source fix holds the
        # all-NaN leg flat at its initial allocation, so the curve is finite
        # (no nulls) AND constant — not a row poisoned to null. The sanitizer
        # is the backstop, but with the source fix there is nothing to null.
        raw_vix = parsed["raw_leg_equities"]["VIX Futures"]
        assert None not in raw_vix, raw_vix
        assert raw_vix[0] is not None
        assert all(v == raw_vix[0] for v in raw_vix), f"not held flat: {raw_vix}"

    async def test_case_b_zero_price_bar(self, mock_app, client: AsyncClient):
        """Case B: a leg has a zero-price bar → normal return is a divide-by
        -zero (inf), and the next return divides by zero again. Both the
        daily equity (inf) and buy-and-hold (NaN) blocks must not leak."""
        dates = DATES
        spx = [100.0 + i for i in range(len(dates))]
        # Zero close at index 1, recovers after — exercises inf and the
        # following division-by-zero.
        vix = [100.0, 0.0] + [50.0 + i for i in range(len(dates) - 2)]
        self._inject(mock_app, dates, spx, vix)

        body = {
            **VALID_BODY,
            "rebalance": "daily",
            "weights": {"SPX": 50, "VIX Futures": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        self._assert_no_bare_nonfinite(resp)

    async def test_case_b_zero_price_buy_and_hold(self, mock_app, client: AsyncClient):
        """Case B under buy-and-hold (rebalance='none'): the zero-price bar
        flows straight into the per-leg equity curve. Source fix must hold it
        flat (no inf); sanitizer is the backstop."""
        dates = DATES
        spx = [100.0 + i for i in range(len(dates))]
        vix = [100.0, 0.0] + [50.0 + i for i in range(len(dates) - 2)]
        self._inject(mock_app, dates, spx, vix)

        body = {
            **VALID_BODY,
            "rebalance": "none",
            "weights": {"SPX": 50, "VIX Futures": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        self._assert_no_bare_nonfinite(resp)


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
                "BAD": {
                    "type": "instrument",
                    "collection": "UNKNOWN_COL",
                    "symbol": "X",
                },
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
