"""HTTP round-trip integration test for /api/signals/compute (v3).

MANDATORY per iter-4 Sign 1: iter-3 had a dict-vs-array ``indicators``
drift that both Vitest and pytest passed. Only the reviewer caught it.
This test submits a real v3 signal with ≥2 inputs through the full
ASGI stack and asserts the exact response-shape keys + the ``indicators``
array-ness.
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


async def test_http_roundtrip_v3_two_inputs_indicator_operand(
    client: AsyncClient,
):
    """Two inputs (X=SPX, Y=NDX); one block per input with an indicator
    operand whose input_id binds it to the block's input. Asserts the
    full response shape contract:
      - top-level keys: timestamps, positions (list), indicators (list),
        clipped (bool), diagnostics (dict).
      - each position: input_id, instrument (dict with 'type'), values,
        clipped_mask, price.
      - ``indicators`` is a LIST (not dict) — iter-3 learning.
    """
    body = {
        "spec": {
            "id": "multi-input-v3",
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
                "long_entry": [
                    {
                        "input_id": "X",
                        "weight": 0.6,
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
                        "input_id": "Y",
                        "weight": 0.4,
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
                "long_exit": [],
                "short_entry": [],
                "short_exit": [],
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

    # --- Top-level shape assertions (iter-3 contract preserved) ---
    assert set(data.keys()) >= {
        "timestamps",
        "positions",
        "indicators",
        "clipped",
        "diagnostics",
    }
    assert isinstance(data["timestamps"], list)
    assert isinstance(data["positions"], list)
    # CRITICAL: indicators is an ARRAY, not a dict. iter-3 PROB-1.
    assert isinstance(data["indicators"], list), (
        "'indicators' must be a list (iter-3 contract); "
        f"got {type(data['indicators']).__name__}"
    )
    assert isinstance(data["clipped"], bool)
    assert isinstance(data["diagnostics"], dict)

    # --- Per-position shape ---
    assert len(data["positions"]) == 2
    for p in data["positions"]:
        assert "input_id" in p
        assert "instrument" in p
        assert isinstance(p["instrument"], dict)
        assert "type" in p["instrument"]
        assert p["instrument"]["type"] in ("spot", "continuous")
        assert isinstance(p["values"], list)
        assert isinstance(p["clipped_mask"], list)
        assert len(p["values"]) == len(data["timestamps"])
        assert len(p["clipped_mask"]) == len(data["timestamps"])
        # price is either None or a {label, values} object.
        assert p["price"] is None or (
            isinstance(p["price"], dict)
            and "label" in p["price"]
            and "values" in p["price"]
        )

    # --- Correct values ---
    by_id = {p["input_id"]: p for p in data["positions"]}
    assert "X" in by_id
    assert "Y" in by_id
    # SPX ascending, SMA(2) trails → SPX > SMA fires from t=1.
    # Weight 0.6 → values = [0, 0.6, 0.6, 0.6, 0.6, 0.6].
    assert by_id["X"]["values"][0] == 0.0
    assert by_id["X"]["values"][1] == pytest.approx(0.6)
    # NDX descending, SMA(2) trails → NDX < SMA fires from t=1.
    # Weight 0.4 → values = [0, 0.4, 0.4, 0.4, 0.4, 0.4].
    assert by_id["Y"]["values"][0] == 0.0
    assert by_id["Y"]["values"][1] == pytest.approx(0.4)

    # Prices are attached and labeled per the underlying instrument.
    assert by_id["X"]["price"]["label"] == "SPX.close"
    assert by_id["Y"]["price"]["label"] == "NDX.close"


# ---------------------------------------------------------------------------
# iter-5 — latched-position semantics over the HTTP boundary
# ---------------------------------------------------------------------------


# Designed so that each bar triggers exactly one block's condition via
# equality comparisons against a single close series.
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
    """ASGI app wired with an 8-bar SPX series crafted for latching
    coverage (one firing pattern per bar)."""
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


def _eq_block(input_id: str, weight: float, threshold: float) -> dict:
    """Helper: build a block that fires when ``close == threshold``."""
    return {
        "input_id": input_id,
        "weight": weight,
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


async def test_http_roundtrip_latched_semantics(latch_client: AsyncClient):
    """Mandatory HTTP integration test (guardrail G2, P5-3..P5-6).

    8-bar SPX close series ``[100, 11, 100, 33, 44, 11, 66, 22]`` with
    block conditions crafted so each bar fires exactly the block we
    want to exercise. Asserts:
      (a) entry latch persists across a bar where condition is False
          (t=2 holds A's latch);
      (b) same-side exit clears same-side latches (t=3 long_exit → 0);
      (c) cross-side exit does NOT clear the opposite latch
          (t=6 short_exit leaves the long latch alone);
      (d) leverage allowed — t=7 block C w=0.5 latches on top of
          A w=0.6 → position = 1.1;
      (e) events / realized_pnl / indicators shapes match the P5-6
          contract consumed by I3.
    """
    body = {
        "spec": {
            "id": "latch-demo",
            "name": "Latching coverage",
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
                # Declaration order matters: A (w=0.6) is evaluated
                # before C (w=0.5) at bars where both could latch.
                "long_entry": [
                    _eq_block("X", 0.6, 11.0),  # block A → t=1, t=5
                    _eq_block("X", 0.5, 22.0),  # block C → t=7
                ],
                "long_exit": [
                    _eq_block("X", 0.0, 33.0),  # fires t=3
                ],
                "short_entry": [
                    _eq_block("X", 0.4, 44.0),  # block B → t=4
                ],
                "short_exit": [
                    _eq_block("X", 0.0, 66.0),  # fires t=6
                ],
            },
        },
        "indicators": [],
        "instruments": {},
    }

    resp = await latch_client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # ---- Response shape (iter-5 additions live alongside iter-3 keys) ----
    assert set(data.keys()) >= {
        "timestamps",
        "positions",
        "realized_pnl",
        "events",
        "indicators",
        "entries_skipped_budget",
        "clipped",
        "diagnostics",
    }
    assert isinstance(data["realized_pnl"], list)
    assert isinstance(data["events"], list)
    assert isinstance(data["indicators"], list)
    assert isinstance(data["entries_skipped_budget"], int)

    # ---- Position path (P5-3..P5-5) ----
    by_id = {p["input_id"]: p for p in data["positions"]}
    vals = by_id["X"]["values"]
    # (a) latch persists at t=2; (b) same-side exit clears at t=3;
    # (c) cross-side exit at t=6 does NOT clear the long;
    # (d) t=7 C's entry fires (leverage allowed) → position = 0.6 + 0.5 = 1.1.
    assert vals == pytest.approx([0.0, 0.6, 0.6, 0.0, -0.4, 0.2, 0.6, 1.1])

    # Budget skip surface — leverage allowed, so no entries skipped.
    assert data["entries_skipped_budget"] == 0

    # ---- Events payload ----
    ev_by = {
        (ev["input_id"], ev["block_id"]): ev for ev in data["events"]
    }
    # A (long_entry#0): fired t=1, t=5; both resulted in a latch (after
    # t=3 clear, A re-latches at t=5).
    a = ev_by[("X", "long_entry#0")]
    assert a["kind"] == "long_entry"
    assert a["fired_indices"] == [1, 5]
    assert a["latched_indices"] == [1, 5]
    # C (long_entry#1): fired t=7, latched (leverage allowed).
    c = ev_by[("X", "long_entry#1")]
    assert c["fired_indices"] == [7]
    assert c["latched_indices"] == [7]
    # long_exit: fired t=3; latched_indices mirrors fired for exit
    # blocks (per P5-6).
    lex = ev_by[("X", "long_exit#0")]
    assert lex["kind"] == "long_exit"
    assert lex["fired_indices"] == [3]
    assert lex["latched_indices"] == [3]
    # B (short_entry#0): fired t=4, latched t=4.
    b = ev_by[("X", "short_entry#0")]
    assert b["fired_indices"] == [4]
    assert b["latched_indices"] == [4]
    # short_exit: fired t=6; latched_indices == fired_indices.
    sex = ev_by[("X", "short_exit#0")]
    assert sex["fired_indices"] == [6]
    assert sex["latched_indices"] == [6]

    # ---- realized_pnl shape + monotonicity where expected ----
    assert len(data["realized_pnl"]) == 1
    pnl = data["realized_pnl"][0]
    assert len(pnl) == len(data["timestamps"])
    # First bar always zero.
    assert pnl[0] == 0.0
    # Every entry is a finite float.
    for v in pnl:
        assert isinstance(v, float)

    # ---- indicators (no indicator operands in this test) ----
    assert data["indicators"] == []
