"""API tests for /api/signals/compute -- v3 shape (iter-4).

Inputs replace block/operand instruments. Every block references an
input_id; every instrument/indicator operand references an input_id.
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


SMA_CODE = (
    "def compute(series, window: int = 3):\n"
    "    s = series['price']\n"
    "    out = np.full_like(s, np.nan, dtype=float)\n"
    "    w = int(window)\n"
    "    if w <= len(s):\n"
    "        out[w-1:] = np.convolve(s, np.ones(w)/w, mode='valid')\n"
    "    return out\n"
)


SPX_INPUT = {
    "id": "X",
    "instrument": {
        "type": "spot",
        "collection": "INDEX",
        "instrument_id": "SPX",
    },
}


class TestComputeEndpointV3:

    async def test_happy_path_single_input_indicator_operand(
        self, client: AsyncClient
    ):
        """v3 happy path: one input + instrument/indicator operands that
        both bind through that input."""
        body = {
            "spec": {
                "id": "sig1",
                "name": "Trend follower",
                "inputs": [SPX_INPUT],
                "rules": {
                    "long_entry": [
                        {
                            "input_id": "X",
                            "weight": 1.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "X",
                                        "field": "close",
                                    },
                                    "rhs": {
                                        "kind": "indicator",
                                        "indicator_id": "sma3",
                                        "input_id": "X",
                                    },
                                }
                            ],
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": [
                {
                    "id": "sma3",
                    "name": "sma3",
                    "code": SMA_CODE,
                    "params": {"window": 3},
                    "seriesMap": {
                        "price": {
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        }
                    },
                }
            ],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Response shape preserved from iter-3 (keys).
        assert set(data.keys()) >= {
            "timestamps",
            "positions",
            "indicators",
            "clipped",
            "diagnostics",
        }
        assert isinstance(data["indicators"], list)
        assert len(data["timestamps"]) == 10

        assert len(data["positions"]) == 1
        p0 = data["positions"][0]
        assert p0["input_id"] == "X"
        assert p0["instrument"] == {
            "type": "spot",
            "collection": "INDEX",
            "instrument_id": "SPX",
        }
        assert len(p0["values"]) == 10
        assert p0["clipped_mask"] == [False] * 10
        # Close monotonically increasing → lhs > SMA(3) fires from t=2.
        assert p0["values"][0] == 0.0
        assert p0["values"][2] == 1.0
        assert p0["values"][-1] == 1.0
        assert data["clipped"] is False
        assert p0["price"] is not None
        assert p0["price"]["label"] == "SPX.close"

    async def test_unknown_input_id_validation(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "inputs": [SPX_INPUT],
                "rules": {
                    "long_entry": [
                        {
                            "input_id": "Z",  # not declared
                            "weight": 1.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "Z",
                                    },
                                    "rhs": {"kind": "constant", "value": 0.0},
                                }
                            ],
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        # Block with unknown input_id is treated as unusable → skipped.
        # The operand resolution still walks though and fails. The
        # engine raises SignalValidationError for operands referencing
        # unknown inputs.
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "Z" in data["message"]

    async def test_validation_error_unknown_op(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "inputs": [SPX_INPUT],
                "rules": {
                    "long_entry": [
                        {
                            "input_id": "X",
                            "weight": 1.0,
                            "conditions": [{"op": "frobnicate"}],
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "frobnicate" in data["message"]

    async def test_data_error_missing_instrument(self, mock_app):
        mock_app.state.market_data.get_prices = AsyncMock(return_value=None)
        transport = ASGITransport(app=mock_app)
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "inputs": [
                    {
                        "id": "X",
                        "instrument": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "NOPE",
                        },
                    }
                ],
                "rules": {
                    "long_entry": [
                        {
                            "input_id": "X",
                            "weight": 1.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "X",
                                    },
                                    "rhs": {"kind": "constant", "value": 0.0},
                                }
                            ],
                        }
                    ],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "data"
        assert "NOPE" in data["message"]

    async def test_empty_signal_is_ok(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "empty",
                "name": "",
                "inputs": [],
                "rules": {
                    "long_entry": [],
                    "long_exit": [],
                    "short_entry": [],
                    "short_exit": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["positions"] == []
        assert data["timestamps"] == []
        assert data["clipped"] is False
