"""API tests for /api/signals/compute -- v2 shape (iter-3).

Builds a FastAPI app with only the signals router and a mocked
MarketDataService. Every test exercises the v2 request/response
contract:

    request:  blocks carry ``instrument`` + ``weight``; indicator operands
              may carry ``params_override`` / ``series_override``.
    response: ``{timestamps, positions: [...], clipped, diagnostics}``.
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
    "    w = int(window)\n"
    "    if w <= len(s):\n"
    "        out[w-1:] = np.convolve(s, np.ones(w)/w, mode='valid')\n"
    "    return out\n"
)


SPX_REF = {"collection": "INDEX", "instrument_id": "SPX"}


class TestComputeEndpoint:

    async def test_happy_path_e2e_with_indicator(self, client: AsyncClient):
        """v2 happy path: single-instrument signal with one indicator operand.

        v2 update from iter-1: block carries ``instrument`` + ``weight``
        and response exposes ``positions[0].values`` instead of a flat
        ``position``.
        """
        body = {
            "spec": {
                "id": "sig1",
                "name": "Trend follower",
                "rules": {
                    "long_entry": [
                        {
                            "instrument": SPX_REF,
                            "weight": 1.0,
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
                            ],
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

        # v2 shape.
        assert "timestamps" in data
        assert "positions" in data
        assert "clipped" in data
        assert isinstance(data["timestamps"], list)
        assert len(data["timestamps"]) == 10

        assert len(data["positions"]) == 1
        p0 = data["positions"][0]
        assert p0["instrument"] == SPX_REF
        assert len(p0["values"]) == 10
        assert len(p0["clipped_mask"]) == 10
        assert p0["clipped_mask"] == [False] * 10

        # Close is monotonically increasing → price > SMA(3) fires from t=2.
        assert p0["values"][0] == 0.0
        assert p0["values"][1] == 0.0
        assert p0["values"][2] == 1.0
        assert p0["values"][-1] == 1.0
        assert data["clipped"] is False

        # Price series attached (walk picks lhs instrument operand first).
        assert p0["price"] is not None
        assert p0["price"]["label"] == "SPX.close"
        assert p0["price"]["values"] == CLOSES.tolist()

    async def test_validation_error_unknown_op(self, client: AsyncClient):
        # v2 update: block carries instrument + weight (though validation
        # fails before any evaluation).
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "instrument": SPX_REF,
                            "weight": 1.0,
                            "conditions": [{"op": "frobnicate"}],
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
                            "instrument": {
                                "collection": "INDEX",
                                "instrument_id": "NOPE",
                            },
                            "weight": 1.0,
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
                            ],
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
        """Indicator whose user code raises must surface error_type='runtime'
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
                            "instrument": SPX_REF,
                            "weight": 1.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "indicator",
                                        "indicator_id": "bad",
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
        assert "tcg/" not in tb

    async def test_price_field_present_per_instrument(self, client: AsyncClient):
        """v2: each positions[i] carries its own price payload.

        Walk order: first instrument operand inside the block's
        conditions (lhs before rhs), falling back to the block's
        top-level instrument at field=close. Here the single block's
        first operand is an ``instrument`` lhs → ``SPX.close``.
        """
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "instrument": SPX_REF,
                            "weight": 1.0,
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
                            ],
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

        assert len(data["positions"]) == 1
        p0 = data["positions"][0]
        assert p0["price"] is not None
        assert set(p0["price"].keys()) == {"label", "values"}
        assert p0["price"]["label"] == "SPX.close"
        assert p0["price"]["values"] == CLOSES.tolist()

    async def test_price_falls_back_to_block_instrument_close(
        self, client: AsyncClient
    ):
        """When a block's conditions reference no instrument operand, the
        price payload MUST fall back to ``block.instrument`` at
        ``field=close`` -- per PLAN.md §Response body walk order note.
        """
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "instrument": SPX_REF,
                            "weight": 1.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "indicator",
                                        "indicator_id": "sma3",
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
        p0 = data["positions"][0]
        assert p0["price"] is not None
        assert p0["price"]["label"] == "SPX.close"

    async def test_rolling_condition_via_api(self, client: AsyncClient):
        """Rolling lookback honoured end-to-end (v2 response shape)."""
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "rules": {
                    "long_entry": [
                        {
                            "instrument": SPX_REF,
                            "weight": 1.0,
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
                            ],
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
        p0 = data["positions"][0]
        # CLOSES strictly increasing → rolling_gt lookback=1 fires from t=1.
        assert p0["values"][0] == 0.0
        assert p0["values"][1] == 1.0
        assert p0["values"][-1] == 1.0


# ── v2: dedicated clipping round-trip ─────────────────────────────────────


class TestV2Clipping:
    async def test_three_entry_blocks_round_trip_clip(self, client: AsyncClient):
        """Round-trip a v2 request with three entry blocks summing weight
        > 1 on the same instrument; assert ``clipped=True`` on the crafted
        timesteps and ``clipped_mask`` reflects exactly those steps.
        """
        # Three blocks each weight 0.5 firing together on ``gt`` close > 12
        # (CLOSES = 10..19 → fires t=3..9). 0.5*3 = 1.5 > 1 ⇒ clipping on
        # all those timesteps.
        body = {
            "spec": {
                "id": "s",
                "name": "s",
                "rules": {
                    "long_entry": [
                        {
                            "instrument": SPX_REF,
                            "weight": 0.5,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "collection": "INDEX",
                                        "instrument_id": "SPX",
                                    },
                                    "rhs": {"kind": "constant", "value": 12.0},
                                }
                            ],
                        }
                    ]
                    * 3,  # three identical blocks
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
        assert data["clipped"] is True

        p0 = data["positions"][0]
        # CLOSES > 12 at t=3..9 (7 timesteps).
        expected_mask = [False, False, False] + [True] * 7
        assert p0["clipped_mask"] == expected_mask
        # Post-clip values: 0 at t=0..2, 1 at t=3..9.
        assert p0["values"] == [0.0, 0.0, 0.0] + [1.0] * 7
