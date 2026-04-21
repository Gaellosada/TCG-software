"""API tests for /api/indicators/compute.

Mirrors the pattern in tests/unit/test_api_portfolio.py: builds a small
FastAPI app with only the indicators router and a mocked MarketDataService.
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
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    PriceSeries,
    RollStrategy,
)


# ── Fixtures ───────────────────────────────────────────────────────────


DATES = np.array(
    [
        20240102, 20240103, 20240104, 20240105, 20240108,
        20240109, 20240110, 20240111, 20240112, 20240115,
    ],
    dtype=np.int64,
)
CLOSES = np.array(
    [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0],
    dtype=np.float64,
)


def _price_series(closes: np.ndarray | None = None) -> PriceSeries:
    c = CLOSES if closes is None else closes
    n = DATES.shape[0]
    return PriceSeries(
        dates=DATES,
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


def _continuous_series(collection: str = "FUT_X") -> ContinuousSeries:
    prices = _price_series()
    return ContinuousSeries(
        collection=collection,
        roll_config=ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
            cycle="Z",
            roll_offset_days=0,
        ),
        prices=prices,
        roll_dates=(20240108,),
        contracts=("FUT_X_Z24", "FUT_X_Z25"),
    )


@pytest.fixture
def mock_app():
    svc = MagicMock()
    svc.get_prices = AsyncMock(return_value=_price_series())

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(indicators_router)
    app.state.market_data = svc
    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── /api/indicators/compute — happy path ───────────────────────────────


SMA_CODE = (
    "def compute(series, window: int = 3):\n"
    "    s = series['price']\n"
    "    out = np.full_like(s, np.nan, dtype=float)\n"
    "    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')\n"
    "    return out\n"
)


class TestCompute:

    async def test_happy_path_sma(self, client: AsyncClient):
        body = {
            "code": SMA_CODE,
            "params": {"window": 3},
            "series": {
                "price": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"}
            },
        }
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["dates"][0] == "2024-01-02"
        assert len(data["dates"]) == 10

        # One labeled series echoed
        assert len(data["series"]) == 1
        assert data["series"][0]["label"] == "price"
        assert data["series"][0]["collection"] == "INDEX"
        assert data["series"][0]["instrument_id"] == "SPX"
        assert len(data["series"][0]["close"]) == 10

        # Indicator aligned, leading NaNs → null, rest are 3-period SMA
        ind = data["indicator"]
        assert len(ind) == 10
        assert ind[0] is None
        assert ind[1] is None
        assert ind[2] == pytest.approx(101.0)
        assert ind[-1] == pytest.approx(108.0)

    async def test_multi_series_labeled(self, mock_app):
        """Two labeled series, code uses both, order preserved."""
        closes_b = CLOSES + 50.0

        async def get_prices(collection, instrument_id, **_kw):
            if instrument_id == "SPX":
                return _price_series(CLOSES)
            if instrument_id == "VIX":
                return _price_series(closes_b)
            return None

        mock_app.state.market_data.get_prices = AsyncMock(
            side_effect=get_prices
        )

        code = (
            "def compute(series, weight: float = 0.5):\n"
            "    return series['price'] * weight + series['vix'] * (1 - weight)\n"
        )
        body = {
            "code": code,
            "params": {"weight": 0.5},
            "series": {
                "price": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"},
                "vix": {"type": "spot", "collection": "INDEX", "instrument_id": "VIX"},
            },
        }
        transport = ASGITransport(app=mock_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/indicators/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Insertion-order preserved
        assert [s["label"] for s in data["series"]] == ["price", "vix"]
        # Indicator value = 0.5 * close + 0.5 * (close + 50) = close + 25
        assert data["indicator"][0] == pytest.approx(125.0)
        assert data["indicator"][-1] == pytest.approx(134.0)

    async def test_disallowed_import_returns_400(self, client: AsyncClient):
        body = {
            "code": "import os\ndef compute(series):\n    return series['price']\n",
            "params": {},
            "series": {
                "price": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"}
            },
        }
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "import" in data["message"].lower()
        # Validation errors must NOT include a traceback field.
        assert "traceback" not in data

    async def test_missing_compute_returns_400(self, client: AsyncClient):
        body = {
            "code": "def other(series):\n    return series['price']\n",
            "params": {},
            "series": {
                "price": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"}
            },
        }
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "compute" in data["message"].lower()
        assert "traceback" not in data

    async def test_empty_series_returns_400(self, client: AsyncClient):
        body = {"code": SMA_CODE, "params": {}, "series": {}}
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code == 400

    async def test_missing_instrument_label_in_error(self, mock_app):
        """When a labeled series can't be fetched, the error returns HTTP 400
        with error_type 'data' and mentions the user-chosen label."""
        mock_app.state.market_data.get_prices = AsyncMock(return_value=None)
        transport = ASGITransport(app=mock_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/indicators/compute",
                json={
                    "code": SMA_CODE,
                    "params": {"window": 3},
                    "series": {
                        "price": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "NOPE",
                        }
                    },
                },
            )
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "data"
        assert "price" in data["message"]
        assert "NOPE" in data["message"]

    async def test_nan_close_serialized_as_null(self, mock_app):
        """Regression: `.tolist()` of a NaN-containing close array must
        serialize NaN as JSON ``null`` rather than as the non-standard
        ``NaN`` token (iter-1 review minor bug)."""
        closes_with_nan = CLOSES.copy()
        closes_with_nan[3] = np.nan
        mock_app.state.market_data.get_prices = AsyncMock(
            return_value=_price_series(closes_with_nan)
        )
        transport = ASGITransport(app=mock_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/indicators/compute",
                json={
                    "code": SMA_CODE,
                    "params": {"window": 3},
                    "series": {
                        "price": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        }
                    },
                },
            )
        assert resp.status_code == 200
        # resp.json() would raise on non-standard NaN — this assertion
        # therefore proves the NaN→null mapping is applied to close too.
        data = resp.json()
        close = data["series"][0]["close"]
        assert close[3] is None
        assert close[0] == pytest.approx(100.0)

    async def test_duplicate_dates_rejected(self, mock_app):
        """Regression: a series with duplicate or non-monotonic dates
        must be rejected up front with a clear 400, not silently produce
        a misaligned join downstream."""
        bad_dates = np.array(
            [20240102, 20240102, 20240104, 20240105, 20240108,
             20240109, 20240110, 20240111, 20240112, 20240115],
            dtype=np.int64,
        )
        series = PriceSeries(
            dates=bad_dates,
            open=CLOSES - 1.0,
            high=CLOSES + 1.0,
            low=CLOSES - 2.0,
            close=CLOSES,
            volume=np.full(bad_dates.shape[0], 1000.0, dtype=np.float64),
        )
        mock_app.state.market_data.get_prices = AsyncMock(return_value=series)
        transport = ASGITransport(app=mock_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/indicators/compute",
                json={
                    "code": SMA_CODE,
                    "params": {"window": 3},
                    "series": {
                        "price": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        }
                    },
                },
            )
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        msg = data["message"].lower()
        assert "duplicate" in msg or "non-monotonic" in msg
        # Message should reference the label, not a synthetic key.
        assert "'price'" in data["message"]

    async def test_duplicate_dates_rejected_on_second_series(self, mock_app):
        """Duplicate dates rejection still fires when the offending series
        is one of several labeled entries."""
        bad_dates = np.array(
            [20240102, 20240102, 20240104, 20240105, 20240108,
             20240109, 20240110, 20240111, 20240112, 20240115],
            dtype=np.int64,
        )
        good = _price_series()
        bad = PriceSeries(
            dates=bad_dates,
            open=CLOSES - 1.0,
            high=CLOSES + 1.0,
            low=CLOSES - 2.0,
            close=CLOSES,
            volume=np.full(bad_dates.shape[0], 1000.0, dtype=np.float64),
        )

        async def get_prices(collection, instrument_id, **_kw):
            return good if instrument_id == "SPX" else bad

        mock_app.state.market_data.get_prices = AsyncMock(
            side_effect=get_prices
        )
        transport = ASGITransport(app=mock_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/indicators/compute",
                json={
                    "code": (
                        "def compute(series, w: int = 2):\n"
                        "    return series['price'] + series['vix']\n"
                    ),
                    "params": {"w": 2},
                    "series": {
                        "price": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        },
                        "vix": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "VIX",
                        },
                    },
                },
            )
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "'vix'" in data["message"]

    # ── Structured error response shape tests ──────────────────────────────

    async def test_runtime_error_returns_structured_response(
        self, client: AsyncClient
    ):
        """A runtime error inside user compute() returns error_type='runtime'
        with a non-empty traceback containing the user's source line.

        The code accesses a key that doesn't exist in the series dict, which
        raises a KeyError inside the user's compute function at a known line.
        """
        code = (
            "def compute(series, window: int = 3):\n"
            "    # This key does not exist — raises KeyError from user code\n"
            "    return series['no_such_key']\n"
        )
        body = {
            "code": code,
            "params": {"window": 3},
            "series": {"price": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"}},
        }
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "runtime"
        assert "KeyError" in data["message"]
        # traceback must be present and reference the indicator file
        assert "traceback" in data
        tb = data["traceback"]
        assert tb  # non-empty
        assert "KeyError" in tb
        assert "<indicator>" in tb  # user-code filename in traceback
        # Must not leak internal TCG module paths
        assert "tcg/" not in tb
        assert "indicator_exec" not in tb
        assert "tcg.engine" not in tb

    async def test_validation_error_no_traceback(self, client: AsyncClient):
        """A static validation error returns error_type='validation' with
        no traceback field."""
        code = (
            "def compute(series, window):\n"  # missing annotation
            "    return series['price']\n"
        )
        body = {
            "code": code,
            "params": {},
            "series": {"price": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"}},
        }
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "traceback" not in data

    async def test_data_error_missing_instrument(self, mock_app):
        """Missing instrument returns error_type='data' with HTTP 400."""
        mock_app.state.market_data.get_prices = AsyncMock(return_value=None)
        transport = ASGITransport(app=mock_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/indicators/compute",
                json={
                    "code": SMA_CODE,
                    "params": {"window": 3},
                    "series": {
                        "price": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "MISSING",
                        }
                    },
                },
            )
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "data"
        assert "MISSING" in data["message"]
        assert "traceback" not in data

    async def test_continuous_series_returns_200(self, mock_app):
        """A ContinuousSeriesRef fetches via svc.get_continuous and returns
        the aligned series data with the correct echoed ref fields."""
        cs = _continuous_series("FUT_X")
        mock_app.state.market_data.get_continuous = AsyncMock(return_value=cs)

        body = {
            "code": SMA_CODE,
            "params": {"window": 3},
            "series": {
                "price": {
                    "type": "continuous",
                    "collection": "FUT_X",
                    "adjustment": "none",
                    "cycle": "Z",
                    "rollOffset": 0,
                    "strategy": "front_month",
                },
            },
        }
        transport = ASGITransport(app=mock_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/indicators/compute", json=body)

        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Dates and indicator output aligned to the mocked continuous series
        assert len(data["dates"]) == len(DATES)
        assert data["dates"][0] == "2024-01-02"

        # Echoed series ref
        assert len(data["series"]) == 1
        s = data["series"][0]
        assert s["label"] == "price"
        assert s["type"] == "continuous"
        assert s["collection"] == "FUT_X"
        assert s["adjustment"] == "none"
        assert s["cycle"] == "Z"
        assert s["rollOffset"] == 0
        assert s["strategy"] == "front_month"
        assert len(s["close"]) == len(DATES)

        # Indicator produced (not all None)
        assert any(v is not None for v in data["indicator"])

        # get_continuous was called (not get_prices)
        mock_app.state.market_data.get_continuous.assert_awaited_once()
        mock_app.state.market_data.get_prices.assert_not_awaited()
