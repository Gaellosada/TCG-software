"""Tests for POST /api/portfolio/compute with option_stream legs.

Covers:
- Hold-mode mid (premium) leg participates in the equity curve (the hold path)
- A mid/bs_mid (premium) leg WITHOUT hold is REJECTED (a rolled option's
  daily-reselect %-return is not a valid equity series — see the LegSpec
  ``validate_option_price_leg_requires_hold`` model validator)
- Level-stream leg (iv/greeks) goes to tracking_series WITH hold off (unchanged)
- Mixed hold-price + level legs
- Level-only portfolio rejected
- roll_offset / adjustment field threading into the OptionStreamRef

NOTE on the removed "premium DISPLAY path" tests
-------------------------------------------------
A premium stream (``mid``/``bs_mid``) in a portfolio leg now REQUIRES hold-mode
(``hold_between_rolls=True``): the model validator rejects the no-hold case at
parse, so the non-hold price DISPLAY branch of ``_evaluate_option_stream_leg``
(forward-fill + the all-NaN ``_diagnostic_hint`` enrichment) is unreachable via a
valid portfolio request.  The former ``TestPortfolioOptionStreamAllNanDiagnostics``
and the ``test_nan_forward_fill`` tests exercised exactly that dead branch and
could no longer even construct their input LegSpec, so they were removed.  The
hold path has its own loud all-NaN guard, covered by
``test_api_portfolio_option_hold_pnl.py::test_all_nan_premium_rejected_loudly``
and (at the HTTP layer) by ``test_hold_mid_all_nan_rejected`` below.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.portfolio import router as portfolio_router
from tcg.data._mongo.registry import CollectionRegistry
from tcg.engine import compute_weighted_portfolio
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries
from tcg.types.signal import InstrumentOptionStream

from _hold_pnl_oracle import oracle_ratio


# ── Helpers ────────────────────────────────────────────────────────────

DATES = [20240102, 20240103, 20240104, 20240105, 20240108]
SPX_CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0]

# Hold-mode fixture: a simple monotone premium with a single roll on day 0, so a
# hold-mode mid leg resolves to a well-formed fixed-contract equity curve that
# overlaps the SPX dates above.
HOLD_PREMIUM = [5.0, 5.1, 5.2, 5.3, 5.4]
HOLD_IS_ROLL = [1.0, 0.0, 0.0, 0.0, 0.0]
HOLD_ROLL_PREMIUM = [5.0, np.nan, np.nan, np.nan, np.nan]


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


def _make_hold_fetcher(*, dates=None, premium=None, is_roll=None, roll_premium=None):
    """Build a synthetic ``make_signal_fetcher`` replacement (dwh-free) matching
    the real shape: a callable + ``.fetch_hold_roll_info``.  Overridable per test
    (e.g. all-NaN premium, or disjoint dates)."""
    d = np.array(dates if dates is not None else DATES, dtype=np.int64)
    prem = np.array(premium if premium is not None else HOLD_PREMIUM, dtype=np.float64)
    isr = np.array(is_roll if is_roll is not None else HOLD_IS_ROLL, dtype=np.float64)
    rollp = np.array(
        roll_premium if roll_premium is not None else HOLD_ROLL_PREMIUM,
        dtype=np.float64,
    )

    def factory(svc, start, end):
        async def fetch(instrument, field):
            assert isinstance(instrument, InstrumentOptionStream)
            assert instrument.hold_between_rolls is True
            return d, prem.copy()

        async def fetch_hold_roll_info(instrument):
            assert isinstance(instrument, InstrumentOptionStream)
            return d, isr.copy(), rollp.copy()

        fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
        return fetch

    return factory


# A mid (premium) leg WITHOUT hold — now REJECTED by the LegSpec model validator.
OPT_MID_LEG = {
    "type": "option_stream",
    "collection": "OPT_SP_500",
    "option_type": "C",
    "cycle": None,
    "maturity": {"kind": "next_third_friday", "offset_months": 0},
    "selection": {
        "kind": "by_delta",
        "target": 0.25,
        "tolerance": 0.1,
        "strict": False,
    },
    "stream": "mid",
}

# The valid premium leg for a portfolio: mid + hold-mode fixed-contract P&L.
HOLD_MID_LEG = {
    **OPT_MID_LEG,
    "hold_between_rolls": True,
    "nav_times": 1.0,
}

# A level (iv) leg — display-only overlay, valid WITH hold off (unchanged).
OPT_IV_LEG = {
    **OPT_MID_LEG,
    "stream": "iv",
}

SPX_LEG = {
    "type": "instrument",
    "collection": "INDEX",
    "symbol": "SP500",
}


@pytest.fixture
def mock_app(monkeypatch):
    """FastAPI app with mocked data service + option materialisation (level legs)
    + a synthetic hold-mode fetcher (premium hold legs)."""
    registry = CollectionRegistry(["INDEX", "OPT_SP_500"])

    common_dates = np.array(DATES, dtype=np.int64)
    aligned_series = {
        "SPX": _price_series(DATES, SPX_CLOSES),
    }

    svc = MagicMock()
    svc._registry = registry
    svc.get_aligned_prices = AsyncMock(return_value=(common_dates, aligned_series))

    # Level (iv/greeks) legs still go through the display materialiser.
    async def fake_materialise(
        refs_with_labels, *, svc, start_date, end_date, progress_callback=None
    ):
        label = refs_with_labels[0][0]
        ref = refs_with_labels[0][1]
        values = (
            [5.0, 5.1, 5.2, 5.3, 5.4]
            if ref.stream == "mid"
            else [0.20, 0.21, 0.22, 0.23, 0.24]
        )
        d = np.array(DATES, dtype=np.int64)
        v = np.array(values, dtype=np.float64)
        diagnostics: list[str | None] = [None] * len(values)
        contracts: list = [None] * len(values)
        return {label: (d, v, diagnostics, contracts)}

    monkeypatch.setattr(
        "tcg.core.api.portfolio.materialise_option_streams",
        fake_materialise,
    )
    # Premium hold legs resolve through make_signal_fetcher (the hold path).
    monkeypatch.setattr(
        "tcg.core.api.portfolio.make_signal_fetcher",
        _make_hold_fetcher(),
    )

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


# ── Tests ──────────────────────────────────────────────────────────────


class TestPortfolioOptionStream:
    async def test_hold_mid_leg_in_equity_curve(self, client):
        """A hold-mode mid (premium) option leg participates in the portfolio
        equity curve (via the fixed-contract $-P&L hold path)."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": HOLD_MID_LEG,
            },
            "weights": {"SPX": 60, "OPT_MID": 40},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "portfolio_equity" in data
        assert len(data["portfolio_equity"]) > 0
        assert "OPT_MID" in data["leg_equities"]
        assert "metrics" in data

    async def test_mid_leg_without_hold_rejected(self, client):
        """A mid (premium) option leg WITHOUT hold-mode is rejected: a rolled
        option's daily-reselect %-return is not a valid equity series."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": OPT_MID_LEG,  # mid, hold_between_rolls defaults False
            },
            "weights": {"SPX": 60, "OPT_MID": 40},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400, resp.text
        body_json = resp.json()
        assert body_json["error_type"] == "validation_error"
        msg = body_json["message"].lower()
        assert "hold" in msg
        assert "mid" in msg or "price" in msg

    @pytest.mark.parametrize("stream", ["mid", "bs_mid"])
    async def test_premium_leg_without_hold_rejected_both_streams(self, client, stream):
        """Both premium streams (mid, bs_mid) are rejected without hold-mode."""
        body = {
            "legs": {"OPT": {**OPT_MID_LEG, "stream": stream}},
            "weights": {"OPT": 100},
            "rebalance": "none",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error_type"] == "validation_error"

    async def test_iv_leg_in_tracking_series_hold_off(self, client):
        """A level (iv) option leg goes to tracking_series and STILL works with
        hold off — level streams are display-only overlays, exempt from the
        hold-mode requirement."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_IV": OPT_IV_LEG,  # iv, hold off
            },
            "weights": {"SPX": 100, "OPT_IV": 100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "tracking_series" in data
        assert "OPT_IV" in data["tracking_series"]
        ts = data["tracking_series"]["OPT_IV"]
        assert ts["stream"] == "iv"
        assert ts["stream_mode"] == "level"
        assert "metrics" in ts
        metrics = ts["metrics"]
        assert metrics["mean"] is not None
        assert metrics["first"] is not None
        assert metrics["last"] is not None
        assert metrics["change"] is not None
        # IV leg should NOT be in equity curve
        assert "OPT_IV" not in data["leg_equities"]

    @pytest.mark.parametrize("stream", ["iv", "delta", "gamma", "vega", "theta"])
    async def test_level_streams_accept_hold_off(self, client, stream):
        """Every level stream (iv/greeks) validates and resolves with hold off."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT": {**OPT_MID_LEG, "stream": stream},
            },
            "weights": {"SPX": 100, "OPT": 100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        assert stream == resp.json()["tracking_series"]["OPT"]["stream"]

    async def test_mixed_hold_price_and_level_legs(self, client):
        """Hold mid leg in equity curve, iv leg in tracking_series, spot in
        equity curve."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": HOLD_MID_LEG,
                "OPT_IV": OPT_IV_LEG,
            },
            "weights": {"SPX": 50, "OPT_MID": 30, "OPT_IV": 20},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Price legs in equity curve
        assert "SPX" in data["leg_equities"]
        assert "OPT_MID" in data["leg_equities"]
        # Level leg in tracking_series
        assert "OPT_IV" in data["tracking_series"]
        assert "OPT_IV" not in data["leg_equities"]

    async def test_multi_leg_direction_applied_once_numeric(self, client):
        """A short hold-option price leg (weight -100) blended with a SIGNED price
        co-leg (a SHORT instrument leg, weight -50): the hold leg must enter with
        |weight| (direction already baked into its synthetic) while the co-leg keeps
        its SIGNED weight.  Asserts ``portfolio_equity`` NUMERICALLY equals the
        |weight|-normalized blend — and that an abs-ALL variant (co-leg wrongly
        abs'd) and an abs-NONE variant (hold leg double-shorted) both DIFFER.  The
        negative co-leg weight is what makes an abs-all regression visible (the
        status-200-only multi-leg tests all use positive co-leg weights)."""
        w_opt, w_spx = -100, -50
        body = {
            "legs": {"SPX": SPX_LEG, "OPT_MID": HOLD_MID_LEG},
            "weights": {"SPX": w_spx, "OPT_MID": w_opt},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        equity = np.array(resp.json()["portfolio_equity"], dtype=np.float64)

        # Independent oracle: the hold synthetic (direction baked in via the weight
        # SIGN) over the single-segment HOLD_PREMIUM fixture, blended with the SPX
        # closes.  ``owner_prev/cur`` are the step-owner mids of HOLD_PREMIUM.
        owner_prev = np.array([np.nan, 5.0, 5.1, 5.2, 5.3])
        owner_cur = np.array([np.nan, 5.1, 5.2, 5.3, 5.4])
        hold_synth = 100.0 * oracle_ratio(
            owner_prev,
            owner_cur,
            np.array(HOLD_IS_ROLL, dtype=np.float64),
            np.array(HOLD_ROLL_PREMIUM, dtype=np.float64),
            nav_times=1.0,
            weight=float(w_opt),
        )
        closes = {
            "OPT_MID": hold_synth,
            "SPX": np.array(SPX_CLOSES, dtype=np.float64),
        }
        dates = np.array(DATES, dtype=np.int64)

        def _blend(weights: dict[str, float]) -> np.ndarray:
            return compute_weighted_portfolio(
                closes, weights, "none", "normal", dates
            ).portfolio_equity

        # Correct wiring: hold leg gets |weight|, the co-leg keeps its signed weight.
        expected = _blend({"OPT_MID": abs(float(w_opt)), "SPX": float(w_spx)})
        np.testing.assert_allclose(equity, expected, rtol=1e-9, atol=1e-9)

        # Regression guards: neither an abs-ALL nor an abs-NONE blend equals it.
        abs_all = _blend({"OPT_MID": abs(float(w_opt)), "SPX": abs(float(w_spx))})
        abs_none = _blend({"OPT_MID": float(w_opt), "SPX": float(w_spx)})
        assert not np.allclose(equity, abs_all)
        assert not np.allclose(equity, abs_none)

    async def test_hold_mid_all_nan_rejected(self, client, monkeypatch):
        """A hold-mode mid leg whose premium resolves all-NaN fails loudly at the
        HTTP layer (the hold path's all-NaN guard)."""
        monkeypatch.setattr(
            "tcg.core.api.portfolio.make_signal_fetcher",
            _make_hold_fetcher(premium=[np.nan] * len(DATES)),
        )
        body = {
            "legs": {"OPT_MID": HOLD_MID_LEG},
            "weights": {"OPT_MID": 100},
            "rebalance": "none",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400, resp.text
        assert "all option stream values are NaN" in resp.json()["message"]

    async def test_level_only_rejected(self, client):
        """Portfolio with only level (non-price) legs is rejected (no equity)."""
        body = {
            "legs": {"OPT_IV": OPT_IV_LEG},
            "weights": {"OPT_IV": 100},
            "rebalance": "none",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code in (400, 422), resp.text

    async def test_level_metrics_structure(self, client):
        """Tracking series level metrics have the expected fields."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_IV": OPT_IV_LEG,
            },
            "weights": {"SPX": 100, "OPT_IV": 100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["tracking_series"]["OPT_IV"]["metrics"]
        expected_keys = {"mean", "std", "min", "max", "first", "last", "change"}
        assert expected_keys <= set(metrics.keys())


class TestPortfolioOptionStreamDisjointDates:
    """When an option leg's dates don't overlap the other legs', the
    'No overlapping dates' 400 must name option legs as a common cause
    (Issue #2 seam 1c).  Exercised via a hold-mode mid leg (the valid way an
    option leg enters the equity curve), whose resolved dates are disjoint."""

    async def test_disjoint_option_dates_message_mentions_options(
        self, client, monkeypatch
    ):
        # Hold leg resolves on dates far from the instrument's 2024 DATES.
        disjoint = [20200102, 20200103, 20200106]
        monkeypatch.setattr(
            "tcg.core.api.portfolio.make_signal_fetcher",
            _make_hold_fetcher(
                dates=disjoint,
                premium=[5.0, 5.1, 5.2],
                is_roll=[1.0, 0.0, 0.0],
                roll_premium=[5.0, np.nan, np.nan],
            ),
        )
        body = {
            "legs": {"SPX": SPX_LEG, "OPT_MID": HOLD_MID_LEG},
            "weights": {"SPX": 50, "OPT_MID": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2019-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 400, resp.text
        msg = resp.json()["message"].lower()
        assert "no overlapping dates" in msg
        assert "option" in msg


# ── roll_offset threading + adjustment-removal (the MAJOR review finding) ─


class TestPortfolioOptionStreamRollFields:
    """A portfolio option leg threads ``roll_offset`` into the
    ``OptionStreamRef`` it builds (mirroring the continuous-leg precedent).
    Unlike futures, option streams carry NO back-adjustment: a stray ``adjustment``
    leg key is ignored (never reaches the ref, never changes the series).

    The ref is built in ``_evaluate_option_stream_leg`` BEFORE the hold/display
    branch, so the threading is identical for every stream.  These tests exercise
    it through a LEVEL (iv) leg (the display path that captures the ref); the leg
    lands in ``tracking_series`` while SPX carries the equity curve.
    """

    # Level leg so the display materialiser (which captures the ref) runs.
    OPT_LEVEL_LEG = {**OPT_MID_LEG, "stream": "iv"}

    @pytest.fixture
    def capture_app(self, mock_app, monkeypatch):
        """Patch materialise to (a) record the ref it received and
        (b) return a series whose level encodes ``roll_offset`` — so a
        tracking-series difference proves that field reached the resolver, not
        just the constructor.  The ref has no ``adjustment`` attribute."""
        captured: dict = {}

        async def recording_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label, ref = refs_with_labels[0]
            captured["ref"] = ref
            # Base 5.0; the roll-offset VALUE nudges the level (proves it is
            # carried).  ``ref.roll_offset`` is the unified RollOffset({value,
            # unit}); read ``.value``.  Option streams have no adjustment, so the
            # level never depends on it.
            base = 5.0 + 0.1 * ref.roll_offset.value
            v = np.array([base + 0.1 * i for i in range(len(DATES))], dtype=np.float64)
            d = np.array(DATES, dtype=np.int64)
            return {label: (d, v, [None] * len(DATES), [None] * len(DATES))}

        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            recording_materialise,
        )
        return mock_app, captured

    @pytest.fixture
    async def capture_client(self, capture_app):
        app, captured = capture_app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, captured

    async def _tracking_values(self, client, leg):
        """Post {SPX + option leg} and return the option leg's resolved
        tracking-series values (SPX supplies the equity curve)."""
        body = {
            "legs": {"SPX": SPX_LEG, "OPT": leg},
            "weights": {"SPX": 50, "OPT": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()["tracking_series"]["OPT"]["values"]

    async def test_adjustment_not_threaded_into_ref(self, capture_client):
        """A stray ``adjustment`` leg key is ignored: the built OptionStreamRef
        has no ``adjustment`` attribute (option streams carry no
        back-adjustment)."""
        from tcg.core.api._models_options import RollOffset

        client, captured = capture_client
        leg = {**self.OPT_LEVEL_LEG, "adjustment": "ratio"}
        await self._tracking_values(client, leg)
        assert not hasattr(captured["ref"], "adjustment")
        assert captured["ref"].roll_offset == RollOffset()

    async def test_roll_offset_days_threaded_into_ref(self, capture_client):
        from tcg.core.api._models_options import RollOffset

        client, captured = capture_client
        leg = {**self.OPT_LEVEL_LEG, "roll_offset": {"value": 5, "unit": "days"}}
        await self._tracking_values(client, leg)
        assert captured["ref"].roll_offset == RollOffset(value=5, unit="days")
        assert not hasattr(captured["ref"], "adjustment")

    async def test_roll_offset_months_threaded_into_ref(self, capture_client):
        from tcg.core.api._models_options import RollOffset

        client, captured = capture_client
        leg = {**self.OPT_LEVEL_LEG, "roll_offset": {"value": 2, "unit": "months"}}
        await self._tracking_values(client, leg)
        assert captured["ref"].roll_offset == RollOffset(value=2, unit="months")

    async def test_legacy_int_roll_offset_reads_as_days(self, capture_client):
        """BACK-COMPAT: a persisted bare int leg roll_offset reads as days."""
        from tcg.core.api._models_options import RollOffset

        client, captured = capture_client
        leg = {**self.OPT_LEVEL_LEG, "roll_offset": 7}
        await self._tracking_values(client, leg)
        assert captured["ref"].roll_offset == RollOffset(value=7, unit="days")

    async def test_defaults_when_absent(self, capture_client):
        from tcg.core.api._models_options import RollOffset

        client, captured = capture_client
        await self._tracking_values(client, self.OPT_LEVEL_LEG)
        assert not hasattr(captured["ref"], "adjustment")
        assert captured["ref"].roll_offset == RollOffset()

    async def test_stray_adjustment_does_not_change_series(self, capture_client):
        """A stray ``adjustment`` leg key must NOT change the resolved series —
        option streams are raw stitched values, so the field is inert."""
        client, _captured = capture_client
        v_default = await self._tracking_values(client, self.OPT_LEVEL_LEG)
        v_stray = await self._tracking_values(
            client, {**self.OPT_LEVEL_LEG, "adjustment": "ratio"}
        )
        assert v_default == v_stray

    async def test_roll_offset_series_differs_from_default(self, capture_client):
        client, _captured = capture_client
        v_default = await self._tracking_values(client, self.OPT_LEVEL_LEG)
        v_rolled = await self._tracking_values(
            client, {**self.OPT_LEVEL_LEG, "roll_offset": {"value": 7, "unit": "days"}}
        )
        assert v_default != v_rolled

    async def test_roll_offset_days_out_of_range_rejected(self, capture_client):
        client, _captured = capture_client
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT": {
                    **self.OPT_LEVEL_LEG,
                    "roll_offset": {"value": 366, "unit": "days"},
                },
            },
            "weights": {"SPX": 50, "OPT": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code in (400, 422), resp.text

    async def test_roll_offset_months_out_of_range_rejected(self, capture_client):
        client, _captured = capture_client
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT": {
                    **self.OPT_LEVEL_LEG,
                    "roll_offset": {"value": 13, "unit": "months"},
                },
            },
            "weights": {"SPX": 50, "OPT": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code in (400, 422), resp.text

    async def test_stray_adjustment_ignored_not_rejected(self, capture_client):
        """An option leg carrying any ``adjustment`` value (even a bogus one) is
        accepted and ignored — adjustment is not part of the option path."""
        client, _captured = capture_client
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT": {**self.OPT_LEVEL_LEG, "adjustment": "bogus"},
            },
            "weights": {"SPX": 50, "OPT": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text

    # ── "Roll at end of month" is the EndOfMonth maturity, not a schedule ──

    async def test_end_of_month_maturity_threaded_into_ref(self, capture_client):
        """A portfolio option leg with the EndOfMonth maturity threads it into
        the OptionStreamRef (the monthly-hold roll trigger); there is no
        ``roll_schedule`` attribute on the ref any more."""
        client, captured = capture_client
        leg = {
            **self.OPT_LEVEL_LEG,
            "maturity": {"kind": "end_of_month", "offset_months": 1},
        }
        await self._tracking_values(client, leg)
        assert captured["ref"].maturity.kind == "end_of_month"
        assert captured["ref"].maturity.offset_months == 1
        assert not hasattr(captured["ref"], "roll_schedule")
