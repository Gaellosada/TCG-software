"""Tests for /api/signals/compute with option_stream inputs.

Covers:
- Happy path: signal with option_stream + spot inputs
- Option_stream-only signal
- Tautological by_delta+delta rejection
- Overlap pre-flight (cheap, no full materialisation)
- Instrument payload serialization for option_stream
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.signals import router as signals_router
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries


# ── Helpers ────────────────────────────────────────────────────────────

DATES = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108],
    dtype=np.int64,
)
CLOSES = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)


def _price_series() -> PriceSeries:
    n = DATES.shape[0]
    return PriceSeries(
        dates=DATES,
        open=CLOSES - 1.0,
        high=CLOSES + 1.0,
        low=CLOSES - 2.0,
        close=CLOSES,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


# Fake resolve_option_stream return: (values, diagnostics, contracts)
OPTION_VALUES = np.array([0.25, 0.26, 0.27, 0.28, 0.29], dtype=np.float64)
OPTION_DATES_PY = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
]
OPTION_DIAGNOSTICS: list[str | None] = [None, None, None, None, None]

# Available expirations for cheap pre-flight
AVAILABLE_EXPIRATIONS = [date(2024, 1, 19), date(2024, 2, 16), date(2024, 3, 15)]


SPX_INPUT = {
    "id": "X",
    "instrument": {
        "type": "spot",
        "collection": "INDEX",
        "instrument_id": "SPX",
    },
}

OPT_INPUT = {
    "id": "Y",
    "instrument": {
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
    },
}


def _simple_signal(inputs, input_id="X"):
    """Build a minimal valid signal spec with one entry block."""
    return {
        "spec": {
            "id": "sig1",
            "name": "Test Signal",
            "inputs": inputs,
            "rules": {
                "entries": [
                    {
                        "id": "E1",
                        "name": "Entry1",
                        "input_id": input_id,
                        "weight": 100.0,
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {
                                    "kind": "instrument",
                                    "input_id": input_id,
                                    "field": "close",
                                },
                                "rhs": {"kind": "constant", "value": 0},
                            }
                        ],
                    }
                ],
                "exits": [],
            },
        },
        "indicators": [],
    }


@pytest.fixture
def mock_app(monkeypatch):
    """FastAPI app with mocked data service and option stream resolution."""
    svc = MagicMock()
    svc.get_prices = AsyncMock(return_value=_price_series())
    svc.list_option_expirations_filtered = AsyncMock(return_value=AVAILABLE_EXPIRATIONS)

    # Mock the wiring factory to return stubs (accept the optional
    # underlying_prefetch_window kwarg the perf memo threads through).
    mock_wiring = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
    monkeypatch.setattr(
        "tcg.core.api._options_wiring.build_stream_resolver_wiring",
        lambda svc, **_kw: mock_wiring,
    )

    # Mock resolve_option_stream — 3-tuple (values, diagnostics, contracts).
    # contracts list is all-None: signals path doesn't consume rolls,
    # but the unpacking would crash on a 2-tuple after the engine change.
    async def fake_resolve(**kwargs):
        n = len(OPTION_VALUES)
        return (OPTION_VALUES.copy(), list(OPTION_DIAGNOSTICS), [None] * n)

    monkeypatch.setattr(
        "tcg.engine.options.series.stream_resolver.resolve_option_stream",
        fake_resolve,
    )

    # Mock _business_dates_in_range to return our test dates
    monkeypatch.setattr(
        "tcg.core.api._options_materialise._business_dates_in_range",
        lambda start, end: OPTION_DATES_PY if start and end else None,
    )

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(signals_router)
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


class TestSignalOptionStream:
    async def test_signal_with_option_stream_and_spot_input(self, client):
        """Signal with both option_stream and spot inputs computes successfully."""
        body = _simple_signal([SPX_INPUT, OPT_INPUT], input_id="X")
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "timestamps" in data
        assert len(data["timestamps"]) > 0
        assert len(data["positions"]) >= 1

    async def test_signal_option_stream_only(self, client):
        """Signal with only an option_stream input computes successfully."""
        body = _simple_signal([OPT_INPUT], input_id="Y")
        body["start"] = "2024-01-01"
        body["end"] = "2024-03-31"
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["timestamps"]) > 0
        # Verify the position references input Y
        pos = data["positions"][0]
        assert pos["input_id"] == "Y"

    async def test_tautological_by_delta_stream_delta_rejected(self, client):
        """by_delta selection with delta stream is rejected as tautological."""
        tautological_input = {
            "id": "Y",
            "instrument": {
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
                "stream": "delta",
            },
        }
        body = _simple_signal([tautological_input], input_id="Y")
        body["start"] = "2024-01-01"
        body["end"] = "2024-03-31"
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "tautolog" in data["message"].lower()

    async def test_instrument_payload_serialization(self, client):
        """option_stream instrument in the response includes all fields."""
        body = _simple_signal([OPT_INPUT], input_id="Y")
        body["start"] = "2024-01-01"
        body["end"] = "2024-03-31"
        resp = await client.post("/api/signals/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        pos = data["positions"][0]
        inst = pos["instrument"]
        assert inst["type"] == "option_stream"
        assert inst["collection"] == "OPT_SP_500"
        assert inst["option_type"] == "C"
        assert inst["stream"] == "mid"
        assert "maturity" in inst
        assert "selection" in inst
        # roll_offset emitted as the unified {value, unit} (default no-op when
        # absent).  Option streams carry NO back-adjustment, so no ``adjustment``
        # key is emitted, and "end of month" is the maturity, not a roll_schedule.
        assert "adjustment" not in inst
        assert "roll_schedule" not in inst
        assert inst["roll_offset"] == {"value": 0, "unit": "days"}


# ── roll_offset threading + adjustment-removal (the MAJOR review finding) ─


def _opt_input(adjustment=None, roll_offset=None, maturity=None):
    inst = {
        "type": "option_stream",
        "collection": "OPT_SP_500",
        "option_type": "C",
        "cycle": None,
        "maturity": maturity or {"kind": "next_third_friday", "offset_months": 0},
        "selection": {
            "kind": "by_delta",
            "target": 0.25,
            "tolerance": 0.1,
            "strict": False,
        },
        "stream": "mid",
    }
    # A stray ``adjustment`` key (e.g. a legacy persisted leg) is tolerated and
    # ignored — option streams carry no back-adjustment.  We still allow tests
    # to send it to prove it has no effect.
    if adjustment is not None:
        inst["adjustment"] = adjustment
    # ``roll_offset`` may be a bare int (legacy days) OR the unified
    # ``{"value", "unit"}`` object — the model accepts both.
    if roll_offset is not None:
        inst["roll_offset"] = roll_offset
    return {"id": "Y", "instrument": inst}


@pytest.fixture
def capture_app(monkeypatch):
    """Like ``mock_app`` but the resolver records the kwargs it received
    so the test can prove ``roll_offset`` was threaded all the way into
    ``resolve_option_stream`` and that ``adjustment`` is NOT passed (option
    streams carry no back-adjustment)."""
    captured: dict = {}

    svc = MagicMock()
    svc.get_prices = AsyncMock(return_value=_price_series())
    svc.list_option_expirations_filtered = AsyncMock(return_value=AVAILABLE_EXPIRATIONS)

    mock_wiring = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
    monkeypatch.setattr(
        "tcg.core.api._options_wiring.build_stream_resolver_wiring",
        lambda svc, **_kw: mock_wiring,
    )

    async def recording_resolve(**kwargs):
        captured.update(kwargs)
        n = len(OPTION_VALUES)
        # Option streams are raw stitched mids — no adjustment bump.
        return (OPTION_VALUES.copy(), list(OPTION_DIAGNOSTICS), [None] * n)

    monkeypatch.setattr(
        "tcg.engine.options.series.stream_resolver.resolve_option_stream",
        recording_resolve,
    )
    monkeypatch.setattr(
        "tcg.core.api._options_materialise._business_dates_in_range",
        lambda start, end: OPTION_DATES_PY if start and end else None,
    )

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(signals_router)
    app.state.market_data = svc
    app.state.app_db_repo = object()
    return app, captured


@pytest.fixture
async def capture_client(capture_app):
    app, captured = capture_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, captured


class TestSignalOptionStreamRollFields:
    async def _run(self, client, opt_input):
        body = _simple_signal([opt_input], input_id="Y")
        body["start"] = "2024-01-01"
        body["end"] = "2024-03-31"
        return await client.post("/api/signals/compute", json=body)

    async def test_adjustment_not_threaded_into_resolver(self, capture_client):
        """A stray ``adjustment`` key on an option leg is ignored: it is never
        passed to ``resolve_option_stream`` (option streams carry no
        back-adjustment)."""
        from tcg.types.options import RollOffset

        client, captured = capture_client
        resp = await self._run(client, _opt_input(adjustment="ratio"))
        assert resp.status_code == 200, resp.text
        assert "adjustment" not in captured
        assert captured["roll_offset"] == RollOffset()  # default no-op

    async def test_roll_offset_days_threaded_into_resolver(self, capture_client):
        """A ``{value, unit:'days'}`` roll offset reaches the resolver as the
        unified RollOffset dataclass."""
        from tcg.types.options import RollOffset

        client, captured = capture_client
        resp = await self._run(
            client, _opt_input(roll_offset={"value": 5, "unit": "days"})
        )
        assert resp.status_code == 200, resp.text
        assert captured["roll_offset"] == RollOffset(value=5, unit="days")
        assert "adjustment" not in captured

    async def test_roll_offset_months_threaded_into_resolver(self, capture_client):
        """A ``{value, unit:'months'}`` roll offset reaches the resolver intact."""
        from tcg.types.options import RollOffset

        client, captured = capture_client
        resp = await self._run(
            client, _opt_input(roll_offset={"value": 1, "unit": "months"})
        )
        assert resp.status_code == 200, resp.text
        assert captured["roll_offset"] == RollOffset(value=1, unit="months")

    async def test_legacy_int_roll_offset_reads_as_days(self, capture_client):
        """BACK-COMPAT: a shipped bare int (old days-only field) is coerced to
        ``{value:int, unit:'days'}`` and reaches the resolver as such."""
        from tcg.types.options import RollOffset

        client, captured = capture_client
        resp = await self._run(client, _opt_input(roll_offset=7))
        assert resp.status_code == 200, resp.text
        assert captured["roll_offset"] == RollOffset(value=7, unit="days")

    async def test_defaults_when_absent(self, capture_client):
        from tcg.types.options import RollOffset

        client, captured = capture_client
        resp = await self._run(client, _opt_input())
        assert resp.status_code == 200, resp.text
        assert "adjustment" not in captured
        assert captured["roll_offset"] == RollOffset()

    async def test_response_payload_round_trips_fields(self, capture_client):
        """The option_stream instrument echoed in the response carries the same
        roll_offset (as the {value, unit} object) and no ``adjustment`` key."""
        client, _captured = capture_client
        resp = await self._run(
            client,
            _opt_input(adjustment="ratio", roll_offset={"value": 3, "unit": "months"}),
        )
        assert resp.status_code == 200, resp.text
        inst = resp.json()["positions"][0]["instrument"]
        assert "adjustment" not in inst
        assert inst["roll_offset"] == {"value": 3, "unit": "months"}

    async def test_roll_offset_days_out_of_range_rejected(self, capture_client):
        client, _captured = capture_client
        resp = await self._run(
            client, _opt_input(roll_offset={"value": 31, "unit": "days"})
        )
        assert resp.status_code in (400, 422), resp.text

    async def test_roll_offset_months_out_of_range_rejected(self, capture_client):
        client, _captured = capture_client
        resp = await self._run(
            client, _opt_input(roll_offset={"value": 13, "unit": "months"})
        )
        assert resp.status_code in (400, 422), resp.text

    async def test_negative_roll_offset_rejected(self, capture_client):
        client, _captured = capture_client
        resp = await self._run(
            client, _opt_input(roll_offset={"value": -1, "unit": "days"})
        )
        assert resp.status_code in (400, 422), resp.text

    async def test_invalid_roll_offset_unit_rejected(self, capture_client):
        client, _captured = capture_client
        resp = await self._run(
            client, _opt_input(roll_offset={"value": 1, "unit": "weeks"})
        )
        assert resp.status_code in (400, 422), resp.text

    # ── "Roll at end of month" is the EndOfMonth maturity (not a schedule) ──

    async def test_end_of_month_maturity_threaded_into_resolver(self, capture_client):
        """Choosing the EndOfMonth maturity reaches the resolver as the
        EndOfMonth dataclass — that IS the monthly-hold roll trigger now."""
        from tcg.types.options import EndOfMonth

        client, captured = capture_client
        resp = await self._run(
            client,
            _opt_input(maturity={"kind": "end_of_month", "offset_months": 1}),
        )
        assert resp.status_code == 200, resp.text
        assert captured["maturity"] == EndOfMonth(offset_months=1)
        # No roll_schedule kwarg exists any more.
        assert "roll_schedule" not in captured
