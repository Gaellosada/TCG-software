"""Tests for ``POST /api/options/stream`` endpoint.

Covers:
- Single stream materialisation
- Multiple streams with keyed results
- Tautology validation (by_delta + stream='delta')
- Greeks-gated validation (gamma/vega/theta on a no-greeks root)
- Empty / missing date range
- Progress tracking
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.options import router as options_router
from tcg.types.errors import TCGError
from tcg.types.options import OptionRootInfo


# ── Fixtures ───────────────────────────────────────────────────────────

_ROOT_WITH_GREEKS = OptionRootInfo(
    collection="OPT_SP_500",
    name="SP 500",
    has_greeks=True,
    providers=("IVOLATILITY",),
    expiration_first=date(2005, 1, 21),
    expiration_last=date(2027, 12, 19),
    doc_count_estimated=1234567,
    strike_factor_verified=True,
    last_trade_date=None,
)

_ROOT_NO_GREEKS = OptionRootInfo(
    # OPT_ETH is the canonical no-greeks root at the data layer
    # (OPT_VIX was unblocked in Phase 1 of the VIX greeks rollout).
    collection="OPT_ETH",
    name="ETH",
    has_greeks=False,
    providers=("DERIBIT",),
    expiration_first=date(2020, 1, 1),
    expiration_last=date(2027, 12, 19),
    doc_count_estimated=500000,
    strike_factor_verified=True,
    last_trade_date=None,
)


def _fake_materialise_result(
    labels: list[str],
) -> dict[str, tuple[np.ndarray, np.ndarray, list[str | None], list]]:
    """Build a synthetic materialise_option_streams result for N labels.

    Returns the 4-tuple per CONTRACT (dates, values, diagnostics,
    contracts).  Contracts list is all-None so derived ``rolls`` is
    empty — tests that need real roll behaviour live in
    ``test_option_stream_rolls.py``.
    """
    dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
    result = {}
    for i, label in enumerate(labels):
        base = 0.20 + i * 0.10
        values = np.array([base, base + 0.01, base + 0.02], dtype=np.float64)
        diagnostics: list[str | None] = [None, None, None]
        contracts: list = [None, None, None]
        result[label] = (dates, values, diagnostics, contracts)
    return result


@pytest.fixture
def mock_app(monkeypatch):
    """FastAPI app wired for options with a stubbed materialiser."""
    svc = MagicMock()
    svc.list_option_roots = AsyncMock(return_value=[_ROOT_WITH_GREEKS, _ROOT_NO_GREEKS])

    # Patch materialise_option_streams to bypass the chain-data layer.
    async def fake_materialise(
        refs_with_labels, *, svc, start_date, end_date, progress_callback=None
    ):
        labels = [label for label, _ref in refs_with_labels]
        return _fake_materialise_result(labels)

    monkeypatch.setattr(
        "tcg.core.api._options_materialise.materialise_option_streams",
        fake_materialise,
    )

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(options_router)
    app.state.market_data = svc
    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Helpers ────────────────────────────────────────────────────────────


def _stream_entry(
    *,
    label: str = "iv_stream",
    collection: str = "OPT_SP_500",
    option_type: str = "C",
    stream: str = "iv",
    selection: dict | None = None,
    maturity: dict | None = None,
) -> dict:
    """Build a single stream entry for the request body."""
    return {
        "ref": {
            "type": "option_stream",
            "collection": collection,
            "option_type": option_type,
            "cycle": None,
            "maturity": maturity
            or {
                "kind": "next_third_friday",
                "offset_months": 0,
            },
            "selection": selection
            or {
                "kind": "by_moneyness",
                "target": 1.0,
                "tolerance": 0.05,
            },
            "stream": stream,
        },
        "label": label,
    }


def _request_body(
    streams: list[dict],
    *,
    start: str = "2024-01-02",
    end: str = "2024-01-04",
    task_id: str | None = None,
) -> dict:
    body: dict = {"streams": streams, "start": start, "end": end}
    if task_id is not None:
        body["task_id"] = task_id
    return body


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSingleStream:
    """Single-stream happy path."""

    async def test_single_stream_returns_200(self, client: AsyncClient):
        body = _request_body([_stream_entry()])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 200, resp.text

    async def test_single_stream_response_shape(self, client: AsyncClient):
        body = _request_body([_stream_entry(label="my_iv")])
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        assert "dates" in data
        assert "streams" in data
        assert "my_iv" in data["streams"]
        stream = data["streams"]["my_iv"]
        assert "values" in stream
        assert "diagnostics" in stream
        assert len(data["dates"]) == len(stream["values"])

    async def test_dates_are_iso_strings(self, client: AsyncClient):
        body = _request_body([_stream_entry()])
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        for d in data["dates"]:
            # Should be parseable as YYYY-MM-DD
            date.fromisoformat(d)


@pytest.mark.asyncio
class TestMultipleStreams:
    """Multiple streams with keyed results."""

    async def test_two_streams_returns_keyed_results(self, client: AsyncClient):
        streams = [
            _stream_entry(label="iv_call", stream="iv"),
            _stream_entry(label="delta_call", stream="delta"),
        ]
        body = _request_body(streams)
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "iv_call" in data["streams"]
        assert "delta_call" in data["streams"]

    async def test_three_streams_all_present(self, client: AsyncClient):
        streams = [
            _stream_entry(label="a", stream="iv"),
            _stream_entry(label="b", stream="mid"),
            _stream_entry(label="c", stream="volume"),
        ]
        body = _request_body(streams)
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        assert set(data["streams"].keys()) == {"a", "b", "c"}


@pytest.mark.asyncio
class TestValidationErrors:
    """Pre-flight validation rejects bad requests."""

    async def test_empty_streams_returns_400(self, client: AsyncClient):
        body = _request_body([])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"

    async def test_invalid_date_format_returns_400(self, client: AsyncClient):
        body = _request_body([_stream_entry()], start="not-a-date", end="2024-01-04")
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_type"] == "validation"
        assert "date" in data["message"].lower()

    async def test_tautology_by_delta_plus_delta_stream(self, client: AsyncClient):
        stream = _stream_entry(
            label="taut",
            stream="delta",
            selection={
                "kind": "by_delta",
                "target": 0.25,
                "tolerance": 0.05,
                "strict": False,
            },
        )
        body = _request_body([stream])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["error_code"] == "TAUTOLOGICAL_OPTION_STREAM"

    async def test_greeks_gated_on_no_greeks_root(self, client: AsyncClient):
        stream = _stream_entry(
            label="gamma_eth",
            collection="OPT_ETH",
            stream="gamma",
        )
        body = _request_body([stream])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["error_code"] == "STREAM_UNAVAILABLE_FOR_ROOT"
        assert data["root"] == "OPT_ETH"

    async def test_greeks_gated_theta_on_no_greeks_root(self, client: AsyncClient):
        stream = _stream_entry(
            label="theta_eth",
            collection="OPT_ETH",
            stream="theta",
        )
        body = _request_body([stream])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["error_code"] == "STREAM_UNAVAILABLE_FOR_ROOT"

    async def test_iv_on_no_greeks_root_still_allowed(self, client: AsyncClient):
        """iv and delta are available on all roots — only gamma/vega/theta
        are gated."""
        stream = _stream_entry(
            label="iv_eth",
            collection="OPT_ETH",
            stream="iv",
        )
        body = _request_body([stream])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestEmptyDateRange:
    """Date range validation."""

    async def test_materialise_error_propagated(self, client: AsyncClient, monkeypatch):
        """When materialise_option_streams returns a string error, the
        endpoint surfaces it as a 400."""

        async def fail_materialise(
            refs_with_labels, *, svc, start_date, end_date, progress_callback=None
        ):
            return "option_stream requires explicit ISO 'start' and 'end' dates"

        monkeypatch.setattr(
            "tcg.core.api._options_materialise.materialise_option_streams",
            fail_materialise,
        )
        body = _request_body([_stream_entry()])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 400
        data = resp.json()
        assert "option_stream" in data["message"]


@pytest.mark.asyncio
class TestProgressTracking:
    """Progress tracking on the stream endpoint."""

    async def test_unknown_task_returns_zeros(self, client: AsyncClient):
        resp = await client.get("/api/options/stream/progress/no-such-task")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"done": 0, "total": 0, "fraction": 0.0}

    async def test_register_and_tick(self, client: AsyncClient):
        from tcg.core.api.options import (
            _stream_progress_clear,
            _stream_progress_register,
            _stream_progress_tick,
        )

        _stream_progress_register("stream-task-1", total=5)
        try:
            _stream_progress_tick("stream-task-1")
            _stream_progress_tick("stream-task-1")
            resp = await client.get("/api/options/stream/progress/stream-task-1")
            data = resp.json()
            assert data["done"] == 2
            assert data["total"] == 5
            assert data["fraction"] == pytest.approx(0.4)
        finally:
            _stream_progress_clear("stream-task-1")

    async def test_with_task_id_compute_cleans_up(self, client: AsyncClient):
        """task_id supplied → progress entry is registered and then
        cleaned up after the response."""
        body = _request_body([_stream_entry()], task_id="stream-cleanup-test")
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 200
        # After response + background cleanup, entry should be gone.
        progress_resp = await client.get(
            "/api/options/stream/progress/stream-cleanup-test"
        )
        assert progress_resp.json() == {"done": 0, "total": 0, "fraction": 0.0}


@pytest.mark.asyncio
class TestFEAliasCompatibility:
    """The endpoint accepts FE-emitted alias shapes for selection/maturity."""

    async def test_by_moneyness_fe_alias(self, client: AsyncClient):
        stream = _stream_entry(
            selection={"kind": "by_moneyness", "target": 1.0, "tolerance": 0.05},
            maturity={"kind": "next_third_friday", "offset_months": 0},
        )
        body = _request_body([stream])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 200, resp.text

    async def test_nearest_to_target_fe_alias(self, client: AsyncClient):
        stream = _stream_entry(
            maturity={"kind": "nearest_to_target", "target_days": 30},
        )
        body = _request_body([stream])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 200, resp.text
