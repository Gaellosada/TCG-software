"""API tests for /api/signals/compute -- v4 shape.

v4 unifies long/short into signed-weight entries and exits targeting a
specific entry block via ``target_entry_block_name``. Weights are signed
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
    async def test_happy_path_single_input_indicator_operand(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig1",
                "name": "Trend follower",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "name": "Entry1",
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
                            "name": "ShortEntry",
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
                            "name": "Entry1",
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
                            "target_entry_block_name": "Entry1",
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

    async def test_unknown_target_entry_block_name_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "bad exit",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "name": "Entry1",
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
                            "target_entry_block_name": "NOPE",
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
        assert "target_entry_block_name" in data["message"]
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
                            "name": "Entry1",
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
        assert "target_entry_block_name" in data["message"]

    async def test_entry_with_target_name_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "name": "Entry1",
                            "input_id": "X",
                            "weight": 100.0,
                            "target_entry_block_name": "Entry1",  # not allowed
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
        assert "target_entry_block_name" in data["message"]

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
                            "name": "Entry1",
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
                            "target_entry_block_name": "Entry1",
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
                            "name": "Entry1",
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
                            "target_entry_block_name": "Entry1",
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
                            "name": "Entry1",
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

    async def test_entry_weight_out_of_range_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "name": "Entry1",
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

    async def test_exit_with_legacy_target_entry_block_id_rejected(
        self, client: AsyncClient
    ):
        """Exit sending target_entry_block_id (legacy) is rejected."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "name": "Entry1",
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
        assert "target_entry_block_id" in data["message"]

    async def test_duplicate_entry_names_rejected(self, client: AsyncClient):
        """Two entries with the same non-empty name are rejected."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "name": "SameName",
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
                            "id": "E2",
                            "name": "SameName",
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
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "duplicate" in data["message"].lower()
        assert "SameName" in data["message"]

    async def test_exit_with_dangling_target_entry_block_name_rejected(
        self, client: AsyncClient
    ):
        """Exit's target_entry_block_name matches no entry name."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E1",
                            "name": "Entry1",
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
                            "target_entry_block_name": "DoesNotExist",
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
        assert "target_entry_block_name" in data["message"]
        assert "DoesNotExist" in data["message"]


