"""Tests for POST /api/portfolio/compute with option_stream legs.

Covers:
- Mid-stream leg (price-like) participates in equity curve
- Level-stream leg (iv/greeks) goes to tracking_series
- Mixed price + level legs
- All-NaN rejection
- NaN forward-fill
- Level-only portfolio rejected
- Date requirement for option_stream legs
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
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries


# ── Helpers ────────────────────────────────────────────────────────────

DATES = [20240102, 20240103, 20240104, 20240105, 20240108]
SPX_CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0]


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


def _fake_materialise_result(values, dates=None):
    """Build a synthetic materialise result for a single leg."""
    d = np.array(dates or DATES, dtype=np.int64)
    v = np.array(values, dtype=np.float64)
    diagnostics: list[str | None] = [None] * len(values)
    return {"_leg": (d, v, diagnostics)}


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
    """FastAPI app with mocked data service and option materialisation."""
    registry = CollectionRegistry(["INDEX", "OPT_SP_500"])

    common_dates = np.array(DATES, dtype=np.int64)
    aligned_series = {
        "SPX": _price_series(DATES, SPX_CLOSES),
    }

    svc = MagicMock()
    svc._registry = registry
    svc.get_aligned_prices = AsyncMock(return_value=(common_dates, aligned_series))

    # Default materialise mock — returns mid price data
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
    async def test_mid_leg_in_equity_curve(self, client):
        """Mid-stream option leg participates in the portfolio equity curve."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": OPT_MID_LEG,
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

    async def test_iv_leg_in_tracking_series(self, client):
        """IV-stream option leg goes to tracking_series, not equity curve."""
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

    async def test_mixed_price_and_level_legs(self, client):
        """Mid leg in equity curve, iv leg in tracking_series, spot in equity curve."""
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": OPT_MID_LEG,
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

    async def test_all_nan_rejected(self, client, monkeypatch):
        """All-NaN option stream values raises validation error."""

        async def nan_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label = refs_with_labels[0][0]
            d = np.array(DATES, dtype=np.int64)
            v = np.full(len(DATES), np.nan, dtype=np.float64)
            return {label: (d, v, [None] * len(DATES), [None] * len(DATES))}

        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            nan_materialise,
        )
        body = {
            "legs": {"OPT_MID": OPT_MID_LEG},
            "weights": {"OPT_MID": 100},
            "rebalance": "none",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code in (400, 422), resp.text

    async def test_nan_forward_fill(self, client, monkeypatch):
        """NaN gaps in mid-stream values are forward-filled."""

        async def gap_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label = refs_with_labels[0][0]
            d = np.array(DATES, dtype=np.int64)
            v = np.array([5.0, np.nan, np.nan, 5.3, 5.4], dtype=np.float64)
            return {label: (d, v, [None] * len(DATES), [None] * len(DATES))}

        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            gap_materialise,
        )
        body = {
            "legs": {
                "SPX": SPX_LEG,
                "OPT_MID": OPT_MID_LEG,
            },
            "weights": {"SPX": 50, "OPT_MID": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Equity curve should have no NaN (forward-fill applied)
        equity = data["portfolio_equity"]
        assert all(
            v is not None and not (isinstance(v, float) and np.isnan(v)) for v in equity
        )

    async def test_level_only_rejected(self, client):
        """Portfolio with only level (non-price) legs is rejected."""
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


# ── Actionable all-NaN diagnostics (Issue #2 seam 1b) ───────────────────


class TestPortfolioOptionStreamAllNanDiagnostics:
    """When a price-like option leg is all-NaN, the 400 must fold in the
    DISCARDED per-date diagnostics (the ``error_codes`` list) so the user
    learns WHY — the dominant cause + an actionable hint — instead of a blunt
    'all option stream values are NaN'."""

    def _nan_with_diags(self, diags):
        async def _materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label = refs_with_labels[0][0]
            d = np.array(DATES, dtype=np.int64)
            v = np.full(len(DATES), np.nan, dtype=np.float64)
            return {label: (d, v, list(diags), [None] * len(DATES))}

        return _materialise

    async def _post_optmid_only(self, client):
        body = {
            "legs": {"OPT_MID": OPT_MID_LEG},
            "weights": {"OPT_MID": 100},
            "rebalance": "none",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        return await client.post("/api/portfolio/compute", json=body)

    async def test_all_nan_message_names_dominant_missing_delta(
        self, client, monkeypatch
    ):
        """ByDelta over a no-stored-greeks range: every date is
        ``missing_delta_no_compute`` -> the 400 names that cause and steers the
        user to By Moneyness / By Strike or a range with greeks."""
        diags = ["missing_delta_no_compute"] * len(DATES)
        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            self._nan_with_diags(diags),
        )
        resp = await self._post_optmid_only(client)
        assert resp.status_code == 400, resp.text
        msg = resp.json()["message"]
        # Keeps the original phrase as a prefix...
        assert "all option stream values are NaN" in msg
        # ...and adds the dominant cause + an actionable hint.
        assert "missing_delta_no_compute" in msg
        assert "greeks" in msg.lower()
        assert ("moneyness" in msg.lower()) or ("strike" in msg.lower())

    async def test_all_nan_message_names_dominant_missing_mid(
        self, client, monkeypatch
    ):
        """Sparse quotes: every date is ``missing_mid`` -> the 400 names that
        cause and mentions bid/ask quotes."""
        diags = ["missing_mid"] * len(DATES)
        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            self._nan_with_diags(diags),
        )
        resp = await self._post_optmid_only(client)
        assert resp.status_code == 400, resp.text
        msg = resp.json()["message"]
        assert "all option stream values are NaN" in msg
        assert "missing_mid" in msg
        assert ("bid" in msg.lower()) or ("quote" in msg.lower())

    async def test_all_nan_message_handles_no_chain(self, client, monkeypatch):
        """Maturity rule whose (post-snap) expiration still isn't listed:
        ``no_chain_for_date`` -> the 400 names that cause and mentions the
        expiration not being listed for the root."""
        diags = ["no_chain_for_date"] * len(DATES)
        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            self._nan_with_diags(diags),
        )
        resp = await self._post_optmid_only(client)
        assert resp.status_code == 400, resp.text
        msg = resp.json()["message"]
        assert "no_chain_for_date" in msg
        assert ("expiration" in msg.lower()) or ("listed" in msg.lower())

    async def test_all_nan_message_robust_when_no_diagnostics(
        self, client, monkeypatch
    ):
        """Defensive: an all-None diagnostics list (no per-date code) still
        produces the base message without crashing."""
        diags = [None] * len(DATES)
        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            self._nan_with_diags(diags),
        )
        resp = await self._post_optmid_only(client)
        assert resp.status_code == 400, resp.text
        assert "all option stream values are NaN" in resp.json()["message"]


