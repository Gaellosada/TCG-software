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
                        "kind": "saved",
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
    assert inst["kind"] == "saved"
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
                        "kind": "saved",
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


# ===========================================================================
# Inline-shape tests (Wave I-back iter1, EXTEND).
#
# Inline baskets bypass the DB pre-pass entirely: the resolver stamps each
# leg with a host collection (derived from id prefix for future/option/
# index; probed for equity), then `_parse_input` constructs an
# `InstrumentBasket` straight from the wire payload.  These tests assert
# the happy path, the no-DB-call invariant, identity-tuple stability, and
# the various leg-level validation rules.
# ===========================================================================


def _inline_basket_spec(
    *,
    input_id: str = "B",
    asset_class: str = "equity",
    legs: list[dict] | None = None,
    signal_id: str = "sig-inline",
    entry_condition_input_id: str | None = None,
) -> dict:
    """Helper: build a minimal signal-compute body with one inline-basket
    input and an AlwaysOn entry block (so the engine runs through and
    produces a position whose weighted-sum was actually evaluated)."""
    if legs is None:
        legs = [
            {"instrument_id": "SPY", "weight": 0.6},
            {"instrument_id": "QQQ", "weight": 0.4},
        ]
    ec_id = entry_condition_input_id or input_id
    return {
        "spec": {
            "id": signal_id,
            "name": "Inline E2E",
            "inputs": [
                {
                    "id": input_id,
                    "instrument": {
                        "type": "basket",
                        "kind": "inline",
                        "asset_class": asset_class,
                        "legs": legs,
                    },
                }
            ],
            "rules": {
                "entries": [
                    {
                        "id": "E1",
                        "name": "AlwaysOn",
                        "input_id": input_id,
                        "weight": 100.0,
                        "conditions": [
                            {
                                "op": "gt",
                                "lhs": {
                                    "kind": "instrument",
                                    "input_id": ec_id,
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


def test_signals_inline_basket_compute_weighted_sum(
    client: TestClient,
    fake_market_data: MagicMock,
    basket_repo: _BasketRepo,
) -> None:
    """Inline basket with two equity legs: weighted-sum runs, response
    shape is the kind-discriminated inline payload, and no DB pre-pass
    was triggered (repo.get_by_id is never called)."""
    # Track repo.get_by_id calls (the resolver short-circuit must skip them).
    seen_calls: list[tuple[str, str]] = []
    orig_get = basket_repo.get_by_id

    async def trace_get_by_id(doc_type: str, doc_id: str):  # noqa: ANN202
        seen_calls.append((doc_type, doc_id))
        return await orig_get(doc_type, doc_id)

    basket_repo.get_by_id = trace_get_by_id  # type: ignore[method-assign]

    body = _inline_basket_spec(asset_class="equity")
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # One position with the inline-shape payload.
    assert len(data["positions"]) == 1
    inst = data["positions"][0]["instrument"]
    assert inst["type"] == "basket"
    assert inst["kind"] == "inline"
    assert inst["asset_class"] == "equity"
    assert "basket_id" not in inst  # inline never carries an id
    leg_ids = {leg["instrument_id"] for leg in inst["legs"]}
    assert leg_ids == {"SPY", "QQQ"}

    # Q6 short-circuit: inline-only request must not hit the repo.
    assert seen_calls == [], (
        f"resolver hit the repo for an inline-only request: {seen_calls}"
    )

    # Equity-probe path actually ran: get_prices was called for SPY/QQQ.
    called_ids = {
        call.args[1] for call in fake_market_data.get_prices.await_args_list
    }
    assert {"SPY", "QQQ"}.issubset(called_ids)


def test_signals_inline_basket_zero_legs_returns_validation_error(
    client: TestClient,
) -> None:
    """Inline with empty `legs` is rejected at Pydantic time (min_length=1)."""
    body = _inline_basket_spec(legs=[])
    resp = client.post("/api/signals/compute", json=body)
    # Pydantic's request-body validation yields HTTP 422 with the
    # project-standard envelope's `error_type="validation_error"`.
    assert resp.status_code in (400, 422), resp.text


def test_signals_inline_basket_single_leg_compute(
    client: TestClient,
) -> None:
    """Inline with a single SPY leg, weight=1.0 — the weighted sum
    equals the SPY close series, position runs to completion."""
    body = _inline_basket_spec(
        legs=[{"instrument_id": "SPY", "weight": 1.0}],
    )
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["positions"]) == 1
    assert data["positions"][0]["instrument"]["kind"] == "inline"
    assert len(data["positions"][0]["values"]) == len(data["timestamps"])


def test_signals_inline_basket_negative_weight_compute(
    client: TestClient, fake_market_data: MagicMock
) -> None:
    """A single negative-weight leg flips the sign relative to the
    baseline positive-weight equivalent.  We verify just that the
    request succeeds and emits a position — the sign-flip semantic is
    covered by the underlying engine tests."""
    body = _inline_basket_spec(
        legs=[{"instrument_id": "SPY", "weight": -1.0}],
    )
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text


def test_signals_saved_basket_kind_discriminator_required(
    client: TestClient,
) -> None:
    """Saved-shape with `kind` missing is rejected at the wire layer —
    proves the discriminator is actually wired."""
    body = {
        "spec": {
            "id": "sig-x",
            "name": "X",
            "inputs": [
                {
                    "id": "B",
                    "instrument": {
                        "type": "basket",
                        # NO `kind` field — must be rejected.
                        "basket_id": "basket-e2e",
                    },
                }
            ],
            "rules": {"entries": [], "exits": []},
        },
        "indicators": [],
        "instruments": {},
    }
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code in (400, 422), resp.text


def test_signals_inline_basket_intersects_dates_across_legs(
    fake_market_data: MagicMock, basket_repo: _BasketRepo
) -> None:
    """Two legs with disjoint date ranges raise a SignalDataError
    ("no overlapping dates between legs") via the standard envelope."""
    # Tweak fake_market_data so SPY and QQQ have non-overlapping dates.
    disjoint_qqq_dates = np.array(
        [20240201, 20240202, 20240205, 20240206, 20240207, 20240208],
        dtype=np.int64,
    )
    disjoint_qqq = PriceSeries(
        dates=disjoint_qqq_dates,
        open=_QQQ_CLOSES - 1.0,
        high=_QQQ_CLOSES + 1.0,
        low=_QQQ_CLOSES - 2.0,
        close=_QQQ_CLOSES,
        volume=np.full(6, 1000.0, dtype=np.float64),
    )

    async def disjoint_get_prices(
        collection: str, instrument_id: str, *, start=None, end=None, provider=None
    ):
        if instrument_id == "SPY":
            return _price_series(_SPY_CLOSES)
        if instrument_id == "QQQ":
            return disjoint_qqq
        return None

    fake_market_data.get_prices = AsyncMock(side_effect=disjoint_get_prices)

    app = create_app()
    app.state.market_data = fake_market_data
    app.dependency_overrides[get_write_repository] = lambda: basket_repo
    client_local = TestClient(app)

    body = _inline_basket_spec(asset_class="equity")
    resp = client_local.post("/api/signals/compute", json=body)
    # SignalDataError is surfaced through error_response("data", ...).
    # Whichever specific envelope key is used, the response is non-200.
    assert resp.status_code != 200, resp.text
    payload = resp.json()
    msg = payload.get("message", "")
    assert "overlap" in msg.lower() or "no business days" in msg.lower() or (
        "basket" in msg.lower()
    ), payload


def test_signals_inline_basket_unknown_asset_class_rejected(
    client: TestClient,
) -> None:
    """Pydantic Literal["future","option","index","equity"] rejects any
    other asset class at request-validation time."""
    body = _inline_basket_spec(asset_class="commodity")  # type: ignore[arg-type]
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code in (400, 422), resp.text


def test_signals_inline_basket_instrument_identity_stable() -> None:
    """Two inline baskets with the same legs in different orders share
    a structural identity tuple (so the engine dedupes their fetches).

    Unit test on `_instrument_identity` directly — no HTTP layer needed."""
    from tcg.engine.signal_exec import _instrument_identity
    from tcg.types.signal import InstrumentBasket

    b1 = InstrumentBasket(
        legs=(
            {"instrument_id": "SPY", "weight": 0.6, "collection": "ETF"},
            {"instrument_id": "QQQ", "weight": 0.4, "collection": "ETF"},
        ),
        collection="ETF",
        basket_id=None,
        asset_class="equity",
    )
    b2 = InstrumentBasket(
        legs=(
            {"instrument_id": "QQQ", "weight": 0.4, "collection": "ETF"},
            {"instrument_id": "SPY", "weight": 0.6, "collection": "ETF"},
        ),
        collection="ETF",
        basket_id=None,
        asset_class="equity",
    )
    assert _instrument_identity(b1) == _instrument_identity(b2)

    # Different weights ⇒ different identity.
    b3 = InstrumentBasket(
        legs=(
            {"instrument_id": "SPY", "weight": 0.5, "collection": "ETF"},
            {"instrument_id": "QQQ", "weight": 0.5, "collection": "ETF"},
        ),
        collection="ETF",
        basket_id=None,
        asset_class="equity",
    )
    assert _instrument_identity(b1) != _instrument_identity(b3)

    # Saved and inline with structurally-equal legs DON'T collide
    # because the saved identity uses ("basket","saved",basket_id).
    b_saved = InstrumentBasket(
        legs=b1.legs,
        collection="ETF",
        basket_id="some-saved-id",
        asset_class=None,
    )
    assert _instrument_identity(b1) != _instrument_identity(b_saved)

    # And a user-chosen basket_id of "inline" cannot collide with a
    # structural-identity inline basket (Q2 collision-avoidance).
    b_named_inline = InstrumentBasket(
        legs=b1.legs,
        collection="ETF",
        basket_id="inline",
        asset_class=None,
    )
    assert _instrument_identity(b_named_inline) != _instrument_identity(b1)


def test_signals_inline_basket_id_asset_class_mismatch_rejected(
    fake_market_data: MagicMock, basket_repo: _BasketRepo
) -> None:
    """A leg whose instrument_id can't be bucketed into the declared
    asset_class is rejected with a validation envelope."""
    app = create_app()
    app.state.market_data = fake_market_data
    app.dependency_overrides[get_write_repository] = lambda: basket_repo
    client_local = TestClient(app)

    # asset_class="future" but instrument_id doesn't start with FUT_.
    body = _inline_basket_spec(
        asset_class="future",
        legs=[{"instrument_id": "SPY", "weight": 1.0}],
    )
    resp = client_local.post("/api/signals/compute", json=body)
    assert resp.status_code == 400, resp.text
    payload = resp.json()
    assert payload.get("error_type") == "validation"
    assert "asset_class" in payload.get("message", "")
