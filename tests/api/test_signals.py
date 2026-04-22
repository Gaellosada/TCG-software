"""API tests for /api/signals/compute -- v4 shape.

v4 unifies long/short into signed-weight entries and exits targeting a
specific entry block via ``target_entry_block_id``. Weights are signed
percentages in ``[-100, +100]``.
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
CLOSES = np.arange(10, 20, dtype=np.float64)


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


class TestComputeEndpointV4:

    async def test_happy_path_single_input_indicator_operand(
        self, client: AsyncClient
    ):
        body = {
            "spec": {
                "id": "sig1",
                "name": "Trend follower",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
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
                    "exits": [],
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

        assert set(data.keys()) >= {
            "timestamps",
            "positions",
            "indicators",
            "clipped",
            "diagnostics",
            "events",
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
        # weight 100 → 1.0 when latched; condition fires from t=2.
        assert p0["values"][0] == 0.0
        assert p0["values"][2] == 1.0
        assert p0["values"][-1] == 1.0

    async def test_signed_short_weight(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig2",
                "name": "short",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": -50.0,
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
                    "exits": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["positions"][0]["values"] == pytest.approx([-0.5] * 10)

    async def test_exit_target_entry_clearing(self, client: AsyncClient):
        """Exit targets a specific entry and clears only its latch."""
        body = {
            "spec": {
                "id": "sig3",
                "name": "exit",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [
                                {
                                    "op": "eq",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "X",
                                    },
                                    # close = 10 at t=0 only.
                                    "rhs": {"kind": "constant", "value": 10.0},
                                }
                            ],
                        }
                    ],
                    "exits": [
                        {
                            "id": "X1",
                            "target_entry_block_id": "E1",
                            "conditions": [
                                {
                                    "op": "eq",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "X",
                                    },
                                    # close = 15 at t=5.
                                    "rhs": {"kind": "constant", "value": 15.0},
                                }
                            ],
                        }
                    ],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        vals = data["positions"][0]["values"]
        # E1 latches at t=0 → 1.0; exit at t=5 clears; E1 cond false thereafter → 0.
        assert vals[0] == pytest.approx(1.0)
        assert vals[4] == pytest.approx(1.0)
        assert vals[5] == pytest.approx(0.0)
        assert vals[-1] == pytest.approx(0.0)

    async def test_unknown_target_entry_block_id_rejected(
        self, client: AsyncClient
    ):
        body = {
            "spec": {
                "id": "sig",
                "name": "bad exit",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
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
                    "exits": [
                        {
                            "id": "X1",
                            "target_entry_block_id": "NOPE",
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
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "NOPE" in data["message"]
        assert "rules.exits[0]" in data["message"]

    async def test_exit_missing_target_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
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
                    "exits": [
                        {
                            "id": "X1",
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
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "target_entry_block_id" in data["message"]

    async def test_entry_with_target_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
                            "target_entry_block_id": "E1",  # not allowed
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
                    "exits": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "target_entry_block_id" in data["message"]

    async def test_exit_with_input_id_rejected(self, client: AsyncClient):
        """Invariant: exit blocks must NOT carry a block-level input_id.

        The operating input is derived from the target entry; accepting
        a redundant value would permit two sources that could disagree.
        """
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
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
                    "exits": [
                        {
                            "id": "X1",
                            # Forbidden: exits must not carry input_id.
                            "input_id": "X",
                            "target_entry_block_id": "E1",
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
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        # Path-qualified error message mentions the exit and the field.
        assert "rules.exits[0]" in data["message"]
        assert "input_id" in data["message"]

    async def test_exit_with_empty_input_id_accepted(self, client: AsyncClient):
        """An empty-string input_id on an exit is treated the same as
        absent — the invariant rejects only non-empty values."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
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
                    "exits": [
                        {
                            "id": "X1",
                            "input_id": "",  # empty = treated as absent
                            "target_entry_block_id": "E1",
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
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text

    async def test_entry_weight_zero_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 0.0,
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
                    "exits": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "non-zero" in data["message"]

    async def test_entry_weight_out_of_range_rejected(
        self, client: AsyncClient
    ):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 150.0,  # out of [-100, 100]
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
                    "exits": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        assert "out of" in resp.json()["message"]

    async def test_duplicate_entry_id_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "DUP",
                            "input_id": "X",
                            "weight": 50.0,
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
                        },
                        {
                            "id": "DUP",
                            "input_id": "X",
                            "weight": 50.0,
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
                        },
                    ],
                    "exits": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        assert "duplicate" in resp.json()["message"].lower()

    async def test_unknown_input_id_validation(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "x",
                "name": "x",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "Z",  # not declared
                            "weight": 100.0,
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
                    "exits": [],
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
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
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [{"op": "frobnicate"}],
                        }
                    ],
                    "exits": [],
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
                    "entries": [
                        {
                            "id": "E1",
                            "input_id": "X",
                            "weight": 100.0,
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
                    "exits": [],
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
                "rules": {"entries": [], "exits": []},
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
