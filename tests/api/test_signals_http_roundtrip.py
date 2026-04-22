"""HTTP round-trip integration test for /api/signals/compute (v4).

Submits a full v4 signal through the ASGI stack and asserts the
response-shape contract: top-level keys, per-position shape, per-event
schema (id/kind/fired/latched/active/target_entry_block_name), and
latching semantics (per-target-entry clearing).
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
    [20240102, 20240103, 20240104, 20240105, 20240108, 20240109], dtype=np.int64
)
LATCH_DATES = np.array(
    [
        20240102, 20240103, 20240104, 20240105,
        20240108, 20240109, 20240110, 20240111,
    ],
    dtype=np.int64,
)


def _price_series(closes: np.ndarray) -> PriceSeries:
    n = DATES.shape[0]
    return PriceSeries(
        dates=DATES,
        open=closes - 1.0,
        high=closes + 1.0,
        low=closes - 2.0,
        close=closes,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


SPX_CLOSES = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
NDX_CLOSES = np.array([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])


@pytest.fixture
def mock_app():
    svc = MagicMock()

    async def fake_get_prices(collection, instrument_id, start=None, end=None):
        if instrument_id == "SPX":
            return _price_series(SPX_CLOSES)
        if instrument_id == "NDX":
            return _price_series(NDX_CLOSES)
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)

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
    "def compute(series, window: int = 2):\n"
    "    s = series['price']\n"
    "    out = np.full_like(s, np.nan, dtype=float)\n"
    "    w = int(window)\n"
    "    if w <= len(s):\n"
    "        out[w-1:] = np.convolve(s, np.ones(w)/w, mode='valid')\n"
    "    return out\n"
)


async def test_http_roundtrip_v4_two_inputs_indicator_operand(
    client: AsyncClient,
):
    """Two inputs (X=SPX, Y=NDX); one entry per input, one bound to X
    (long w=+60), the other bound to Y (short w=-40). Indicator
    operands use input-scoped binding."""
    body = {
        "spec": {
            "id": "multi-input-v4",
            "name": "Two inputs demo",
            "inputs": [
                {
                    "id": "X",
                    "instrument": {
                        "type": "spot",
                        "collection": "INDEX",
                        "instrument_id": "SPX",
                    },
                },
                {
                    "id": "Y",
                    "instrument": {
                        "type": "spot",
                        "collection": "INDEX",
                        "instrument_id": "NDX",
                    },
                },
            ],
            "rules": {
                "entries": [
                    {
                        "id": "EX",
                        "name": "EntryX",
                        "input_id": "X",
                        "weight": 60.0,
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
                                    "indicator_id": "sma",
                                    "input_id": "X",
                                },
                            }
                        ],
                    },
                    {
                        "id": "EY",
                        "name": "EntryY",
                        "input_id": "Y",
                        "weight": -40.0,
                        "conditions": [
                            {
                                "op": "lt",
                                "lhs": {
                                    "kind": "instrument",
                                    "input_id": "Y",
                                    "field": "close",
                                },
                                "rhs": {
                                    "kind": "indicator",
                                    "indicator_id": "sma",
                                    "input_id": "Y",
                                },
                            }
                        ],
                    },
                ],
                "exits": [],
            },
        },
        "indicators": [
            {
                "id": "sma",
                "name": "SMA",
                "code": SMA_CODE,
                "params": {"window": 2},
                "seriesMap": {
                    "price": {
                        "collection": "INDEX",
                        "instrument_id": "PLACEHOLDER",
                    }
                },
            }
        ],
        "instruments": {},
    }
    resp = await client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Top-level shape
    assert set(data.keys()) >= {
        "timestamps",
        "positions",
        "indicators",
        "events",
        "clipped",
        "diagnostics",
    }
    assert isinstance(data["timestamps"], list)
    assert isinstance(data["positions"], list)
    # indicators is an ARRAY (iter-3 contract preserved).
    assert isinstance(data["indicators"], list)
    assert isinstance(data["clipped"], bool)
    assert isinstance(data["diagnostics"], dict)

    # Per-position shape
    assert len(data["positions"]) == 2
    for p in data["positions"]:
        assert "input_id" in p
        assert "instrument" in p and "type" in p["instrument"]
        assert isinstance(p["values"], list)
        assert len(p["values"]) == len(data["timestamps"])

    by_id = {p["input_id"]: p for p in data["positions"]}
    # SPX ascending, SMA(2) trails → SPX > SMA fires from t=1. Weight
    # 60 → 0.6. Latched from t=1 onward.
    assert by_id["X"]["values"][0] == 0.0
    assert by_id["X"]["values"][1] == pytest.approx(0.6)
    # NDX descending, SMA(2) trails → NDX < SMA fires from t=1. Weight
    # -40 → short 0.4.
    assert by_id["Y"]["values"][0] == 0.0
    assert by_id["Y"]["values"][1] == pytest.approx(-0.4)

    # Events carry the new v4 schema.
    ev_by_id = {ev["block_id"]: ev for ev in data["events"]}
    assert ev_by_id["EX"]["kind"] == "entry"
    assert ev_by_id["EX"]["target_entry_block_name"] is None
    assert "active_indices" in ev_by_id["EX"]


# ---------------------------------------------------------------------------
# v4 latched-position semantics over the HTTP boundary
# ---------------------------------------------------------------------------


LATCH_CLOSES = np.array(
    [100.0, 11.0, 100.0, 33.0, 44.0, 11.0, 66.0, 22.0]
)


def _latch_price_series(closes: np.ndarray) -> PriceSeries:
    n = LATCH_DATES.shape[0]
    return PriceSeries(
        dates=LATCH_DATES,
        open=closes - 1.0,
        high=closes + 1.0,
        low=closes - 2.0,
        close=closes,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


@pytest.fixture
def latch_app():
    svc = MagicMock()

    async def fake_get_prices(collection, instrument_id, start=None, end=None):
        if instrument_id == "SPX":
            return _latch_price_series(LATCH_CLOSES)
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(signals_router)
    app.state.market_data = svc
    return app


@pytest.fixture
async def latch_client(latch_app):
    transport = ASGITransport(app=latch_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _eq_block(
    bid: str,
    input_id: str,
    weight: float,
    threshold: float,
    target: str | None = None,
    name: str = "",
) -> dict:
    """Build an eq-block fixture.

    The ``input_id`` parameter is used for both the operand binding AND,
    on entries only, the block-level input_id. Exit blocks (``target``
    supplied) omit block-level ``input_id`` + ``weight`` per the v4
    contract — the operating input is derived from the target entry.

    ``name`` is set on entry blocks (the user-editable display name).
    ``target`` is the ``target_entry_block_name`` for exit blocks.
    """
    blk: dict = {
        "id": bid,
        "conditions": [
            {
                "op": "eq",
                "lhs": {
                    "kind": "instrument",
                    "input_id": input_id,
                    "field": "close",
                },
                "rhs": {"kind": "constant", "value": threshold},
            }
        ],
    }
    if target is not None:
        blk["target_entry_block_name"] = target
    else:
        blk["input_id"] = input_id
        blk["weight"] = weight
        if name:
            blk["name"] = name
    return blk


async def test_http_roundtrip_latched_semantics_v4(latch_client: AsyncClient):
    """8-bar SPX close series ``[100, 11, 100, 33, 44, 11, 66, 22]``
    with entries A (+60, close=11), B (-40, close=44), C (+50, close=22)
    and an exit targeting A (close=33). Asserts:
      (a) A latches at t=1 and holds across t=2 (cond false);
      (b) exit targeting A fires at t=3 → A cleared;
      (c) B latches at t=4 → -0.4;
      (d) A re-fires t=5 → A latched again, 0.6 + (-0.4) = 0.2;
      (e) t=6: neither cond fires → still 0.2;
      (f) t=7: C latches with weight +50 → 0.6 + (-0.4) + 0.5 = 0.7.
    """
    body = {
        "spec": {
            "id": "latch-demo-v4",
            "name": "Latching v4",
            "inputs": [
                {
                    "id": "X",
                    "instrument": {
                        "type": "spot",
                        "collection": "INDEX",
                        "instrument_id": "SPX",
                    },
                }
            ],
            "rules": {
                "entries": [
                    _eq_block("A", "X", 60.0, 11.0, name="EntryA"),   # long, close=11 → t=1,5
                    _eq_block("B", "X", -40.0, 44.0, name="EntryB"),  # short, close=44 → t=4
                    _eq_block("C", "X", 50.0, 22.0, name="EntryC"),   # long, close=22 → t=7
                ],
                "exits": [
                    # Targets entry named "EntryA"; fires at close=33 → t=3.
                    _eq_block("XA", "X", 0.0, 33.0, target="EntryA"),
                ],
            },
        },
        "indicators": [],
        "instruments": {},
    }

    resp = await latch_client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    vals = data["positions"][0]["values"]
    # t=0: 100 → nothing fires → 0
    # t=1: A latches +0.6 → 0.6
    # t=2: 100 → A still latched → 0.6
    # t=3: exit XA fires → A cleared → 0
    # t=4: B latches -0.4 → -0.4
    # t=5: A re-fires → latched; B still latched → 0.6 - 0.4 = 0.2
    # t=6: 66 → nothing → 0.2
    # t=7: C latches +0.5 → 0.6 - 0.4 + 0.5 = 0.7
    assert vals == pytest.approx([0.0, 0.6, 0.6, 0.0, -0.4, 0.2, 0.2, 0.7])

    # Events schema
    ev_by = {ev["block_id"]: ev for ev in data["events"]}
    assert ev_by["A"]["kind"] == "entry"
    assert ev_by["A"]["fired_indices"] == [1, 5]
    # Latched = bars where False→True transitioned: 1 (fresh), 5 (after clear).
    assert ev_by["A"]["latched_indices"] == [1, 5]
    # Active = bars where A's latch held at emission.
    assert ev_by["A"]["active_indices"] == [1, 2, 5, 6, 7]
    assert ev_by["A"]["target_entry_block_name"] is None

    assert ev_by["B"]["fired_indices"] == [4]
    assert ev_by["B"]["latched_indices"] == [4]
    assert ev_by["B"]["active_indices"] == [4, 5, 6, 7]

    assert ev_by["C"]["fired_indices"] == [7]
    assert ev_by["C"]["latched_indices"] == [7]
    assert ev_by["C"]["active_indices"] == [7]

    assert ev_by["XA"]["kind"] == "exit"
    assert ev_by["XA"]["fired_indices"] == [3]
    # Effective exit: A was open at t=3 → actually cleared.
    assert ev_by["XA"]["latched_indices"] == [3]
    assert ev_by["XA"]["target_entry_block_name"] == "EntryA"
    assert ev_by["XA"]["active_indices"] == []

    # realized_pnl exposed as list of lists.
    assert len(data["realized_pnl"]) == 1
    pnl = data["realized_pnl"][0]
    assert len(pnl) == len(data["timestamps"])
    assert pnl[0] == 0.0

    assert data["indicators"] == []
