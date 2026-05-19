"""E2E test for ``/api/signals/compute`` with a ``type:"basket"`` input.

Addresses ORDERS.md success criterion #6: "at least one E2E test asserts a
signal referencing a basket evaluates without crashing." Prior coverage
was unit-only on the individual wiring pieces (`_resolve_basket_inputs`,
`make_signal_fetcher`'s basket branch, `compute_input_overlap`'s basket
branch). This test exercises the full HTTP path from request to response.

Approach (Option A from the iteration brief): use the in-memory
``_FakeRepo`` pattern from ``test_persistence_api.py`` to seed a real
``BasketDoc``; mock ``MarketDataService.get_prices`` to return tiny
synthetic ``PriceSeries`` arrays for each leg; POST a minimal v4 signal
spec whose single input is ``{type: "basket", basket_id: "<seeded>"}``;
assert HTTP 200 with the expected response shape and a position whose
instrument payload echoes the basket id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.app import create_app
from tcg.types.market import PriceSeries
from tcg.types.persistence import BasketDoc, Category, DocType


# ---------------------------------------------------------------------------
# Synthetic data — two legs with identical date arrays so the weighted
# combination has the same length and the engine can evaluate every bar.
# ---------------------------------------------------------------------------

_DATES = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108, 20240109],
    dtype=np.int64,
)
_SPY_CLOSES = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
_QQQ_CLOSES = np.array([200.0, 201.0, 200.0, 202.0, 203.0, 204.0])


def _price_series(closes: np.ndarray) -> PriceSeries:
    n = closes.shape[0]
    return PriceSeries(
        dates=_DATES,
        open=closes - 1.0,
        high=closes + 1.0,
        low=closes - 2.0,
        close=closes,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


# ---------------------------------------------------------------------------
# Minimal fake repo — only ``get_by_id`` is exercised by the resolver.
# Mirrors the ``_FakeRepo`` pattern from ``test_persistence_api.py`` but
# keeps the surface tight; we don't go through the CRUD routes here.
# ---------------------------------------------------------------------------


class _BasketRepo:
    """In-memory ``WriteRepository`` stand-in for basket lookup.

    Only the ``get_by_id`` method is invoked by ``_resolve_basket_inputs``;
    the other repo methods are not reached by the ``/compute`` path.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}

    def seed(self, doc: Any) -> None:
        self._store[(doc.type, doc.id)] = doc

    async def get_by_id(self, doc_type: str, doc_id: str) -> Any:
        return self._store.get((doc_type, doc_id))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_market_data() -> MagicMock:
    svc = MagicMock()

    async def fake_get_prices(
        collection: str, instrument_id: str, *, start=None, end=None, provider=None
    ):
        if instrument_id == "SPY":
            return _price_series(_SPY_CLOSES)
        if instrument_id == "QQQ":
            return _price_series(_QQQ_CLOSES)
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)
    return svc


@pytest.fixture
def basket_repo() -> _BasketRepo:
    repo = _BasketRepo()
    now = datetime.now(timezone.utc)
    repo.seed(
        BasketDoc(
            id="basket-e2e",
            type="basket",
            name="E2E Basket",
            category=Category.RESEARCH,
            created_at=now,
            updated_at=now,
            legs=(
                {"instrument_id": "SPY", "collection": "ETF", "weight": 0.6},
                {"instrument_id": "QQQ", "collection": "ETF", "weight": 0.4},
            ),
        )
    )
    return repo


@pytest.fixture
def client(
    basket_repo: _BasketRepo, fake_market_data: MagicMock
) -> TestClient:
    """TestClient with both deps wired without starting the lifespan.

    ``TestClient(app)`` without ``with``-statement does NOT trigger the
    Mongo-touching lifespan, so we can set ``app.state.market_data``
    manually. ``get_write_repository`` is overridden through FastAPI's
    dependency-override mechanism (same pattern as the persistence-API
    tests).
    """
    app = create_app()
    app.state.market_data = fake_market_data
    app.dependency_overrides[get_write_repository] = lambda: basket_repo
    return TestClient(app)


# ---------------------------------------------------------------------------
# The E2E test
# ---------------------------------------------------------------------------