class TestPortfolioOptionStreamDisjointDates:
    """When an option leg's dates don't overlap the other legs', the
    'No overlapping dates' 400 must name option legs as a common cause
    (Issue #2 seam 1c) — the old message mentioned only instrument/signal."""

    async def test_disjoint_option_dates_message_mentions_options(
        self, client, monkeypatch
    ):
        # Option leg materialises on dates far from the instrument's DATES.
        disjoint = [20200102, 20200103, 20200106]

        async def disjoint_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label = refs_with_labels[0][0]
            d = np.array(disjoint, dtype=np.int64)
            v = np.array([5.0, 5.1, 5.2], dtype=np.float64)
            return {label: (d, v, [None] * 3, [None] * 3)}

        monkeypatch.setattr(
            "tcg.core.api.portfolio.materialise_option_streams",
            disjoint_materialise,
        )
        body = {
            "legs": {"SPX": SPX_LEG, "OPT_MID": OPT_MID_LEG},
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
    leg key is ignored (never reaches the ref, never changes the curve).
    """

    @pytest.fixture
    def capture_app(self, mock_app, monkeypatch):
        """Patch materialise to (a) record the ref it received and
        (b) return a series whose level encodes ``roll_offset`` — so an
        equity-curve difference proves that field reached the resolver, not
        just the constructor.  The ref has no ``adjustment`` attribute."""
        captured: dict = {}

        async def recording_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            label, ref = refs_with_labels[0]
            captured["ref"] = ref
            # Base 5.0; roll_offset nudges the level (proves it is carried).
            # Option streams have no adjustment, so the level never depends on it.
            base = 5.0 + 0.1 * ref.roll_offset
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

    async def _equity(self, client, leg):
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
        return resp.json()["portfolio_equity"]

    async def test_adjustment_not_threaded_into_ref(self, capture_client):
        """A stray ``adjustment`` leg key is ignored: the built OptionStreamRef
        has no ``adjustment`` attribute (option streams carry no
        back-adjustment)."""
        client, captured = capture_client
        leg = {**OPT_MID_LEG, "adjustment": "ratio"}
        await self._equity(client, leg)
        assert not hasattr(captured["ref"], "adjustment")
        assert captured["ref"].roll_offset == 0

    async def test_roll_offset_threaded_into_ref(self, capture_client):
        client, captured = capture_client
        leg = {**OPT_MID_LEG, "roll_offset": 5}
        await self._equity(client, leg)
        assert captured["ref"].roll_offset == 5
        assert not hasattr(captured["ref"], "adjustment")

    async def test_defaults_when_absent(self, capture_client):
        client, captured = capture_client
        await self._equity(client, OPT_MID_LEG)
        assert not hasattr(captured["ref"], "adjustment")
        assert captured["ref"].roll_offset == 0

    async def test_stray_adjustment_does_not_change_series(self, capture_client):
        """A stray ``adjustment`` leg key must NOT change the equity curve —
        option streams are raw stitched mids, so the field is inert."""
        client, _captured = capture_client
        eq_default = await self._equity(client, OPT_MID_LEG)
        eq_stray = await self._equity(client, {**OPT_MID_LEG, "adjustment": "ratio"})
        assert eq_default == eq_stray

    async def test_roll_offset_series_differs_from_default(self, capture_client):
        client, _captured = capture_client
        eq_default = await self._equity(client, OPT_MID_LEG)
        eq_rolled = await self._equity(client, {**OPT_MID_LEG, "roll_offset": 7})
        assert eq_default != eq_rolled

    async def test_roll_offset_out_of_range_rejected(self, capture_client):
        client, _captured = capture_client
        body = {
            "legs": {"SPX": SPX_LEG, "OPT": {**OPT_MID_LEG, "roll_offset": 31}},
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
                "OPT": {**OPT_MID_LEG, "adjustment": "bogus"},
            },
            "weights": {"SPX": 50, "OPT": 50},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        resp = await client.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
