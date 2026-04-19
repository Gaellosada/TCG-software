"""API tests for /api/signals/compute — mirrors tests/api/test_indicators.py.

Builds a FastAPI app with only the signals router and a mocked
MarketDataService.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.signals import router as signals_router
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries


DATES = np.array(
    [
        20240102, 20240103, 20240104, 20240105, 20240108,
        20240109, 20240110, 20240111, 20240112, 20240115,
    ],
    dtype=np.int64,
)
CLOSES = np.arange(10, 20, dtype=np.float64)  # [10..19]


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


@pytest.fixture
def mock_app():
    svc = MagicMock()
    svc.get_prices = AsyncMock(return_value=_price_series())

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(signals_router)
    app.state.market_data = svc
    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Happy path ──


SMA_CODE = (
    "def compute(series, window: int = 3):\n"
    "    s = series['price']\n"
    "    out = np.full_like(s, np.nan, dtype=float)\n"
    "    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')\n"
    "    return out\n"
)


class TestComputeEndpoint:

    async def test_happy_path_e2e_with_indicator(self, client: AsyncClient):
        """End-to-end: signal with one instrument operand and one indicator
        operand (a 3-period SMA). Must return a non-trivial position vector.
        """
        body = {
            "spec": {
                "id": "sig1",
                "name": "Trend follower",
                "rules": {
                    "long_entry": [
                        {
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "collection": "INDEX",
                                        "instrument_id": "SPX",
                                    },
                                    "rhs": {
                                        "kind": "indicator",
                                        "indicator_id": "sma3",
                                    },
                                }
                            ]
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": {
                "sma3": {
                    "code": SMA_CODE,
                    "params": {"window": 3},
                    "seriesMap": {
                        "price": {
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        }
                    },
                }
            },
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Shape checks.
        assert len(data["index"]) == 10
        assert data["index"][0] == "2024-01-02T00:00:00Z"
        assert len(data["position"]) == 10
        assert len(data["long_score"]) == 10
        assert len(data["short_score"]) == 10
        assert isinstance(data["entries_long"], list)
        assert isinstance(data["exits_long"], list)

        # Close is monotonically increasing → price > SMA(3) should hold
        # from t=2 onwards (SMA is NaN at t=0,1 → NaN→0 → position=0 there).
        assert data["position"][0] == 0.0
        assert data["position"][1] == 0.0
        assert data["position"][2] == 1.0
        assert data["position"][-1] == 1.0
        assert data["entries_long"] == [2]
        assert data["exits_long"] == []

    async def test_validation_error_unknown_op(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {"conditions": [{"op": "frobnicate"}]}
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": {},
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "frobnicate" in data["message"]
        assert "traceback" not in data

    async def test_data_error_missing_instrument(self, mock_app):
        """Missing instrument → error_type='data' with HTTP 400."""
        mock_app.state.market_data.get_prices = AsyncMock(return_value=None)
        transport = ASGITransport(app=mock_app)
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "collection": "INDEX",
                                        "instrument_id": "NOPE",
                                    },
                                    "rhs": {"kind": "constant", "value": 0.0},
                                }
                            ]
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": {},
            "instruments": {},
        }
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "data"
        assert "NOPE" in data["message"]
        assert "traceback" not in data

    async def test_runtime_error_from_indicator(self, client: AsyncClient):
        """An indicator whose user code raises must surface error_type='runtime'
        with a sanitized traceback."""
        bad_code = (
            "def compute(series):\n"
            "    return series['nope']  # KeyError inside user code\n"
        )
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "indicator",
                                        "indicator_id": "bad",
                                    },
                                    "rhs": {"kind": "constant", "value": 0.0},
                                }
                            ]
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": {
                "bad": {
                    "code": bad_code,
                    "params": {},
                    "seriesMap": {
                        "price": {
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        }
                    },
                }
            },
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "runtime"
        assert "traceback" in data
        tb = data["traceback"]
        assert "<indicator>" in tb
        # Internal module paths must NOT leak.
        assert "tcg/" not in tb

    async def test_price_field_present_when_instrument_operand_exists(
        self, client: AsyncClient
    ):
        """When the signal has at least one instrument operand, the response
        must carry ``price = {label, values}`` aligned on the union index.

        Walk order is ``long_entry → long_exit → short_entry → short_exit``,
        then block, then condition, then ``lhs`` before ``rhs`` / ``operand``
        before ``min``/``max``. Here a single long_entry comparison picks
        the lhs instrument (``SPX.close``).
        """
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "collection": "INDEX",
                                        "instrument_id": "SPX",
                                    },
                                    "rhs": {"kind": "constant", "value": 0.0},
                                }
                            ]
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": {},
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["price"] is not None
        assert set(data["price"].keys()) == {"label", "values"}
        assert data["price"]["label"] == "SPX.close"
        # values aligned to index
        assert len(data["price"]["values"]) == len(data["index"])
        # and equal to the fixture's CLOSES (10..19) cast to float.
        assert data["price"]["values"] == CLOSES.tolist()

    async def test_price_field_null_when_no_instrument_operand(
        self, client: AsyncClient
    ):
        """A signal with only indicator/constant operands → ``price`` is
        JSON ``null``. We use an indicator operand against a constant so the
        evaluator still produces a union index.
        """
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "indicator",
                                        "indicator_id": "sma3",
                                    },
                                    "rhs": {"kind": "constant", "value": 0.0},
                                }
                            ]
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": {
                "sma3": {
                    "code": SMA_CODE,
                    "params": {"window": 3},
                    "seriesMap": {
                        "price": {
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        }
                    },
                }
            },
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert "price" in data
        assert data["price"] is None

    async def test_rolling_condition_via_api(self, client: AsyncClient):
        """Rolling lookback is honoured end-to-end."""
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "conditions": [
                                {
                                    "op": "rolling_gt",
                                    "operand": {
                                        "kind": "instrument",
                                        "collection": "INDEX",
                                        "instrument_id": "SPX",
                                    },
                                    "lookback": 1,
                                }
                            ]
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": {},
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # CLOSES is strictly increasing → rolling_gt with lookback=1 fires
        # from t=1 onwards.
        assert data["position"][0] == 0.0
        assert data["position"][1] == 1.0
        assert data["position"][-1] == 1.0
