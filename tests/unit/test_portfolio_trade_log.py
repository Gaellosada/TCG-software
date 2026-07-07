"""Tests for portfolio trade log aggregation.

Covers:
  1. ``Trade`` dataclass extension with optional ``holding_id``/``holding_name``.
  2. Portfolio aggregation surfaces ``trades`` + ``positions`` keys.
  3. Mixed-axis legs: per-signal bar indices are re-mapped to ``common_dates``
     and out-of-window trades are DROPPED (not clamped).
  4. Direct (non-signal) legs each synthesize one open "Holding" trade with
     their price series bubbled up into positions[].
  5. Signal-leg trade ``signed_weight`` is scaled by the signal leg's
     allocation weight before holding_id stamping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.api.portfolio import (
    _SignalLegEvalResult,
    _leg_multiplier_and_unit,
    _signal_input_collection,
    _signal_input_underlying_id,
)
from tcg.data._mongo.registry import CollectionRegistry
from tcg.types.market import PriceSeries
from tcg.types.signal import (
    InstrumentContinuous,
    InstrumentSpot,
    Trade,
)


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
        "SPX": _price_series(common_dates.tolist(), [100.0, 101.0, 102.0, 103.0, 104.0])
    }

    svc = MagicMock()
    svc._registry = registry
    svc.get_aligned_prices = AsyncMock(return_value=(common_dates, aligned_series))
    # Default: no roll boundaries re-surfaced (a continuous leg then shows a single
    # open→end roll row). Tests that exercise interior rolls override this.
    svc.get_continuous = AsyncMock(return_value=None)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = svc
    # app-data repo is resolved by get_write_repository but never
    # invoked here (no signal legs / signal eval is patched).
    app.state.app_db_repo = object()
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
    collection_by_input: dict | None = None,
) -> _SignalLegEvalResult:
    return _SignalLegEvalResult(
        index=dates,
        synthetic=prices,
        trades=trades,
        positions_payload=positions_payload,
        collection_by_input=collection_by_input or {},
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
    async def test_mixed_axis_legs_remap_and_drop(self, mock_eval, client: AsyncClient):
        """Two signal legs with different date overlaps. Trades whose
        endpoints fall outside common_dates are DROPPED (not clamped)."""
        # Leg A index: [20240102, 20240103, 20240104, 20240105]
        a_dates = np.array([20240102, 20240103, 20240104, 20240105], dtype=np.int64)
        a_prices = np.full(4, 100.0, dtype=np.float64)
        # Leg B index: [20240103, 20240104, 20240105, 20240108]
        b_dates = np.array([20240103, 20240104, 20240105, 20240108], dtype=np.int64)
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

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_cross_leg_shared_input_id_deduplicates_positions(
        self, mock_eval, client: AsyncClient
    ):
        """Two signal legs both referencing the same input_id 'AAPL'.
        Positions must be de-duplicated (first-leg-wins per §5.3.2 step 5);
        trades from both legs must appear with distinct holding_ids."""
        # Both legs share the full common_dates window so bar indices align 1:1.
        shared_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64
        )
        shared_prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)
        leg_a_price_values = [100.0, 101.0, 102.0, 103.0, 104.0]
        leg_b_price_values = [
            200.0,
            201.0,
            202.0,
            203.0,
            204.0,
        ]  # different — first wins

        leg_a_trades = (
            Trade(
                input_id="AAPL",
                entry_block_id="EA",
                entry_block_name="entry-a",
                exit_block_id="XA",
                exit_block_name="exit-a",
                open_bar=0,
                close_bar=2,
                direction="long",
                signed_weight=1.0,
            ),
        )
        leg_b_trades = (
            Trade(
                input_id="AAPL",
                entry_block_id="EB",
                entry_block_name="entry-b",
                exit_block_id="XB",
                exit_block_name="exit-b",
                open_bar=3,
                close_bar=4,
                direction="long",
                signed_weight=0.5,
            ),
        )

        leg_a_result = _leg_result(
            shared_dates,
            shared_prices,
            leg_a_trades,
            (
                {
                    "input_id": "AAPL",
                    "price": {"label": "AAPL.close", "values": leg_a_price_values},
                },
            ),
        )
        leg_b_result = _leg_result(
            shared_dates,
            shared_prices,
            leg_b_trades,
            (
                {
                    "input_id": "AAPL",
                    "price": {"label": "AAPL.close", "values": leg_b_price_values},
                },
            ),
        )
        mock_eval.side_effect = [leg_a_result, leg_b_result]

        body = {
            "legs": {
                "leg_a": {"type": "signal", "signal_spec": _minimal_signal_spec()},
                "leg_b": {"type": "signal", "signal_spec": _minimal_signal_spec()},
            },
            "weights": {"leg_a": 50, "leg_b": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()

        # Positions: exactly one entry for AAPL (de-duplicated).
        out_positions = data["positions"]
        assert len(out_positions) == 1
        assert out_positions[0]["input_id"] == "AAPL"

        # Price values come from the first leg (leg_a).
        price_vals = out_positions[0]["price"]["values"]
        # Projection is identity (leg shares common_dates fully) — values match leg_a.
        assert price_vals == leg_a_price_values

        # Trades: both legs contribute — two rows, distinct holding_ids.
        out_trades = data["trades"]
        assert len(out_trades) == 2
        holding_ids = {t["holding_id"] for t in out_trades}
        assert holding_ids == {"leg_a", "leg_b"}

    async def test_direct_only_portfolio_emits_holding_trades(
        self, client: AsyncClient
    ):
        """Portfolio with only direct (instrument) legs: each leg now
        contributes one synthesized Holding open trade, and its price
        series is bubbled up into positions[] (Sign 10 supersedes Sign 5).
        """
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
        assert len(data["trades"]) == 1
        tr = data["trades"][0]
        assert tr["entry_block_id"] == "holding"
        assert tr["entry_block_name"] == "Holding"
        assert tr["exit_block_id"] is None
        assert tr["exit_block_name"] is None
        assert tr["open_bar"] == 0
        assert tr["close_bar"] is None
        assert tr["direction"] == "long"
        # weight 100 (percent) → 1.0 (fraction).
        assert tr["signed_weight"] == pytest.approx(1.0)
        assert tr["holding_id"] == "SPX"
        assert tr["holding_name"] == "SPX"
        assert tr["input_id"] == "SPX"
        # Positions: SPX price bubbled up.
        assert len(data["positions"]) == 1
        pos = data["positions"][0]
        assert pos["input_id"] == "SPX"
        assert pos["price"] is not None
        assert len(pos["price"]["values"]) == len(data["dates"])

    # ── Open-trade aggregation (iter-4 regression) -------------------------
    #
    # The engine emits open trades wherever an entry block latched and
    # never closed — ``open_bar`` can be ANYWHERE in [0, n_sig-1], not
    # only at the signal's last bar (cf. engine
    # ``test_trades_open_at_end`` which produces open_bar=2 in a 5-bar
    # signal). Portfolio aggregation must keep these open trades as long
    # as their open date falls inside ``common_dates``.

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_open_trade_with_open_bar_before_signal_end_is_kept(
        self, mock_eval, client: AsyncClient
    ):
        """An open trade whose open_bar is NOT the signal's last bar but
        whose open date IS in common_dates must be kept (close_bar=None)
        — this is the realistic case the engine actually emits (e.g.
        ``test_trades_open_at_end`` produces open_bar=2 in 5-bar input).
        """
        # Signal's per-leg index spans the full mock common_dates (5 bars).
        sig_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108],
            dtype=np.int64,
        )
        sig_prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)
        # Single open trade with open_bar=1 (NOT the last bar, which is 4).
        trades = (
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry-1",
                exit_block_id=None,
                exit_block_name=None,
                open_bar=1,
                close_bar=None,
                direction="short",
                signed_weight=-0.25,
            ),
        )
        mock_eval.return_value = _leg_result(sig_dates, sig_prices, trades)

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

        out = data["trades"]
        assert len(out) == 1, "open trade with open_bar < n_sig-1 must NOT be dropped"
        tr = out[0]
        assert tr["close_bar"] is None
        # Signal index aligns 1:1 with common_dates, so open_bar=1 maps to 1.
        assert tr["open_bar"] == 1
        assert tr["direction"] == "short"
        # weight 100% → fraction 1.0; signed_weight scaled by leg fraction.
        assert tr["signed_weight"] == pytest.approx(-0.25)
        assert tr["entry_block_id"] == "E1"
        assert tr["holding_id"] == "sig1"

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_open_trade_with_open_bar_outside_common_dates_is_dropped(
        self, mock_eval, client: AsyncClient
    ):
        """An open trade whose open date is OUTSIDE common_dates must be
        dropped — we can't place it on the portfolio's date axis.

        Use two signal legs with different per-signal date grids so the
        intersection (= common_dates) excludes 20231229. Leg A emits the
        problematic open trade at 20231229 (its own bar 0) — this date
        is not in common_dates and the trade must be dropped. Leg B
        emits no trades, so the response's ``trades`` list is empty.
        """
        # Leg A index includes 20231229 (which falls outside common_dates
        # since Leg B does not have it).
        a_dates = np.array(
            [20231229, 20240102, 20240103, 20240104, 20240105],
            dtype=np.int64,
        )
        a_prices = np.array([99.0, 100.0, 101.0, 102.0, 103.0], dtype=np.float64)
        # Leg B index: starts at 20240102 (so 20231229 is NOT in common).
        b_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108],
            dtype=np.int64,
        )
        b_prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)
        # common_dates = [20240102, 20240103, 20240104, 20240105].

        # Leg A open trade at its own bar 0 (date 20231229 — OUTSIDE
        # common_dates). Must be dropped.
        a_trades = (
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry-1",
                exit_block_id=None,
                exit_block_name=None,
                open_bar=0,
                close_bar=None,
                direction="long",
                signed_weight=1.0,
            ),
        )

        mock_eval.side_effect = [
            _leg_result(a_dates, a_prices, a_trades),
            _leg_result(b_dates, b_prices, ()),
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

        # The leg-A open trade is dropped (open date 20231229 isn't in
        # common_dates); leg-B emits no trades. No signal-leg trades survive.
        assert data["trades"] == []

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_open_trade_at_signal_last_bar_still_works(
        self, mock_eval, client: AsyncClient
    ):
        """Regression: the case the OLD condition already handled —
        ``open_bar == n_sig - 1`` with the open date being the last
        common_date — must continue to be kept after the fix."""
        sig_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108],
            dtype=np.int64,
        )
        sig_prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)
        # Open trade at the signal's LAST bar (index 4 == n_sig - 1).
        trades = (
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry-1",
                exit_block_id=None,
                exit_block_name=None,
                open_bar=4,
                close_bar=None,
                direction="long",
                signed_weight=1.0,
            ),
        )
        mock_eval.return_value = _leg_result(sig_dates, sig_prices, trades)

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

        out = data["trades"]
        assert len(out) == 1
        tr = out[0]
        assert tr["close_bar"] is None
        assert tr["open_bar"] == 4  # last index of common_dates
        assert tr["signed_weight"] == pytest.approx(1.0)


# ── Holding-trade synthesis for non-signal legs (Sign 10/11) ----------------


class TestHoldingTradeSynthesis:
    async def test_holding_trade_for_instrument_leg_long(self, client: AsyncClient):
        """Single direct instrument leg at weight 0.7 emits one Holding
        open trade pointing at the SPX positions entry."""
        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SPX",
                }
            },
            # body.weights is in PERCENT units (frontend default 100).
            # 70 percent → signed_weight 0.7 (FRACTION) on the trade.
            "weights": {"SPX": 70},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["trades"]) == 1
        tr = data["trades"][0]
        assert tr["entry_block_name"] == "Holding"
        assert tr["entry_block_id"] == "holding"
        assert tr["close_bar"] is None
        assert tr["signed_weight"] == pytest.approx(0.7)
        assert tr["direction"] == "long"
        assert tr["holding_id"] == "SPX"
        assert tr["holding_name"] == "SPX"
        assert tr["input_id"] == "SPX"
        # input_id appears in positions.
        position_ids = {p["input_id"] for p in data["positions"]}
        assert "SPX" in position_ids

    async def test_holding_trade_for_instrument_leg_short(self, client: AsyncClient):
        """Negative weight → direction='short', signed_weight preserves sign."""
        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SPX",
                }
            },
            # -40 percent → signed_weight -0.4 (FRACTION).
            "weights": {"SPX": -40},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["trades"]) == 1
        tr = data["trades"][0]
        assert tr["direction"] == "short"
        assert tr["signed_weight"] == pytest.approx(-0.4)
        assert tr["holding_id"] == "SPX"

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_leg_signed_weight_scaled_by_leg_allocation(
        self, mock_eval, client: AsyncClient
    ):
        """A signal leg at allocation 50 percent emitting trades with
        internal signed_weight 1.0 yields portfolio trades with
        signed_weight 0.5 (FRACTION units)."""
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
            "weights": {"sig1": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()
        out_trades = data["trades"]
        assert len(out_trades) == 1
        assert out_trades[0]["signed_weight"] == pytest.approx(0.5)
        # holding_id stamped AFTER scaling.
        assert out_trades[0]["holding_id"] == "sig1"

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_mixed_portfolio_signal_and_direct(
        self, mock_eval, client: AsyncClient
    ):
        """One signal leg at 60% + one direct leg at 40%: response has both
        a synthesized Holding trade (direct) and scaled signal trades.
        Trade signed_weights are FRACTIONS (0.6, 0.4 * 1.0)."""
        sig_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64
        )
        sig_prices = np.full(5, 100.0, dtype=np.float64)
        trades = (
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry-1",
                exit_block_id="X1",
                exit_block_name="exit-1",
                open_bar=1,
                close_bar=3,
                direction="long",
                signed_weight=1.0,
            ),
        )
        mock_eval.return_value = _leg_result(sig_dates, sig_prices, trades, ())

        body = {
            "legs": {
                "sig1": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                },
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SPX",
                },
            },
            "weights": {"sig1": 60, "SPX": 40},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()
        out = data["trades"]
        assert len(out) == 2

        holdings = {t["holding_id"]: t for t in out}
        assert "sig1" in holdings and "SPX" in holdings

        holding_direct = holdings["SPX"]
        assert holding_direct["entry_block_name"] == "Holding"
        assert holding_direct["close_bar"] is None
        assert holding_direct["signed_weight"] == pytest.approx(0.4)
        assert holding_direct["open_bar"] == 0

        signal_trade = holdings["sig1"]
        assert signal_trade["entry_block_name"] == "entry-1"
        assert signal_trade["signed_weight"] == pytest.approx(1.0 * 0.6)

        # Sorted by (open_bar, entry_block_id): Holding trade has open_bar=0
        # and entry_block_id="holding", signal trade has open_bar=1 → Holding
        # appears first.
        assert out[0]["holding_id"] == "SPX"
        assert out[1]["holding_id"] == "sig1"

    async def test_continuous_leg_no_rolls_emits_single_open_end_row(
        self, mock_app, client: AsyncClient
    ):
        """Continuous-futures leg with NO re-surfaced roll boundaries (fixture
        get_continuous → None) emits ONE display-only roll row spanning the whole
        window: entry 'open', exit 'end' (NOT the old 'Holding' row).

        The fixture's AsyncMock get_aligned_prices returns its canned series under
        key 'SPX' regardless of the leg spec — so the leg label here is 'SPX' to
        match. input_id resolves to leg.collection.
        """
        body = {
            "legs": {
                "SPX": {
                    "type": "continuous",
                    "collection": "FUT_VIX",
                    "strategy": "front_month",
                }
            },
            # 60 percent → 0.6 fraction on the roll row.
            "weights": {"SPX": 60},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["trades"]) == 1
        tr = data["trades"][0]
        assert tr["entry_block_name"] == "open"
        assert tr["exit_block_name"] == "end"
        assert tr["entry_block_id"] == "roll:SPX"
        assert tr["exit_block_id"] == "roll:SPX"
        assert tr["open_bar"] == 0
        # Whole-window segment → close at the LAST bar (no longer None).
        assert tr["close_bar"] == len(data["dates"]) - 1
        assert tr["direction"] == "long"
        assert tr["signed_weight"] == pytest.approx(0.6)
        assert tr["holding_id"] == "SPX"
        assert tr["roll_hover"] == "rolling FUT_VIX"
        assert "_roll_row" not in tr  # internal build marker is stripped
        # continuous → input_id = leg.collection
        assert tr["input_id"] == "FUT_VIX"
        position_ids = {p["input_id"] for p in data["positions"]}
        assert "FUT_VIX" in position_ids


# ── Weight unit conversion (PERCENT → FRACTION on signed_weight) -----------


class TestWeightAsFraction:
    """``body.weights[label]`` is in PERCENT units. Trade ``signed_weight``
    is always in FRACTION units. The aggregation layer divides by 100."""

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_trade_signed_weight_at_60pct_is_fraction(
        self, mock_eval, client: AsyncClient
    ):
        """Leg weight 60 (percent), signal trade signed_weight 1.0 →
        portfolio trade signed_weight exactly 0.6 (FRACTION)."""
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
        )
        mock_eval.return_value = _leg_result(sig_dates, sig_prices, trades, ())

        body = {
            "legs": {
                "sig1": {
                    "type": "signal",
                    "signal_spec": _minimal_signal_spec(),
                }
            },
            "weights": {"sig1": 60},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        out_trades = resp.json()["trades"]
        assert len(out_trades) == 1
        # 60 percent × 1.0 = 0.6 — guard against forgetting the /100 div.
        assert out_trades[0]["signed_weight"] == pytest.approx(0.6)
        # Belt-and-braces: NOT 60.0 (the percent form).
        assert out_trades[0]["signed_weight"] != pytest.approx(60.0)

    async def test_holding_trade_signed_weight_at_40pct_is_fraction(
        self, client: AsyncClient
    ):
        """Direct leg at weight 40 (percent) → Holding trade
        signed_weight = 0.4 (FRACTION)."""
        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SPX",
                }
            },
            "weights": {"SPX": 40},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        tr = resp.json()["trades"][0]
        assert tr["signed_weight"] == pytest.approx(0.4)
        assert tr["direction"] == "long"

    async def test_holding_trade_short_at_neg_25pct(self, client: AsyncClient):
        """Direct leg at weight -25 (percent) → Holding trade
        signed_weight = -0.25, direction='short'."""
        body = {
            "legs": {
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SPX",
                }
            },
            "weights": {"SPX": -25},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200
        tr = resp.json()["trades"][0]
        assert tr["signed_weight"] == pytest.approx(-0.25)
        assert tr["direction"] == "short"


# ── Signal input_id → underlying instrument id remap (Fix B) ---------------


class TestSignalInputUnderlyingHelper:
    def test_spot_returns_instrument_id(self):
        inst = InstrumentSpot(collection="INDEX", instrument_id="SPX")
        assert _signal_input_underlying_id(inst) == "SPX"

    def test_continuous_returns_collection(self):
        inst = InstrumentContinuous(collection="FUT_VIX", adjustment="none")
        assert _signal_input_underlying_id(inst) == "FUT_VIX"

    def test_unknown_variant_returns_none(self):
        # Helper is defensive: returns None so caller can fall back.
        assert _signal_input_underlying_id(object()) is None


class TestSignalTradeInputIdRemap:
    """Signal-leg trades carry the signal-INTERNAL ``input_id`` (e.g.
    "index"). At the portfolio layer we remap to the underlying
    instrument id (e.g. "SPX") so the TradeLog can lookup prices and
    so signal trades line up with direct-leg trades.

    These tests exercise the REAL ``_evaluate_signal_leg`` by mocking
    only the engine boundary (``evaluate_signal`` +
    ``compute_input_overlap`` + ``make_signal_fetcher``).
    """

    @staticmethod
    def _spx_signal_spec() -> dict:
        """Signal with a single Input bound to spot SPX (one trade)."""
        return {
            "spec": {
                "id": "s1",
                "name": "Test",
                "inputs": [
                    {
                        "id": "index",
                        "instrument": {
                            "type": "spot",
                            "collection": "INDEX",
                            "instrument_id": "SPX",
                        },
                    }
                ],
                "rules": {"entries": [], "exits": []},
            },
            "indicators": [],
        }

    @patch("tcg.core.api.portfolio.make_signal_fetcher")
    @patch("tcg.core.api.portfolio.compute_input_overlap", new_callable=AsyncMock)
    @patch("tcg.core.api.portfolio.evaluate_signal", new_callable=AsyncMock)
    async def test_signal_trade_input_id_remapped_to_underlying(
        self,
        mock_eval_signal,
        mock_overlap,
        mock_fetcher,
        client: AsyncClient,
    ):
        from datetime import date as date_t

        from tcg.engine.signal_exec import (
            InstrumentPositionResult,
            SignalEvalResult,
        )

        sig_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108],
            dtype=np.int64,
        )
        T = len(sig_dates)
        pos_values = np.zeros(T, dtype=np.float64)
        clipped = np.zeros(T, dtype=np.bool_)
        realized_pnl = np.zeros(T, dtype=np.float64)
        price_vals = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)

        # The engine emits the trade keyed by signal-local input id "index".
        engine_trade = Trade(
            input_id="index",
            entry_block_id="E1",
            entry_block_name="entry-1",
            exit_block_id="X1",
            exit_block_name="exit-1",
            open_bar=0,
            close_bar=2,
            direction="long",
            signed_weight=1.0,
        )
        engine_position = InstrumentPositionResult(
            input_id="index",
            instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
            values=pos_values,
            clipped_mask=clipped,
            realized_pnl=realized_pnl,
            price_label="SPX.close",
            price_values=price_vals,
        )
        mock_eval_signal.return_value = SignalEvalResult(
            index=sig_dates,
            positions=(engine_position,),
            clipped=False,
            events=(),
            indicator_series=(),
            diagnostics={},
            trades=(engine_trade,),
        )
        mock_overlap.return_value = (date_t(2024, 1, 2), date_t(2024, 1, 8))
        mock_fetcher.return_value = MagicMock()

        body = {
            "legs": {
                "sig1": {
                    "type": "signal",
                    "signal_spec": self._spx_signal_spec(),
                }
            },
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Trade input_id remapped from signal-local "index" → underlying "SPX".
        out_trades = data["trades"]
        assert len(out_trades) == 1
        assert out_trades[0]["input_id"] == "SPX"
        # And positions[] mirrors the same remap.
        position_ids = {p["input_id"] for p in data["positions"]}
        assert "SPX" in position_ids
        assert "index" not in position_ids

    @patch("tcg.core.api.portfolio.make_signal_fetcher")
    @patch("tcg.core.api.portfolio.compute_input_overlap", new_callable=AsyncMock)
    @patch("tcg.core.api.portfolio.evaluate_signal", new_callable=AsyncMock)
    async def test_shared_underlying_dedups_across_signal_and_direct(
        self,
        mock_eval_signal,
        mock_overlap,
        mock_fetcher,
        client: AsyncClient,
    ):
        """A signal whose "index" input binds to SPX + a direct SPX leg →
        positions[] has EXACTLY ONE entry for SPX (first-leg-wins on the
        underlying id, not the signal-local id)."""
        from datetime import date as date_t

        from tcg.engine.signal_exec import (
            InstrumentPositionResult,
            SignalEvalResult,
        )

        sig_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108],
            dtype=np.int64,
        )
        T = len(sig_dates)
        pos_values = np.zeros(T, dtype=np.float64)
        clipped = np.zeros(T, dtype=np.bool_)
        realized_pnl = np.zeros(T, dtype=np.float64)
        # Distinctive price series so we can tell which leg won the dedup.
        sig_price_vals = np.array([999.0, 999.0, 999.0, 999.0, 999.0], dtype=np.float64)

        engine_position = InstrumentPositionResult(
            input_id="index",
            instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
            values=pos_values,
            clipped_mask=clipped,
            realized_pnl=realized_pnl,
            price_label="SPX.close",
            price_values=sig_price_vals,
        )
        mock_eval_signal.return_value = SignalEvalResult(
            index=sig_dates,
            positions=(engine_position,),
            clipped=False,
            events=(),
            indicator_series=(),
            diagnostics={},
            trades=(),
        )
        mock_overlap.return_value = (date_t(2024, 1, 2), date_t(2024, 1, 8))
        mock_fetcher.return_value = MagicMock()

        # Signal leg processed FIRST (dict insertion order) — its remapped
        # SPX positions entry wins. Then the direct SPX leg sees "SPX" in
        # ``seen_inputs`` and skips.
        body = {
            "legs": {
                "sig1": {
                    "type": "signal",
                    "signal_spec": self._spx_signal_spec(),
                },
                "SPX": {
                    "type": "instrument",
                    "collection": "INDEX",
                    "symbol": "SPX",
                },
            },
            "weights": {"sig1": 50, "SPX": 50},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        spx_positions = [p for p in data["positions"] if p["input_id"] == "SPX"]
        assert len(spx_positions) == 1, (
            "expected exactly one SPX position entry after dedup, "
            f"got {len(data['positions'])}: {data['positions']}"
        )
        # Signal leg won → its 999.0 price values are kept.
        assert spx_positions[0]["price"]["values"][0] == pytest.approx(999.0)


# ── _leg_multiplier_and_unit / _signal_input_collection helpers ------------


class TestMultiplierAndUnitHelper:
    def test_futures_collection_uses_m_fut_contracts(self):
        m, unit = _leg_multiplier_and_unit("FUT_SP_500")
        assert unit == "contracts"
        assert m == pytest.approx(50.0)

    def test_option_collection_uses_m_opt_contracts(self):
        # VIX is the root where m_fut (1000) != m_opt (100) — verify OPT_ picks
        # m_opt, proving the FUT/OPT split is honored.
        m, unit = _leg_multiplier_and_unit("OPT_VIX")
        assert unit == "contracts"
        assert m == pytest.approx(100.0)

    def test_futures_collection_uses_m_fut_not_m_opt_for_vix(self):
        m, unit = _leg_multiplier_and_unit("FUT_VIX")
        assert unit == "contracts"
        assert m == pytest.approx(1000.0)

    def test_spot_collection_is_shares_multiplier_one(self):
        m, unit = _leg_multiplier_and_unit("INDEX")
        assert unit == "shares"
        assert m == pytest.approx(1.0)

    def test_none_collection_is_shares_multiplier_one(self):
        m, unit = _leg_multiplier_and_unit(None)
        assert unit == "shares"
        assert m == pytest.approx(1.0)

    def test_unknown_futures_root_returns_none_multiplier_contracts(self):
        # Unknown FUT root with no config + no live contract_size → M None
        # (NEVER a silent 1.0), unit still flagged "contracts" for the label.
        m, unit = _leg_multiplier_and_unit("FUT_TOTALLY_UNKNOWN_ROOT")
        assert unit == "contracts"
        assert m is None


class TestSignalInputCollectionHelper:
    def test_spot_returns_collection(self):
        inst = InstrumentSpot(collection="INDEX", instrument_id="SPX")
        assert _signal_input_collection(inst) == "INDEX"

    def test_continuous_returns_collection(self):
        inst = InstrumentContinuous(collection="FUT_SP_500", adjustment="none")
        assert _signal_input_collection(inst) == "FUT_SP_500"

    def test_unknown_variant_returns_none(self):
        assert _signal_input_collection(object()) is None


# ── Per-trade fractional contract/asset COUNT (quantity) -------------------


class TestTradeQuantitySizing:
    """quantity = |signed_weight| * NAV_open / (price_open * M)."""

    async def test_futures_direct_leg_quantity_and_unit(self, client: AsyncClient):
        """Direct continuous FUT_SP_500 leg: quantity uses M=50, unit
        'contracts', and matches the hand formula against the returned
        equity + price."""
        body = {
            "legs": {
                # Label MUST be 'SPX' — the fixture's get_aligned_prices returns
                # its canned series under that key regardless of the leg spec.
                "SPX": {
                    "type": "continuous",
                    "collection": "FUT_SP_500",
                    "strategy": "front_month",
                }
            },
            "weights": {"SPX": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["trades"]) == 1
        tr = data["trades"][0]
        assert tr["quantity_unit"] == "contracts"
        assert tr["multiplier"] == pytest.approx(50.0)

        equity = data["portfolio_equity"]
        price_vals = next(
            p["price"]["values"]
            for p in data["positions"]
            if p["input_id"] == "FUT_SP_500"
        )
        ob = tr["open_bar"]
        expected = abs(tr["signed_weight"]) * equity[ob] / (price_vals[ob] * 50.0)
        assert tr["quantity"] == pytest.approx(expected)
        # equity[0]=100 (100-based index), price[0]=100, w=1.0 → 100/5000 = 0.02
        assert tr["quantity"] == pytest.approx(0.02)

    async def test_spot_equity_leg_is_shares_multiplier_one(self, client: AsyncClient):
        """Direct spot/equity leg (collection INDEX): M=1.0, unit 'shares'."""
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
        assert resp.status_code == 200, resp.text
        data = resp.json()
        tr = data["trades"][0]
        assert tr["quantity_unit"] == "shares"
        assert tr["multiplier"] == pytest.approx(1.0)
        equity = data["portfolio_equity"]
        price_vals = next(
            p["price"]["values"] for p in data["positions"] if p["input_id"] == "SPX"
        )
        ob = tr["open_bar"]
        expected = abs(tr["signed_weight"]) * equity[ob] / (price_vals[ob] * 1.0)
        assert tr["quantity"] == pytest.approx(expected)
        # equity[0]=100, price[0]=100, w=1.0 → 1.0 share.
        assert tr["quantity"] == pytest.approx(1.0)

    async def test_unknown_futures_root_yields_null_quantity(self, client: AsyncClient):
        """A FUT_ leg whose root is absent from the multiplier config →
        multiplier None and quantity null (never a silent 1.0), no crash.
        The JSON carries a real null (not NaN)."""
        body = {
            "legs": {
                "SPX": {
                    "type": "continuous",
                    "collection": "FUT_TOTALLY_UNKNOWN_ROOT",
                    "strategy": "front_month",
                }
            },
            "weights": {"SPX": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        assert "NaN" not in resp.text  # RFC-8259: null, never NaN.
        data = resp.json()
        tr = data["trades"][0]
        assert tr["quantity_unit"] == "contracts"
        assert tr["multiplier"] is None
        assert tr["quantity"] is None

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_zero_price_yields_null_quantity(
        self, mock_eval, client: AsyncClient
    ):
        """price_open <= 0 → quantity null even when M is valid."""
        sig_dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
        sig_prices = np.array([100.0, 101.0, 103.0], dtype=np.float64)
        trades = (
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry",
                exit_block_id=None,
                exit_block_name=None,
                open_bar=0,
                close_bar=None,
                direction="long",
                signed_weight=1.0,
            ),
        )
        positions_payload = (
            {
                "input_id": "X",
                "price": {"label": "P", "values": [0.0, 101.0, 103.0]},
            },
        )
        mock_eval.return_value = _leg_result(
            sig_dates,
            sig_prices,
            trades,
            positions_payload,
            collection_by_input={"X": "FUT_SP_500"},
        )
        body = {
            "legs": {"sig1": {"type": "signal", "signal_spec": _minimal_signal_spec()}},
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        assert "NaN" not in resp.text
        data = resp.json()
        tr = data["trades"][0]
        assert tr["multiplier"] == pytest.approx(50.0)
        assert tr["quantity"] is None

    @patch(
        "tcg.core.api.portfolio._evaluate_signal_leg",
        new_callable=AsyncMock,
    )
    async def test_signal_leg_two_entries_have_differing_quantities(
        self, mock_eval, client: AsyncClient
    ):
        """KEY property: two entries at different NAV (equity rises) with a
        constant price → the two contract COUNTS differ (proves the size is
        no longer the constant target %)."""
        sig_dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
        # Synthetic drives the equity (rises 100→110→121). Positions price is a
        # SEPARATE constant series so NAV/price ratio changes between entries.
        sig_prices = np.array([100.0, 110.0, 121.0], dtype=np.float64)
        trades = (
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry",
                exit_block_id="X1",
                exit_block_name="exit",
                open_bar=0,
                close_bar=1,
                direction="long",
                signed_weight=1.0,
            ),
            Trade(
                input_id="X",
                entry_block_id="E1",
                entry_block_name="entry",
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
                "price": {"label": "P", "values": [100.0, 100.0, 100.0]},
            },
        )
        mock_eval.return_value = _leg_result(
            sig_dates,
            sig_prices,
            trades,
            positions_payload,
            collection_by_input={"X": "FUT_SP_500"},
        )
        body = {
            "legs": {"sig1": {"type": "signal", "signal_spec": _minimal_signal_spec()}},
            "weights": {"sig1": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        out = sorted(data["trades"], key=lambda t: t["open_bar"])
        assert len(out) == 2
        equity = data["portfolio_equity"]
        price_vals = next(
            p["price"]["values"] for p in data["positions"] if p["input_id"] == "X"
        )
        for tr in out:
            ob = tr["open_bar"]
            assert tr["quantity_unit"] == "contracts"
            assert tr["multiplier"] == pytest.approx(50.0)
            expected = abs(tr["signed_weight"]) * equity[ob] / (price_vals[ob] * 50.0)
            assert tr["quantity"] == pytest.approx(expected)
        # The whole point: the two counts are NOT equal (equity rose, price flat).
        assert out[0]["quantity"] != pytest.approx(out[1]["quantity"])
        assert out[1]["quantity"] > out[0]["quantity"]


# ── Per-held-contract ROLL rows for rolling direct legs (continuous) --------


def _continuous_series(roll_dates: tuple[int, ...]) -> "object":
    """Minimal ``ContinuousSeries`` whose only field the portfolio path reads is
    ``roll_dates`` (the trade-log roll-boundary re-surfacing). Prices/contracts are
    dummies."""
    from tcg.types.market import (
        AdjustmentMethod,
        ContinuousRollConfig,
        ContinuousSeries,
        RollStrategy,
    )

    prices = _price_series([20240102], [100.0])
    return ContinuousSeries(
        collection="FUT_SP_500",
        roll_config=ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH,
            adjustment=AdjustmentMethod.NONE,
        ),
        prices=prices,
        roll_dates=roll_dates,
        contracts=tuple(f"C{i}" for i in range(len(roll_dates) + 1)),
    )


class TestContinuousRollRows:
    """A continuous-futures leg emits ONE display-only trade row per held contract
    (open / rolling… / end), sized in contracts, with a per-segment realised P&L —
    and NONE of it perturbs equity/metrics/returns (the display-only hard gate)."""

    async def test_continuous_leg_with_one_roll_emits_two_segment_rows(
        self, mock_app, client: AsyncClient
    ):
        # Fixture common_dates: [02,03,04,05,08]; closes [100,101,102,103,104].
        # One interior roll on 20240104 → bar 2 → segments [0,1] and [2,4].
        mock_app.state.market_data.get_continuous = AsyncMock(
            return_value=_continuous_series((20240104,))
        )
        body = {
            "legs": {
                "SPX": {
                    "type": "continuous",
                    "collection": "FUT_SP_500",
                    "strategy": "front_month",
                }
            },
            "weights": {"SPX": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        rows = sorted(data["trades"], key=lambda t: t["open_bar"])
        assert len(rows) == 2

        r0, r1 = rows
        # Reasons: row0 open→rolling; last row rolling→end (decision 1).
        assert (r0["entry_block_name"], r0["exit_block_name"]) == ("open", "rolling")
        assert (r1["entry_block_name"], r1["exit_block_name"]) == ("rolling", "end")
        # Boundaries == ContinuousSeries.roll_dates mapped to bars.
        assert (r0["open_bar"], r0["close_bar"]) == (0, 1)
        assert (r1["open_bar"], r1["close_bar"]) == (2, 4)
        # Hover text = "rolling <input name>" (the collection, not a contract).
        for r in rows:
            assert r["roll_hover"] == "rolling FUT_SP_500"
            assert r["entry_block_id"] == "roll:SPX"
            assert r["exit_block_id"] == "roll:SPX"
            assert r["quantity_unit"] == "contracts"
            assert r["multiplier"] == pytest.approx(50.0)
            assert "_roll_row" not in r

        equity = data["portfolio_equity"]
        price_vals = next(
            p["price"]["values"]
            for p in data["positions"]
            if p["input_id"] == "FUT_SP_500"
        )
        # quantity = |w| * NAV_open / (price_open * M) (§10.5 formula reused).
        for r in rows:
            ob = r["open_bar"]
            exp_q = abs(r["signed_weight"]) * equity[ob] / (price_vals[ob] * 50.0)
            assert r["quantity"] == pytest.approx(exp_q)
        # per-segment P&L = sign * qty * (price_close - price_open) * M.
        for r in rows:
            ob, cb = r["open_bar"], r["close_bar"]
            exp_pnl = r["quantity"] * (price_vals[cb] - price_vals[ob]) * 50.0
            assert r["segment_pnl"] == pytest.approx(exp_pnl)

    async def test_roll_dates_outside_common_dates_are_dropped(
        self, mock_app, client: AsyncClient
    ):
        # 20240104 is in common_dates (bar 2); 20240106 is NOT a trading day in the
        # fixture window → dropped (remap-drop). → 2 segments, not 3.
        mock_app.state.market_data.get_continuous = AsyncMock(
            return_value=_continuous_series((20240104, 20240106))
        )
        body = {
            "legs": {
                "SPX": {
                    "type": "continuous",
                    "collection": "FUT_SP_500",
                    "strategy": "front_month",
                }
            },
            "weights": {"SPX": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        rows = resp.json()["trades"]
        assert len(rows) == 2

    async def test_roll_rows_are_display_only_equity_metrics_byte_identical(
        self, mock_app, client: AsyncClient
    ):
        """HARD GATE: adding per-roll display rows must leave equity + metrics +
        monthly/yearly returns byte-identical to the pure engine output computed on
        the same aligned closes."""
        from dataclasses import asdict

        from tcg.core.api._serializers import sanitize_json_floats
        from tcg.engine import (
            aggregate_returns,
            compute_metrics,
            compute_weighted_portfolio,
        )

        # Roll rows ARE generated (one interior roll) so this proves they don't feed
        # back into the numeric outputs.
        mock_app.state.market_data.get_continuous = AsyncMock(
            return_value=_continuous_series((20240104,))
        )
        body = {
            "legs": {
                "SPX": {
                    "type": "continuous",
                    "collection": "FUT_SP_500",
                    "strategy": "front_month",
                }
            },
            "weights": {"SPX": 100},
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Sanity: the display feature actually produced roll rows.
        assert any(t.get("entry_block_id") == "roll:SPX" for t in data["trades"])

        common_dates = np.array(
            [20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64
        )
        closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)
        res = compute_weighted_portfolio(
            {"SPX": closes}, {"SPX": 100}, "none", "normal", common_dates
        )
        assert data["portfolio_equity"] == sanitize_json_floats(
            res.portfolio_equity.tolist()
        )
        assert data["metrics"] == sanitize_json_floats(
            asdict(compute_metrics(res.portfolio_equity, return_type="normal"))
        )
        mo = aggregate_returns(
            common_dates,
            res.portfolio_returns,
            res.per_leg_returns,
            "normal",
            "monthly",
        )
        yr = aggregate_returns(
            common_dates, res.portfolio_returns, res.per_leg_returns, "normal", "yearly"
        )
        assert data["monthly_returns"] == sanitize_json_floats(mo)
        assert data["yearly_returns"] == sanitize_json_floats(yr)
