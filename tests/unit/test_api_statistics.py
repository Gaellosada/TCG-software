"""Tests for POST /api/statistics."""

from __future__ import annotations

import math

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.engine.statistics import compute_statistics


@pytest.fixture
def app():
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.statistics import router as statistics_router
    from tcg.core.app import _request_validation_error_handler
    from tcg.types.errors import TCGError

    application = FastAPI()
    application.add_exception_handler(TCGError, tcg_error_handler)
    application.add_exception_handler(
        RequestValidationError, _request_validation_error_handler
    )
    application.include_router(statistics_router)
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Helpers ────────────────────────────────────────────────────────────


def _make_equity(n: int) -> tuple[list[int], list[float]]:
    dates: list[int] = []
    year, month, day = 2024, 1, 1
    for _ in range(n):
        dates.append(year * 10000 + month * 100 + day)
        day += 1
        if day > 28:
            day = 1
            month += 1
            if month > 12:
                month = 1
                year += 1
    equity = (100.0 * np.cumprod(1.0 + np.linspace(0.001, 0.002, n))).tolist()
    return dates, equity


# ── Happy path ─────────────────────────────────────────────────────────


async def test_happy_path_returns_full_shape(client):
    dates, equity = _make_equity(60)
    resp = await client.post(
        "/api/statistics",
        json={"dates": dates, "equity": equity, "risk_free_rate": 0.04},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    assert set(payload.keys()) == {
        "return",
        "risk_adjusted",
        "tail",
        "drawdown",
        "risk_free_rate_used",
        "num_observations",
    }
    assert set(payload["return"].keys()) == {
        "total_return",
        "cagr",
        "excess_return",
        "annualized_volatility",
        "best_day",
        "worst_day",
        "best_month",
        "worst_month",
    }
    assert set(payload["risk_adjusted"].keys()) == {
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
    }
    assert set(payload["tail"].keys()) == {
        "var_95",
        "var_99",
        "cvar_5",
        "skewness",
        "kurtosis",
    }
    assert set(payload["drawdown"].keys()) == {
        "max_drawdown",
        "avg_drawdown",
        "current_drawdown",
        "longest_drawdown_days",
        "time_underwater_days",
    }
    assert payload["risk_free_rate_used"] == 0.04
    assert payload["num_observations"] == 59


async def test_response_matches_compute_statistics_directly(client):
    dates, equity = _make_equity(120)
    resp = await client.post(
        "/api/statistics",
        json={"dates": dates, "equity": equity, "risk_free_rate": 0.05},
    )
    assert resp.status_code == 200
    payload = resp.json()

    direct = compute_statistics(
        np.asarray(dates, dtype=np.int64),
        np.asarray(equity, dtype=np.float64),
        0.05,
    )

    assert math.isclose(payload["return"]["total_return"], direct.return_.total_return)
    assert math.isclose(payload["return"]["cagr"], direct.return_.cagr)
    assert math.isclose(
        payload["risk_adjusted"]["sharpe_ratio"], direct.risk_adjusted.sharpe_ratio
    )
    assert math.isclose(payload["drawdown"]["max_drawdown"], direct.drawdown.max_drawdown)


async def test_missing_rf_defaults_to_004(client):
    dates, equity = _make_equity(30)
    resp = await client.post(
        "/api/statistics",
        json={"dates": dates, "equity": equity},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["risk_free_rate_used"] == 0.04


async def test_null_rf_defaults_to_004(client):
    dates, equity = _make_equity(30)
    resp = await client.post(
        "/api/statistics",
        json={"dates": dates, "equity": equity, "risk_free_rate": None},
    )
    assert resp.status_code == 200
    assert resp.json()["risk_free_rate_used"] == 0.04


# ── Validation errors ─────────────────────────────────────────────────


async def test_length_mismatch_400(client):
    resp = await client.post(
        "/api/statistics",
        json={"dates": [20240101, 20240102], "equity": [100.0]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert "length" in body["message"].lower()


async def test_fewer_than_two_400(client):
    resp = await client.post(
        "/api/statistics",
        json={"dates": [20240101], "equity": [100.0]},
    )
    assert resp.status_code == 400
    assert resp.json()["error_type"] == "validation_error"


async def test_non_positive_equity_400(client):
    resp = await client.post(
        "/api/statistics",
        json={
            "dates": [20240101, 20240102, 20240103],
            "equity": [100.0, 0.0, 50.0],
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert "positive" in body["message"].lower()


async def test_negative_equity_400(client):
    resp = await client.post(
        "/api/statistics",
        json={
            "dates": [20240101, 20240102],
            "equity": [100.0, -50.0],
        },
    )
    assert resp.status_code == 400


async def test_pydantic_validation_returns_project_envelope(client):
    # ``null`` inside the equity list trips the Pydantic float coercion
    # before our hand-rolled checks. Must come back as the project's
    # ``{error_type, message}`` envelope with status 400 — NOT FastAPI's
    # default ``{"detail": [...]}`` 422.
    resp = await client.post(
        "/api/statistics",
        json={"dates": [20240101, 20240102], "equity": [None, 2.0]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert isinstance(body["message"], str) and body["message"]
    assert "detail" not in body


async def test_positive_infinity_equity_400(client):
    # ``Infinity`` / ``NaN`` are non-standard JSON literals but the
    # JS frontend can emit them and Python's parser accepts them by
    # default. Send raw to mirror the real-world failure mode.
    resp = await client.post(
        "/api/statistics",
        content='{"dates":[20240101,20240102,20240103],"equity":[100.0,Infinity,110.0]}',
        headers={"content-type": "application/json"},
    )
    # ``+inf`` reaches the route (Pydantic accepts it as a float). Our
    # finite-guard must reject — without it ``np.all(equity > 0)`` is True
    # and downstream metrics stamp NaN.
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"


async def test_negative_infinity_equity_400(client):
    resp = await client.post(
        "/api/statistics",
        content='{"dates":[20240101,20240102],"equity":[100.0,-Infinity]}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


async def test_nan_equity_400(client):
    resp = await client.post(
        "/api/statistics",
        content='{"dates":[20240101,20240102],"equity":[100.0,NaN]}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


async def test_skew_kurtosis_null_for_small_sample(client):
    # 25 points -> 24 returns < 30 threshold -> null
    dates, equity = _make_equity(25)
    resp = await client.post(
        "/api/statistics", json={"dates": dates, "equity": equity}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["tail"]["skewness"] is None
    assert payload["tail"]["kurtosis"] is None