def test_compute_with_basket_input_returns_200_with_basket_position(
    client: TestClient, fake_market_data: MagicMock
) -> None:
    """POST ``/api/signals/compute`` with a ``type:"basket"`` input and
    assert the full happy path: HTTP 200, top-level response shape,
    one position keyed to the basket input, instrument payload echoes
    the basket id, and the synthetic ``get_prices`` was called for both
    legs (proving the weighted-sum fetcher actually ran).
    """
    body = {
        "spec": {
            "id": "sig-basket-e2e",
            "name": "Basket E2E",
            "inputs": [
                {
                    "id": "B",
                    "instrument": {
                        "type": "basket",
                        "basket_id": "basket-e2e",
                    },
                }
            ],
            "rules": {
                # Single trivial entry block: basket-close > 0. With the
                # synthetic closes (60% * SPY + 40% * QQQ, all > 0) the
                # condition is always true, so the engine runs to
                # completion and emits a position for input "B".
                "entries": [
                    {
                        "id": "E1",
                        "name": "AlwaysOn",
                        "input_id": "B",
                        "weight": 100.0,
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {
                                    "kind": "instrument",
                                    "input_id": "B",
                                    "field": "close",
                                },
                                "rhs": {"kind": "constant", "value": 0.0},
                            }
                        ],
                    }
                ],
                "exits": [],
            },
        },
        "indicators": [],
        "instruments": {},
    }

    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Top-level shape — same contract as the existing signals roundtrip.
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
    assert isinstance(data["events"], list)
    assert isinstance(data["clipped"], bool)
    assert isinstance(data["diagnostics"], dict)

    # Exactly one position, for input "B", whose instrument payload is
    # the ``type:"basket"`` flavour built by ``_instrument_payload``.
    assert len(data["positions"]) == 1
    pos = data["positions"][0]
    assert pos["input_id"] == "B"
    inst = pos["instrument"]
    assert inst["type"] == "basket"
    assert inst["basket_id"] == "basket-e2e"
    # Legs round-trip through the response (snapshot of what was fetched).
    assert isinstance(inst["legs"], list)
    leg_ids = {leg["instrument_id"] for leg in inst["legs"]}
    assert leg_ids == {"SPY", "QQQ"}

    # ``values`` is the per-bar net position (signed weight when the
    # entry latch is open). The bar count must match the timestamps.
    assert len(pos["values"]) == len(data["timestamps"])

    # Weighted-sum fetcher actually ran: ``get_prices`` was called for
    # both legs. Without that, the basket branch in
    # ``make_signal_fetcher`` never executed.
    called_instrument_ids = {
        call.args[1] for call in fake_market_data.get_prices.await_args_list
    } | {
        call.kwargs.get("instrument_id")
        for call in fake_market_data.get_prices.await_args_list
        if "instrument_id" in call.kwargs
    }
    # Drop any ``None`` slot from kwargs intersection above.
    called_instrument_ids.discard(None)
    assert {"SPY", "QQQ"}.issubset(called_instrument_ids)


def test_compute_with_unknown_basket_id_returns_validation_error(
    client: TestClient,
) -> None:
    """Reference an id the repo doesn't know about — the resolver must
    raise ``SignalValidationError`` and the endpoint maps it to the
    project's standard validation envelope (HTTP 200 + error envelope,
    per the ``error_response`` contract used elsewhere in the file).
    """
    body = {
        "spec": {
            "id": "sig-bad",
            "name": "Bad",
            "inputs": [
                {
                    "id": "B",
                    "instrument": {
                        "type": "basket",
                        "basket_id": "does-not-exist",
                    },
                }
            ],
            "rules": {"entries": [], "exits": []},
        },
        "indicators": [],
        "instruments": {},
    }
    resp = client.post("/api/signals/compute", json=body)
    # The endpoint funnels validation errors through ``error_response``
    # which returns a JSONResponse with the project-wide error envelope.
    # Default status is 400.
    assert resp.status_code == 400, resp.text
    body_json = resp.json()
    assert body_json.get("error_type") == "validation"
    assert "does-not-exist" in body_json.get("message", "")
