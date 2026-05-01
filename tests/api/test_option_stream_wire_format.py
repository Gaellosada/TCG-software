"""FE↔BE wire-format parity test for ``OptionStreamRef``.

Sign 12 (added by reviewer feedback after Iteration 1 FAIL): when a new
SeriesRef variant is shipped, at least one test must ingest the EXACT
JSON the FE form emits and assert the BE accepts it.  Mocking out the
API client on the FE while feeding BE-shaped fixtures on the BE leaves
the wire boundary untested — exactly the silent-failure mode that hit
this task in Iteration 1.

This test exercises two layers:

1. Direct Pydantic validation of the discriminated unions
   (``SelectionCriterion`` / ``MaturityRule``) against the
   FE-emitted shape — ``target`` (ByMoneyness/ByDelta) and
   ``target_days`` (NearestToTarget) — guaranteed by the field aliases
   on ``ByMoneyness.target_K_over_S``, ``ByDelta.target_delta``, and
   ``NearestToTarget.target_dte_days``.

2. End-to-end POST to ``/api/indicators/compute`` with the EXACT JSON
   the FE ``OptionStreamForm`` emits.  The route handler is patched at
   ``_materialise_option_stream`` so we never reach the chain-data
   layer — the test purely asserts that Pydantic validation succeeds
   (no 422 with ``error_type='validation'`` from missing required
   fields).

   Pre-flight 422s (TAUTOLOGICAL_OPTION_STREAM, STREAM_UNAVAILABLE_FOR_ROOT)
   are NOT triggered by the test bodies — selection vs stream is
   non-tautological and SP_500 has greeks.

A backwards-compatibility row also POSTs the OLD (BE field-name) shape
to confirm ``populate_by_name=True`` keeps existing call sites green.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api._models_options import (
    ByDelta,
    ByMoneyness,
    NearestToTarget,
)
from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.indicators import router as indicators_router
from tcg.types.errors import TCGError
from tcg.types.options import OptionRootInfo


# ── Layer 1 — Pydantic-only parity ─────────────────────────────────────


class TestPydanticAliasParity:
    """The discriminated-union Pydantic models accept BOTH the FE-emitted
    wire name and the BE field name (populate_by_name=True)."""

    # FE-shape (canonical wire name = the alias)
    def test_by_moneyness_fe_shape(self):
        m = ByMoneyness.model_validate(
            {"kind": "by_moneyness", "target": 1.0, "tolerance": 0.05}
        )
        assert m.target_K_over_S == 1.0
        assert m.tolerance == 0.05

    def test_by_delta_fe_shape(self):
        m = ByDelta.model_validate(
            {"kind": "by_delta", "target": 0.25, "tolerance": 0.05, "strict": False}
        )
        assert m.target_delta == 0.25
        assert m.tolerance == 0.05
        assert m.strict is False

    def test_nearest_to_target_fe_shape(self):
        m = NearestToTarget.model_validate(
            {"kind": "nearest_to_target", "target_days": 30}
        )
        assert m.target_dte_days == 30

    # BE-shape (backwards-compat: old call sites construct via field name)
    def test_by_moneyness_be_shape_backwards_compat(self):
        m = ByMoneyness.model_validate(
            {"kind": "by_moneyness", "target_K_over_S": 1.02, "tolerance": 0.01}
        )
        assert m.target_K_over_S == 1.02

    def test_by_delta_be_shape_backwards_compat(self):
        m = ByDelta.model_validate(
            {"kind": "by_delta", "target_delta": -0.10}
        )
        assert m.target_delta == -0.10

    def test_nearest_to_target_be_shape_backwards_compat(self):
        m = NearestToTarget.model_validate(
            {"kind": "nearest_to_target", "target_dte_days": 45}
        )
        assert m.target_dte_days == 45

    def test_kwarg_construction_still_works(self):
        """Existing tests / call sites use kwargs by field name — must
        keep working under populate_by_name=True."""
        m_money = ByMoneyness(target_K_over_S=1.05, tolerance=0.02)
        assert m_money.target_K_over_S == 1.05
        m_delta = ByDelta(target_delta=0.30)
        assert m_delta.target_delta == 0.30
        m_near = NearestToTarget(target_dte_days=60)
        assert m_near.target_dte_days == 60


# ── Layer 2 — Full POST through the FastAPI router ─────────────────────


_TRIVIAL_INDICATOR = (
    "def compute(series):\n"
    "    return series['x']\n"
)


@pytest.fixture
def mock_app(monkeypatch):
    """FastAPI app wired for indicators with a stubbed materialiser.

    Sign 12 parity: we want to test that Pydantic accepts the FE-emitted
    JSON.  We do NOT want to actually run the chain query — that's
    exercised by ``test_stream_resolver.py``.  Patching
    ``_materialise_option_stream`` returns a tiny synthetic series so
    the route handler proceeds past Pydantic validation and reaches the
    indicator-execution path; a 200 with empty intersection is fine.
    """
    svc = MagicMock()
    # SP_500 has greeks → STREAM_UNAVAILABLE_FOR_ROOT does not fire.
    svc.list_option_roots = AsyncMock(
        return_value=[
            OptionRootInfo(
                collection="OPT_SP_500",
                name="SP 500",
                has_greeks=True,
                providers=("IVOLATILITY",),
                expiration_first=date(2005, 1, 21),
                expiration_last=date(2027, 12, 19),
                doc_count_estimated=1234567,
                strike_factor_verified=True,
                last_trade_date=None,
            ),
        ]
    )

    # Patch the materialiser to bypass the chain-data layer.
    async def fake_materialise(ref, *, svc, start_date, end_date):  # noqa: ARG001
        dates = np.array([20240102, 20240103, 20240104], dtype=np.int64)
        values = np.array([0.20, 0.21, 0.22], dtype=np.float64)
        diagnostics: list[str | None] = [None, None, None]
        return dates, values, diagnostics

    monkeypatch.setattr(
        "tcg.core.api.indicators._materialise_option_stream",
        fake_materialise,
    )

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(indicators_router)
    app.state.market_data = svc
    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _fe_body(*, selection: dict, maturity: dict, stream: str = "iv") -> dict:
    """Build a request body using the EXACT shape ``OptionStreamForm`` emits."""
    return {
        "code": _TRIVIAL_INDICATOR,
        "params": {},
        "series": {
            "x": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "option_type": "C",
                "cycle": None,
                "maturity": maturity,
                "selection": selection,
                "stream": stream,
            }
        },
        "start": "2024-01-02",
        "end": "2024-01-04",
        "indicator_id": "atm-contract-iv",
    }


class TestRouteAcceptsFEShape:
    """End-to-end: the FastAPI route accepts the JSON the FE form emits.

    We assert the response is NOT a Pydantic validation 422.  The request
    body is non-tautological and the root has greeks, so neither
    pre-flight 422 (TAUTOLOGICAL_OPTION_STREAM /
    STREAM_UNAVAILABLE_FOR_ROOT) fires.  With the materialiser stubbed
    the response is 200.
    """

    async def test_by_moneyness_fe_shape_post(self, client: AsyncClient):
        body = _fe_body(
            selection={"kind": "by_moneyness", "target": 1.0, "tolerance": 0.05},
            maturity={"kind": "next_third_friday", "offset_months": 0},
        )
        resp = await client.post("/api/indicators/compute", json=body)
        # If aliases are missing this is a Pydantic 422 with
        # error_type='validation' and a "field required: target_K_over_S" detail.
        assert resp.status_code != 422, resp.text
        assert resp.status_code == 200, resp.text

    async def test_by_delta_fe_shape_post(self, client: AsyncClient):
        body = _fe_body(
            selection={
                "kind": "by_delta",
                "target": 0.25,
                "tolerance": 0.05,
                "strict": False,
            },
            maturity={"kind": "next_third_friday", "offset_months": 0},
            stream="iv",  # not 'delta' → avoids TAUTOLOGICAL_OPTION_STREAM
        )
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code != 422, resp.text
        assert resp.status_code == 200, resp.text

    async def test_nearest_to_target_fe_shape_post(self, client: AsyncClient):
        body = _fe_body(
            selection={"kind": "by_moneyness", "target": 1.0, "tolerance": 0.05},
            maturity={"kind": "nearest_to_target", "target_days": 30},
        )
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code != 422, resp.text
        assert resp.status_code == 200, resp.text

    async def test_be_shape_backwards_compat_post(self, client: AsyncClient):
        """Old BE field-name shape still works — populate_by_name=True."""
        body = _fe_body(
            selection={
                "kind": "by_moneyness",
                "target_K_over_S": 1.0,
                "tolerance": 0.05,
            },
            maturity={
                "kind": "nearest_to_target",
                "target_dte_days": 30,
            },
        )
        resp = await client.post("/api/indicators/compute", json=body)
        assert resp.status_code != 422, resp.text
        assert resp.status_code == 200, resp.text