class TestResetBlocksAPI:
    """Reset block parsing + strict rejection (CONTRACT §1.5)."""

    async def test_t20_empty_resets_equals_omitted(self, client: AsyncClient):
        """``rules.resets: []`` produces an identical result to omitting
        the field entirely.

        The shape of the per-position values + events must match
        byte-for-byte across both wire encodings; we capture the response
        body and compare.
        """
        base_spec = {
            "id": "sig",
            "name": "",
            "inputs": [SPX_INPUT],
            "rules": {
                "entries": [
                    {
                        "id": "E1",
                        "name": "Entry1",
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
        }
        body_no_field = {
            "spec": base_spec,
            "indicators": [],
            "instruments": {},
        }
        spec_with_empty = dict(base_spec)
        spec_with_empty["rules"] = dict(base_spec["rules"])
        spec_with_empty["rules"]["resets"] = []
        body_with_empty = {
            "spec": spec_with_empty,
            "indicators": [],
            "instruments": {},
        }

        r1 = await client.post("/api/signals/compute", json=body_no_field)
        r2 = await client.post("/api/signals/compute", json=body_with_empty)
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["positions"] == r2.json()["positions"]
        assert r1.json()["events"] == r2.json()["events"]

    async def test_t21_reset_with_input_id_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [],
                    "exits": [],
                    "resets": [
                        {
                            "id": "R1",
                            "name": "Reset1",
                            "input_id": "X",
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
        assert "reset blocks must not set input_id" in data["message"]

    async def test_t22_reset_with_target_entry_block_name_rejected(
        self, client: AsyncClient
    ):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [],
                    "exits": [],
                    "resets": [
                        {
                            "id": "R1",
                            "name": "Reset1",
                            "target_entry_block_name": "abc",
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
        assert "reset blocks must not set target_entry_block_name" in data["message"]

    async def test_t23_reset_with_nonzero_weight_rejected(self, client: AsyncClient):
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [],
                    "exits": [],
                    "resets": [
                        {
                            "id": "R1",
                            "name": "Reset1",
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
                },
            },
            "indicators": [],
            "instruments": {},
        }
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "reset blocks must not set weight" in data["message"]

    # ----- requires_reset_count (COUNT feature) -----------------------

    async def test_t24_reset_count_below_one_rejected(self, client: AsyncClient):
        # requires_reset_count must be >= 1; 0 is rejected at parse time.
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E",
                            "name": "Entry",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "X",
                                    },
                                    "rhs": {"kind": "constant", "value": 1.0},
                                }
                            ],
                            "requires_reset_block_id": "R1",
                            "requires_reset_count": 0,
                        }
                    ],
                    "exits": [],
                    "resets": [
                        {
                            "id": "R1",
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
        assert "requires_reset_count" in data["message"]

    async def test_t25_reset_block_with_count_rejected(self, client: AsyncClient):
        # A reset block must NOT carry requires_reset_count (mirrors the
        # requires_reset_block_id rejection). The default value 1 is the
        # only acceptable value on a reset; any explicit non-default is
        # rejected loudly.
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [],
                    "exits": [],
                    "resets": [
                        {
                            "id": "R1",
                            "requires_reset_count": 3,
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
        assert "reset blocks must not set requires_reset_count" in data["message"]

    async def test_t26_valid_count_threads_into_block(self, client: AsyncClient):
        # A valid requires_reset_count on a bound entry parses and is
        # threaded onto the resulting Block. Asserted via parse_signal so
        # the typed field is inspectable.
        from tcg.core.api.signals import SignalIn, parse_signal

        spec = SignalIn.model_validate(
            {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E",
                            "name": "Entry",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "X",
                                    },
                                    "rhs": {"kind": "constant", "value": 1.0},
                                }
                            ],
                            "requires_reset_block_id": "R1",
                            "requires_reset_count": 4,
                        }
                    ],
                    "exits": [],
                    "resets": [
                        {
                            "id": "R1",
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
            }
        )
        signal = parse_signal(spec)
        assert signal.rules.entries[0].requires_reset_count == 4
        # Default applies when omitted on another bound block.
        assert signal.rules.resets[0].requires_reset_count == 1

    async def test_t27_count_omitted_defaults_to_one(self, client: AsyncClient):
        # Backward-compat: a stored/legacy spec that omits requires_reset_count
        # parses with the default 1 (no schema-version change required).
        from tcg.core.api.signals import SignalIn, parse_signal

        spec = SignalIn.model_validate(
            {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "E",
                            "name": "Entry",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [
                                {
                                    "op": "gt",
                                    "lhs": {
                                        "kind": "instrument",
                                        "input_id": "X",
                                    },
                                    "rhs": {"kind": "constant", "value": 1.0},
                                }
                            ],
                            "requires_reset_block_id": "R1",
                        }
                    ],
                    "exits": [],
                    "resets": [
                        {
                            "id": "R1",
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
            }
        )
        signal = parse_signal(spec)
        assert signal.rules.entries[0].requires_reset_count == 1


def _eq_cond(value: float) -> dict:
    """An ``X.close == value`` condition (wire shape)."""
    return {
        "op": "eq",
        "lhs": {"kind": "instrument", "input_id": "X"},
        "rhs": {"kind": "constant", "value": value},
    }


class TestMultiTargetExitsF1:
    """F1 — a single exit closing MULTIPLE entry blocks.

    Covers: plural ``target_entry_block_names`` accepted; legacy singular
    ``target_entry_block_name`` accepted and EQUIVALENT; dangling target
    rejected (plural key); duplicate target names rejected; exit carrying
    ``input_id`` rejected even with the plural key.
    """

    async def test_plural_targets_close_two_entries(self, client: AsyncClient):
        """One exit with two targets clears BOTH entry latches at once.

        CLOSES = ``[10..19]``. EntryA (w=100, X==10 → t0) and EntryB
        (w=50, X==11 → t1); exit (X==15 → t5) targets both. Position:
        t0 1.0, t1 1.5, holds, t5 → 0.0 (both cleared, conds false after).
        """
        body = {
            "spec": {
                "id": "sig",
                "name": "multi",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "EA",
                            "name": "EntryA",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [_eq_cond(10.0)],
                        },
                        {
                            "id": "EB",
                            "name": "EntryB",
                            "input_id": "X",
                            "weight": 50.0,
                            "conditions": [_eq_cond(11.0)],
                        },
                    ],
                    "exits": [
                        {
                            "id": "X1",
                            "target_entry_block_names": ["EntryA", "EntryB"],
                            "conditions": [_eq_cond(15.0)],
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
        assert vals[0] == pytest.approx(1.0)
        assert vals[1] == pytest.approx(1.5)
        assert vals[4] == pytest.approx(1.5)
        assert vals[5] == pytest.approx(0.0)
        assert vals[-1] == pytest.approx(0.0)
        # Exit event emits the plural key listing both targets.
        ev = {e["block_id"]: e for e in data["events"]}
        assert set(ev["X1"]["target_entry_block_names"]) == {"EntryA", "EntryB"}
        # Entry events carry an empty list (not the legacy null).
        assert ev["EA"]["target_entry_block_names"] == []

    async def test_legacy_singular_accepted_and_equivalent(self, client: AsyncClient):
        """A spec using the legacy singular ``target_entry_block_name``
        produces IDENTICAL positions to the same spec using the plural
        one-element ``target_entry_block_names`` — proving the migration
        is behaviour-preserving."""

        def _make_body(exit_block: dict) -> dict:
            return {
                "spec": {
                    "id": "sig",
                    "name": "eq",
                    "inputs": [SPX_INPUT],
                    "rules": {
                        "entries": [
                            {
                                "id": "EA",
                                "name": "EntryA",
                                "input_id": "X",
                                "weight": 100.0,
                                "conditions": [_eq_cond(10.0)],
                            }
                        ],
                        "exits": [exit_block],
                    },
                },
                "indicators": [],
                "instruments": {},
            }

        legacy = _make_body(
            {
                "id": "X1",
                "target_entry_block_name": "EntryA",
                "conditions": [_eq_cond(15.0)],
            }
        )
        plural = _make_body(
            {
                "id": "X1",
                "target_entry_block_names": ["EntryA"],
                "conditions": [_eq_cond(15.0)],
            }
        )
        r_legacy = await client.post("/api/signals/compute", json=legacy)
        r_plural = await client.post("/api/signals/compute", json=plural)
        assert r_legacy.status_code == 200, r_legacy.text
        assert r_plural.status_code == 200, r_plural.text
        v_legacy = r_legacy.json()["positions"][0]["values"]
        v_plural = r_plural.json()["positions"][0]["values"]
        assert v_legacy == pytest.approx(v_plural)
        # And both round-trip to the same typed tuple at parse time.
        from tcg.core.api.signals import SignalIn, parse_signal

        s_legacy = parse_signal(SignalIn.model_validate(legacy["spec"]))
        s_plural = parse_signal(SignalIn.model_validate(plural["spec"]))
        assert (
            s_legacy.rules.exits[0].target_entry_block_names
            == s_plural.rules.exits[0].target_entry_block_names
            == ("EntryA",)
        )

    async def test_plural_dangling_target_rejected(self, client: AsyncClient):
        """A plural list containing a name with no matching entry is
        rejected (every target must resolve)."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "EA",
                            "name": "EntryA",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [_eq_cond(10.0)],
                        }
                    ],
                    "exits": [
                        {
                            "id": "X1",
                            "target_entry_block_names": ["EntryA", "NOPE"],
                            "conditions": [_eq_cond(15.0)],
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

    async def test_duplicate_target_names_rejected(self, client: AsyncClient):
        """The same target name twice in one exit is rejected."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "EA",
                            "name": "EntryA",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [_eq_cond(10.0)],
                        }
                    ],
                    "exits": [
                        {
                            "id": "X1",
                            "target_entry_block_names": ["EntryA", "EntryA"],
                            "conditions": [_eq_cond(15.0)],
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
        assert "duplicate" in data["message"].lower()
        assert "rules.exits[0]" in data["message"]

    async def test_plural_exit_with_input_id_rejected(self, client: AsyncClient):
        """An exit must not carry ``input_id`` even with the plural key."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "EA",
                            "name": "EntryA",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [_eq_cond(10.0)],
                        }
                    ],
                    "exits": [
                        {
                            "id": "X1",
                            "input_id": "X",  # forbidden on exits
                            "target_entry_block_names": ["EntryA"],
                            "conditions": [_eq_cond(15.0)],
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
        assert "rules.exits[0]" in data["message"]
        assert "input_id" in data["message"]

    async def test_empty_plural_list_rejected(self, client: AsyncClient):
        """An explicit empty ``target_entry_block_names`` list on an exit
        is rejected (exits require ≥1 target)."""
        body = {
            "spec": {
                "id": "sig",
                "name": "",
                "inputs": [SPX_INPUT],
                "rules": {
                    "entries": [
                        {
                            "id": "EA",
                            "name": "EntryA",
                            "input_id": "X",
                            "weight": 100.0,
                            "conditions": [_eq_cond(10.0)],
                        }
                    ],
                    "exits": [
                        {
                            "id": "X1",
                            "target_entry_block_names": [],
                            "conditions": [_eq_cond(15.0)],
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
        assert "target_entry_block_name" in data["message"]
        assert "rules.exits[0]" in data["message"]
