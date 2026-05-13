"""Tests for portfolio trade log aggregation (§5.4 of CONTRACT).

Covers:
  1. ``Trade`` dataclass extension with optional ``holding_id``/``holding_name``.
  2. Portfolio aggregation surfaces ``trades`` + ``positions`` keys.
  3. Mixed-axis legs: per-signal bar indices are re-mapped to ``common_dates``
     and out-of-window trades are DROPPED (not clamped).
  4. No-signal portfolios still emit empty ``trades`` / ``positions`` arrays.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.api.portfolio import _SignalLegEvalResult
from tcg.data._mongo.registry import CollectionRegistry
from tcg.types.market import PriceSeries
from tcg.types.signal import Trade


# ── Trade schema -----------------------------------------------------------


class TestTradeSchema:
    def test_trade_without_holding_fields_keeps_defaults(self):
        tr = Trade(
            input_id="X",
            entry_block_id="E1",
            entry_block_name="entry",
            exit_block_id=None,
            exit_block_name=None,
            open_bar=2,
            close_bar=None,
            direction="long",
            signed_weight=0.5,
        )
        assert tr.holding_id is None
        assert tr.holding_name is None

    def test_trade_with_holding_fields_round_trips(self):
        tr = Trade(
            input_id="X",
            entry_block_id="E1",
            entry_block_name="entry",
            exit_block_id="X1",
            exit_block_name="exit",
            open_bar=2,
            close_bar=5,
            direction="short",
            signed_weight=-0.25,
            holding_id="my_leg",
            holding_name="my_leg",
        )
        assert tr.holding_id == "my_leg"
        assert tr.holding_name == "my_leg"
        # Existing fields untouched.
        assert tr.signed_weight == -0.25
        assert tr.direction == "short"

    def test_trade_field_order_preserved(self):
        """Holding fields appended at end so positional construction up to
        the original 9 fields still works (backward compat)."""
        tr = Trade("X", "E", "entry", None, None, 0, None, "long", 1.0)
        assert tr.input_id == "X"
        assert tr.signed_weight == 1.0
        assert tr.holding_id is None
        assert tr.holding_name is None


# ── Portfolio aggregation integration --------------------------------------


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


def _minimal_signal_spec() -> dict:
    return {
        "spec": {
            "id": "s1",
            "name": "Test Signal",
            "inputs": [],
            "rules": {"entries": [], "exits": []},
        },
        "indicators": [],
    }


@pytest.fixture
def mock_app():
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.types.errors import TCGError

    registry = CollectionRegistry(["INDEX", "FUT_VIX", "FUT_SP_500", "ETF"])
    common_dates = np.array(
        [20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64
    )
    aligned_series = {
        "SPX": _price_series(
            common_dates.tolist(), [100.0, 101.0, 102.0, 103.0, 104.0]
        )
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


def _leg_result(
    dates: np.ndarray,
    prices: np.ndarray,
    trades: tuple = (),
    positions_payload: tuple = (),
) -> _SignalLegEvalResult:
    return _SignalLegEvalResult(
        index=dates,
        synthetic=prices,
        trades=trades,
        positions_payload=positions_payload,
    )


class TestPortfolioAggregation:
    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_portfolio_aggregates_trades_and_positions(
        self, mock_eval, client: AsyncClient
    ):
        """One signal leg, two trades — verify response carries them."""
        sig_dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
        sig_prices = np.array([100.0, 101.0, 103.0], dtype=np.float64)
        trades = (
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry-1",
                exit_block_id="X1",
                exit_block_name="exit-1",
                open_bar=0,
                close_bar=1,
                direction="long",
                signed_weight=1.0,
            ),
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry-1",
                exit_block_id=None,
                exit_block_name=None,
                open_bar=2,
                close_bar=None,
                direction="long",
                signed_weight=1.0,
            ),
        )
        positions_payload = (
            {
                "input_id": "X",
                "price": {
                    "label": "SPX.close",
                    "values": [100.0, 101.0, 103.0],
                },
            },
        )
        mock_eval.return_value = _leg_result(
            sig_dates, sig_prices, trades, positions_payload
        )

        body = {
            "legs": {
                "sig1": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                }
            },
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()

        assert "trades" in data and "positions" in data
        out_trades = data["trades"]
        assert len(out_trades) == 2
        for t in out_trades:
            assert t["holding_id"] == "sig1"
            assert t["holding_name"] == "sig1"
            assert 0 <= t["open_bar"] < len(data["dates"])
            if t["close_bar"] is not None:
                assert 0 <= t["close_bar"] < len(data["dates"])

        out_positions = data["positions"]
        assert len(out_positions) == 1
        p = out_positions[0]
        assert p["input_id"] == "X"
        assert p["price"]["label"] == "SPX.close"
        assert len(p["price"]["values"]) == len(data["dates"])

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_mixed_axis_legs_remap_and_drop(
        self, mock_eval, client: AsyncClient
    ):
        """Two signal legs with different date overlaps. Trades whose
        endpoints fall outside common_dates are DROPPED (not clamped)."""
        # Leg A index: [20240102, 20240103, 20240104, 20240105]
        a_dates = np.array(
            [20240102, 20240103, 20240104, 20240105], dtype=np.int64
        )
        a_prices = np.full(4, 100.0, dtype=np.float64)
        # Leg B index: [20240103, 20240104, 20240105, 20240108]
        b_dates = np.array(
            [20240103, 20240104, 20240105, 20240108], dtype=np.int64
        )
        b_prices = np.full(4, 100.0, dtype=np.float64)
        # Common: [20240103, 20240104, 20240105]
        # ── Leg A trades:
        #   t1: open=0 (20240102), close=2 (20240104) → open OUT, DROP
        #   t2: open=1 (20240103), close=3 (20240105) → open=0, close=2 KEEP
        a_trades = (
            Trade(
                input_id="X",
                entry_block_id="EA",
                entry_block_name="a",
                exit_block_id="XA",
                exit_block_name="xa",
                open_bar=0,
                close_bar=2,
                direction="long",
                signed_weight=1.0,
            ),
            Trade(
                input_id="X",
                entry_block_id="EA",
                entry_block_name="a",
                exit_block_id="XA",
                exit_block_name="xa",
                open_bar=1,
                close_bar=3,
                direction="long",
                signed_weight=1.0,
            ),
        )
        # ── Leg B trades:
        #   t1: open=0 (20240103), close=3 (20240108) → close OUT, DROP
        #   t2: open=1 (20240104), close=2 (20240105) → KEEP
        b_trades = (
            Trade(
                input_id="Y",
                entry_block_id="EB",
                entry_block_name="b",
                exit_block_id="XB",
                exit_block_name="xb",
                open_bar=0,
                close_bar=3,
                direction="short",
                signed_weight=-0.5,
            ),
            Trade(
                input_id="Y",
                entry_block_id="EB",
                entry_block_name="b",
                exit_block_id="XB",
                exit_block_name="xb",
                open_bar=1,
                close_bar=2,
                direction="short",
                signed_weight=-0.5,
            ),
        )

        mock_eval.side_effect = [
            _leg_result(a_dates, a_prices, a_trades),
            _leg_result(b_dates, b_prices, b_trades),
        ]

        body = {
            "legs": {
                "leg_a": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
                "leg_b": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
            },
            "weights": {"leg_a": 50, "leg_b": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()

        # common_dates should have length 3.
        assert len(data["dates"]) == 3
        out = data["trades"]
        # 4 trades input, 2 kept (1 per leg).
        assert len(out) == 2

        by_leg = {t["holding_id"]: t for t in out}
        assert "leg_a" in by_leg and "leg_b" in by_leg

        # Leg A kept trade: remapped open=0, close=2.
        assert by_leg["leg_a"]["open_bar"] == 0
        assert by_leg["leg_a"]["close_bar"] == 2
        # Leg B kept trade: remapped open=1, close=2.
        assert by_leg["leg_b"]["open_bar"] == 1
        assert by_leg["leg_b"]["close_bar"] == 2

        # Sorted by (open_bar, entry_block_id).
        opens = [t["open_bar"] for t in out]
        assert opens == sorted(opens)

    async def test_no_signal_portfolio_emits_empty_trades_and_positions(
        self, client: AsyncClient
    ):
        """Portfolio with only direct (instrument) legs: ``trades`` and
        ``positions`` keys are present and empty."""
        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SPX",
                }
            },
            "weights": {"SPX": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["trades"] == []
        assert data["positions"] == []
