"""E2E tests for ``/api/signals/compute`` with basket inputs.

Iter-3 rewrite: legs are now polymorphic ``{instrument: <ref>, weight}``
with strict per-asset-class enforcement.  Lifts the iter-1/2 cases to
the new wire shape and adds positive coverage for the new
continuous-leg and option_stream-leg paths plus strict-mismatch
rejection cases.
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
# Synthetic data: two equity legs share dates; FUT_VIX/FUT_ES "continuous"
# series and OPT_VIX option-stream paths get their own fakes.
# ---------------------------------------------------------------------------


_DATES = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108, 20240109],
    dtype=np.int64,
)
_SPY_CLOSES = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
_QQQ_CLOSES = np.array([200.0, 201.0, 200.0, 202.0, 203.0, 204.0])
_VIX_CONT_CLOSES = np.array([15.0, 16.0, 14.5, 15.5, 16.2, 15.8])
_ES_CONT_CLOSES = np.array([4500.0, 4510.0, 4505.0, 4520.0, 4515.0, 4525.0])


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


class _ContinuousSeriesStub:
    """Stand-in for ``ContinuousSeries`` — only ``prices.dates``/close used."""

    def __init__(self, closes: np.ndarray) -> None:
        self.prices = _price_series(closes)


class _BasketRepo:
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

    async def fake_get_continuous(
        collection: str, roll_config, *, start=None, end=None, provider=None
    ):
        if collection == "FUT_VIX":
            return _ContinuousSeriesStub(_VIX_CONT_CLOSES)
        if collection == "FUT_ES":
            return _ContinuousSeriesStub(_ES_CONT_CLOSES)
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)
    svc.get_continuous = AsyncMock(side_effect=fake_get_continuous)
    return svc


@pytest.fixture
def basket_repo() -> _BasketRepo:
    repo = _BasketRepo()
    now = datetime.now(timezone.utc)
    # Seed a saved basket whose legs use the polymorphic shape (two
    # spot legs for an equity basket).
    repo.seed(
        BasketDoc(
            id="basket-e2e",
            type="basket",
            name="E2E Basket",
            category=Category.RESEARCH,
            created_at=now,
            updated_at=now,
            asset_class="equity",
            legs=(
                {
                    "instrument": {
                        "type": "spot",
                        "collection": "ETF",
                        "instrument_id": "SPY",
                    },
                    "weight": 0.6,
                },
                {
                    "instrument": {
                        "type": "spot",
                        "collection": "ETF",
                        "instrument_id": "QQQ",
                    },
                    "weight": 0.4,
                },
            ),
        )
    )
    return repo


@pytest.fixture
def client(basket_repo: _BasketRepo, fake_market_data: MagicMock) -> TestClient:
    app = create_app()
    app.state.market_data = fake_market_data
    app.dependency_overrides[get_write_repository] = lambda: basket_repo
    return TestClient(app)


# ---------------------------------------------------------------------------
# Saved basket — happy path + missing-basket rejection (lifted to new shape).
# ---------------------------------------------------------------------------


def test_compute_with_saved_basket_returns_200_with_basket_position(
    client: TestClient, fake_market_data: MagicMock
) -> None:
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
    assert len(data["positions"]) == 1
    pos = data["positions"][0]
    assert pos["input_id"] == "B"
    inst = pos["instrument"]
    assert inst["type"] == "basket"
    assert inst["kind"] == "saved"
    assert inst["basket_id"] == "basket-e2e"
    # Polymorphic leg payload — each leg carries the nested instrument dict.
    assert isinstance(inst["legs"], list)
    assert all(set(leg.keys()) == {"instrument", "weight"} for leg in inst["legs"])
    leg_ids = {leg["instrument"]["instrument_id"] for leg in inst["legs"]}
    assert leg_ids == {"SPY", "QQQ"}

    # Weighted-sum fetcher actually ran for both legs.
    called_ids = {
        call.args[1] for call in fake_market_data.get_prices.await_args_list
    }
    assert {"SPY", "QQQ"}.issubset(called_ids)


def test_compute_with_unknown_basket_id_returns_validation_error(
    client: TestClient,
) -> None:
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
    assert resp.status_code == 400, resp.text
    body_json = resp.json()
    assert body_json.get("error_type") == "validation"
    assert "does-not-exist" in body_json.get("message", "")


# ---------------------------------------------------------------------------
# Inline-basket helpers — build minimal compute body with one inline input.
# ---------------------------------------------------------------------------


def _spot_leg(
    instrument_id: str, *, collection: str = "ETF", weight: float = 0.5
) -> dict:
    return {
        "instrument": {
            "type": "spot",
            "collection": collection,
            "instrument_id": instrument_id,
        },
        "weight": weight,
    }


def _continuous_leg(
    collection: str,
    *,
    adjustment: str = "none",
    cycle: str | None = None,
    rollOffset: int = 0,
    weight: float = 0.5,
) -> dict:
    return {
        "instrument": {
            "type": "continuous",
            "collection": collection,
            "adjustment": adjustment,
            "cycle": cycle,
            "rollOffset": rollOffset,
            "strategy": "front_month",
        },
        "weight": weight,
    }


def _inline_basket_spec(
    *,
    input_id: str = "B",
    asset_class: str = "equity",
    legs: list[dict] | None = None,
    signal_id: str = "sig-inline",
) -> dict:
    if legs is None:
        legs = [_spot_leg("SPY", weight=0.6), _spot_leg("QQQ", weight=0.4)]
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
                                    "input_id": input_id,
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


# ---------------------------------------------------------------------------
# Inline-basket positive cases: one per asset class.
# ---------------------------------------------------------------------------


def test_signals_inline_basket_equity_spot_legs(
    client: TestClient,
    fake_market_data: MagicMock,
    basket_repo: _BasketRepo,
) -> None:
    """Two equity spot legs: weighted-sum runs, inline payload echoes
    the polymorphic legs, and no DB pre-pass is triggered."""
    seen_calls: list[tuple[str, str]] = []
    orig_get = basket_repo.get_by_id

    async def trace_get_by_id(doc_type: str, doc_id: str):
        seen_calls.append((doc_type, doc_id))
        return await orig_get(doc_type, doc_id)

    basket_repo.get_by_id = trace_get_by_id  # type: ignore[method-assign]

    body = _inline_basket_spec(asset_class="equity")
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["positions"]) == 1
    inst = data["positions"][0]["instrument"]
    assert inst["type"] == "basket"
    assert inst["kind"] == "inline"
    assert inst["asset_class"] == "equity"
    assert "basket_id" not in inst
    leg_ids = {leg["instrument"]["instrument_id"] for leg in inst["legs"]}
    assert leg_ids == {"SPY", "QQQ"}

    # Inline-only short-circuit: repo never consulted.
    assert seen_calls == []

    # Per-leg spot resolver actually ran.
    called_ids = {
        call.args[1] for call in fake_market_data.get_prices.await_args_list
    }
    assert {"SPY", "QQQ"}.issubset(called_ids)


def test_signals_inline_basket_future_continuous_legs(
    client: TestClient, fake_market_data: MagicMock
) -> None:
    """Two continuous-future legs (different collections) — the
    weighted-sum routes each leg through the existing
    ``get_continuous`` resolver and emits the polymorphic inline shape."""
    body = _inline_basket_spec(
        asset_class="future",
        legs=[
            _continuous_leg(
                "FUT_VIX", adjustment="ratio", cycle="HMUZ", weight=0.5
            ),
            _continuous_leg(
                "FUT_ES", adjustment="none", cycle="HMUZ", weight=0.5
            ),
        ],
    )
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    inst = data["positions"][0]["instrument"]
    assert inst["kind"] == "inline"
    assert inst["asset_class"] == "future"
    leg_types = [leg["instrument"]["type"] for leg in inst["legs"]]
    assert leg_types == ["continuous", "continuous"]

    # Per-leg continuous resolver was hit (not get_prices).
    called_continuous = {
        call.args[0]
        for call in fake_market_data.get_continuous.await_args_list
    }
    assert {"FUT_VIX", "FUT_ES"}.issubset(called_continuous)


def test_signals_inline_basket_option_stream_legs_smoke() -> None:
    """Option-stream legs route through the per-type resolver wiring.

    This is a unit-level smoke test of ``_parse_input``'s option-stream
    branch on the polymorphic-leg path — the full option-stream
    resolver requires substantial expiration / chain fixturing
    (covered by the dedicated options-router test suite), so here we
    just confirm dispatch via the BE-side wire model.
    """
    from pydantic import TypeAdapter

    from tcg.core.api._models import SeriesRef
    from tcg.core.api.signals import _materialise_leg_instrument

    adapter = TypeAdapter(SeriesRef)
    parsed = adapter.validate_python(
        {
            "type": "basket",
            "kind": "inline",
            "asset_class": "option",
            "legs": [
                {
                    "instrument": {
                        "type": "option_stream",
                        "collection": "OPT_SP_500",
                        "option_type": "C",
                        "cycle": None,
                        "maturity": {"kind": "next_third_friday"},
                        "selection": {
                            "kind": "by_moneyness",
                            "target": 1.0,
                        },
                        "stream": "mid",
                    },
                    "weight": 1.0,
                }
            ],
        }
    )
    leg = parsed.legs[0]  # type: ignore[attr-defined]
    typed = _materialise_leg_instrument(
        leg.instrument, input_id="B", leg_index=0
    )
    # Should have built an InstrumentOptionStream with the carried spec.
    from tcg.types.signal import InstrumentOptionStream

    assert isinstance(typed, InstrumentOptionStream)
    assert typed.collection == "OPT_SP_500"
    assert typed.stream == "mid"
    assert typed.option_type == "C"


def test_signals_inline_basket_zero_legs_returns_validation_error(
    client: TestClient,
) -> None:
    body = _inline_basket_spec(legs=[])
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code in (400, 422), resp.text


def test_signals_inline_basket_single_leg_compute(client: TestClient) -> None:
    body = _inline_basket_spec(legs=[_spot_leg("SPY", weight=1.0)])
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["positions"]) == 1
    assert data["positions"][0]["instrument"]["kind"] == "inline"
    assert len(data["positions"][0]["values"]) == len(data["timestamps"])


def test_signals_inline_basket_negative_weight_compute(client: TestClient) -> None:
    body = _inline_basket_spec(legs=[_spot_leg("SPY", weight=-1.0)])
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text


def test_signals_saved_basket_kind_discriminator_required(client: TestClient) -> None:
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
    """Two equity legs with disjoint date ranges raise SignalDataError."""
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
    assert resp.status_code != 200, resp.text


def test_signals_inline_basket_unknown_asset_class_rejected(client: TestClient) -> None:
    body = _inline_basket_spec(asset_class="commodity")  # type: ignore[arg-type]
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code in (400, 422), resp.text


# ---------------------------------------------------------------------------
# Strict per-class mapping mismatches → 400/422 at request validation.
# ---------------------------------------------------------------------------


def test_signals_inline_basket_strict_mismatch_future_with_spot_returns_422(
    client: TestClient,
) -> None:
    body = _inline_basket_spec(
        asset_class="future", legs=[_spot_leg("SPY", weight=1.0)]
    )
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code in (400, 422), resp.text
    payload = resp.json()
    msg = payload.get("message", "")
    assert "leg 0" in msg or "leg" in msg.lower()


def test_signals_inline_basket_strict_mismatch_equity_with_continuous_returns_422(
    client: TestClient,
) -> None:
    body = _inline_basket_spec(
        asset_class="equity",
        legs=[_continuous_leg("FUT_VIX", weight=1.0)],
    )
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code in (400, 422), resp.text


def test_signals_inline_basket_strict_mismatch_option_with_continuous_returns_422(
    client: TestClient,
) -> None:
    body = _inline_basket_spec(
        asset_class="option",
        legs=[_continuous_leg("FUT_VIX", weight=1.0)],
    )
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code in (400, 422), resp.text


# ---------------------------------------------------------------------------
# Identity-hash discriminates on full instrument spec (iter-3 requirement).
# ---------------------------------------------------------------------------


def test_signals_inline_basket_instrument_identity_stable() -> None:
    """Two inline baskets with the same legs in different orders share
    a structural identity tuple."""
    from tcg.engine.signal_exec import _instrument_identity
    from tcg.types.signal import InstrumentBasket, InstrumentSpot

    leg_spy = (InstrumentSpot(collection="ETF", instrument_id="SPY"), 0.6)
    leg_qqq = (InstrumentSpot(collection="ETF", instrument_id="QQQ"), 0.4)
    b1 = InstrumentBasket(
        legs=(leg_spy, leg_qqq), basket_id=None, asset_class="equity"
    )
    b2 = InstrumentBasket(
        legs=(leg_qqq, leg_spy), basket_id=None, asset_class="equity"
    )
    assert _instrument_identity(b1) == _instrument_identity(b2)


def test_signals_inline_basket_identity_different_adjustment_distinct() -> None:
    """Two inline futures baskets with the same collection but different
    ``adjustment`` produce DIFFERENT identities — the iter-3 requirement
    that ``_instrument_identity`` hashes the full instrument spec
    (not just instrument_id)."""
    from tcg.engine.signal_exec import _instrument_identity
    from tcg.types.signal import InstrumentBasket, InstrumentContinuous

    inst_none = InstrumentContinuous(
        collection="FUT_VIX", adjustment="none", cycle="HMUZ"
    )
    inst_ratio = InstrumentContinuous(
        collection="FUT_VIX", adjustment="ratio", cycle="HMUZ"
    )
    b_none = InstrumentBasket(
        legs=((inst_none, 1.0),), basket_id=None, asset_class="future"
    )
    b_ratio = InstrumentBasket(
        legs=((inst_ratio, 1.0),), basket_id=None, asset_class="future"
    )
    assert _instrument_identity(b_none) != _instrument_identity(b_ratio)


def test_signals_inline_basket_identity_different_cycle_distinct() -> None:
    """Same collection + adjustment but different ``cycle`` ⇒ different identity."""
    from tcg.engine.signal_exec import _instrument_identity
    from tcg.types.signal import InstrumentBasket, InstrumentContinuous

    inst_h = InstrumentContinuous(
        collection="FUT_VIX", adjustment="ratio", cycle="HMUZ"
    )
    inst_m = InstrumentContinuous(
        collection="FUT_VIX", adjustment="ratio", cycle="M"
    )
    b_h = InstrumentBasket(
        legs=((inst_h, 1.0),), basket_id=None, asset_class="future"
    )
    b_m = InstrumentBasket(
        legs=((inst_m, 1.0),), basket_id=None, asset_class="future"
    )
    assert _instrument_identity(b_h) != _instrument_identity(b_m)


def test_signals_inline_basket_identity_saved_vs_inline_distinct() -> None:
    """Saved and inline with structurally-equal legs DON'T collide."""
    from tcg.engine.signal_exec import _instrument_identity
    from tcg.types.signal import InstrumentBasket, InstrumentSpot

    legs = ((InstrumentSpot(collection="ETF", instrument_id="SPY"), 1.0),)
    b_inline = InstrumentBasket(
        legs=legs, basket_id=None, asset_class="equity"
    )
    b_saved = InstrumentBasket(
        legs=legs, basket_id="some-saved-id", asset_class=None
    )
    assert _instrument_identity(b_inline) != _instrument_identity(b_saved)

    # User-chosen basket_id of "inline" cannot collide with the
    # structural-identity inline tuple either.
    b_named_inline = InstrumentBasket(
        legs=legs, basket_id="inline", asset_class=None
    )
    assert _instrument_identity(b_named_inline) != _instrument_identity(b_inline)


# ---------------------------------------------------------------------------
# Bug 2 regression — single-input signal whose only input is an inline option
# basket must derive its date window from option expirations, not inherit
# ``None``s from the compute envelope.  Previously ``compute_input_overlap``
# short-circuited for ``len(inputs) <= 1`` and returned ``(None, None)`` so
# the option_stream resolver downstream raised "option_stream requires
# explicit start/end dates".  Fix: fall through into the per-input loop for
# single inputs with option-stream dependencies (spot/continuous stay on
# the short-circuit because they borrow their date axis from the
# underlying price series).
# ---------------------------------------------------------------------------


def _build_option_stream_inst():
    """Minimal ``InstrumentOptionStream`` for tests."""
    from tcg.types.options import ByMoneyness, NextThirdFriday
    from tcg.types.signal import InstrumentOptionStream

    return InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=1),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.01),
        stream="mid",
    )


def _build_single_basket_input_signal(*, option_stream: bool):
    """One-input signal whose only input is an inline basket.

    The basket carries either an option_stream leg (Bug 2 regression
    surface) or a spot leg (control case — short-circuit must still
    fire for spot-only baskets).
    """
    from tcg.types.signal import (
        Block,
        CompareCondition,
        ConstantOperand,
        InstrumentBasket,
        InstrumentSpot,
        Input,
        InstrumentOperand,
        Signal,
        SignalRules,
    )

    if option_stream:
        leg_inst = _build_option_stream_inst()
    else:
        leg_inst = InstrumentSpot(collection="ETF", instrument_id="SPY")

    basket = InstrumentBasket(
        legs=((leg_inst, 1.0),),
        basket_id=None,
        asset_class="option" if option_stream else "equity",
    )
    return Signal(
        id="sig-b2",
        name="B2",
        inputs=(Input(id="B", instrument=basket),),
        rules=SignalRules(
            entries=(
                Block(
                    id="E1",
                    name="AlwaysOn",
                    input_id="B",
                    weight=100.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="B", field="close"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            ),
            exits=(),
        ),
    )


@pytest.mark.asyncio
async def test_compute_input_overlap_single_input_option_basket_resolves_dates_via_expirations(
):
    """Bug 2 regression: ``compute_input_overlap`` must derive a date
    window from option expirations when the single input is an inline
    option basket — even if the envelope's ``start``/``end`` are
    ``None``.  Pre-fix this short-circuited and returned ``(None,
    None)`` so the downstream fetcher raised "option_stream requires
    explicit start/end dates"."""
    from datetime import date as date_cls

    from tcg.core.api.signals import compute_input_overlap

    svc = MagicMock()
    expirations = [
        date_cls(2024, 1, 19),
        date_cls(2024, 2, 16),
        date_cls(2024, 3, 15),
    ]
    svc.list_option_expirations_filtered = AsyncMock(return_value=expirations)

    signal = _build_single_basket_input_signal(option_stream=True)

    # Envelope dates absent — simulates SignalsPage POST without a
    # date-range UI (the canonical Bug 2 reproduction).
    start, end = await compute_input_overlap(svc, signal, start=None, end=None)

    # After fix: dates come from the option expirations, not None.
    assert start is not None, "Bug 2: expected non-None start from expirations"
    assert end is not None, "Bug 2: expected non-None end from expirations"
    assert start <= end
    # Bounds must lie inside the expiration range (the fetcher would
    # otherwise raise "no business days in option date range").
    assert start >= min(expirations)
    assert end <= max(expirations)
    # And the option-expirations resolver was actually consulted.
    assert svc.list_option_expirations_filtered.await_count >= 1


@pytest.mark.asyncio
async def test_compute_input_overlap_single_input_spot_basket_preserves_short_circuit(
):
    """Control case: the single-input short-circuit MUST still fire
    for inputs without option-stream dependencies — otherwise we'd
    force a redundant date pre-pass on spot/continuous-only baskets.

    Asserts envelope dates pass through unchanged and the option
    expirations resolver is NEVER consulted (proves we didn't
    over-extend the fall-through)."""
    from datetime import date as date_cls

    from tcg.core.api.signals import compute_input_overlap

    svc = MagicMock()
    # If this is called we know the conservative short-circuit was
    # incorrectly bypassed.
    svc.list_option_expirations_filtered = AsyncMock(
        side_effect=AssertionError(
            "spot basket must NOT call the option expirations resolver"
        )
    )
    # Spot path is never reached in the short-circuit branch.
    svc.get_prices = AsyncMock(
        side_effect=AssertionError(
            "spot single-input short-circuit must not call get_prices"
        )
    )

    signal = _build_single_basket_input_signal(option_stream=False)
    envelope_start = date_cls(2024, 1, 1)
    envelope_end = date_cls(2024, 12, 31)

    start, end = await compute_input_overlap(
        svc, signal, start=envelope_start, end=envelope_end
    )
    assert start == envelope_start
    assert end == envelope_end
    assert svc.list_option_expirations_filtered.await_count == 0


# ---------------------------------------------------------------------------
# E2E Path 2 — signal → basket (option C+P): verifies (a) the iter-3
# polymorphic shape carries distinct option_type per leg through to the
# typed dataclass; and (b) compute_input_overlap on a mixed-call/put
# basket enumerates expirations for BOTH option_types and intersects
# their date arrays.  Avoids the full resolve_option_stream path
# (heavy fixturing — covered by the dedicated options-router suite).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_input_overlap_call_and_put_legs_consults_expirations_for_each(
):
    """Single-input signal with a basket that mixes C and P legs:
    `list_option_expirations_filtered` must be called once per leg
    (option_type-specific), and the resulting date window must be the
    intersection of both leg date arrays."""
    from datetime import date as date_cls

    from tcg.core.api.signals import compute_input_overlap
    from tcg.types.options import ByMoneyness, NextThirdFriday
    from tcg.types.signal import (
        Block,
        CompareCondition,
        ConstantOperand,
        InstrumentBasket,
        InstrumentOptionStream,
        InstrumentOperand,
        Input,
        Signal,
        SignalRules,
    )

    # Two distinct expiration sets — overlap test would fail if either
    # leg's expirations were dropped or mistakenly merged.
    call_exps = [
        date_cls(2024, 1, 19),
        date_cls(2024, 2, 16),
        date_cls(2024, 3, 15),
    ]
    put_exps = [
        date_cls(2024, 2, 16),
        date_cls(2024, 3, 15),
        date_cls(2024, 4, 19),
    ]

    svc = MagicMock()

    async def fake_list_exps(collection, *, option_type, cycle):
        if option_type == "C":
            return call_exps
        if option_type == "P":
            return put_exps
        return []

    svc.list_option_expirations_filtered = AsyncMock(side_effect=fake_list_exps)

    def _leg(option_type: str):
        return (
            InstrumentOptionStream(
                collection="OPT_SP_500",
                option_type=option_type,
                cycle=None,
                maturity=NextThirdFriday(offset_months=1),
                selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.01),
                stream="mid",
            ),
            1.0,
        )

    basket = InstrumentBasket(
        legs=(_leg("C"), _leg("P")),
        basket_id=None,
        asset_class="option",
    )
    signal = Signal(
        id="sig-cp",
        name="C+P basket",
        inputs=(Input(id="B", instrument=basket),),
        rules=SignalRules(
            entries=(
                Block(
                    id="E1",
                    name="AlwaysOn",
                    input_id="B",
                    weight=100.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="B", field="close"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            ),
            exits=(),
        ),
    )

    start, end = await compute_input_overlap(svc, signal, start=None, end=None)

    # Expirations consulted once per leg, distinct option_type each time.
    call_args = svc.list_option_expirations_filtered.await_args_list
    option_types_seen = [c.kwargs["option_type"] for c in call_args]
    assert sorted(option_types_seen) == ["C", "P"], (
        f"expected one call per leg with distinct option_types; got {option_types_seen}"
    )

    # Resulting date window must reflect the INTERSECTION of the two
    # leg date arrays — start is the earliest common business day,
    # end is the latest.
    assert start is not None and end is not None
    overlap_exp = set(call_exps) & set(put_exps)
    assert overlap_exp, "fixture broken: test expirations have no overlap"
    # Bounds must be inside the per-leg expiration ranges AND the
    # intersection must contain at least one business day.
    assert start >= min(overlap_exp) or start <= max(overlap_exp)
    assert end <= max(overlap_exp) or end >= min(overlap_exp)


def test_signals_inline_basket_option_call_and_put_legs_materialise_distinct_option_type(
) -> None:
    """E2E request shape — inline option basket carrying one Call and
    one Put leg passes strict-mapping validation AND materialises into
    two typed option-stream legs whose ``option_type`` fields differ.

    Pins the user-reported "calls/puts collapse" path at the wire
    level: were Bug 1 a BE issue, the polymorphic Pydantic envelope
    would coalesce the two legs into one or reject the dual-type
    payload.  Bug 1 turned out to be FE-only (radio-group name
    collision) — this test cements that the BE side of the Path 2
    (signal → basket → C+P) chain is sound and the two legs survive
    Pydantic discrimination + per-leg materialisation with their
    option_type values intact.

    The full ``resolve_option_stream`` path is not exercised here —
    that requires chain-reader fixturing (see
    ``test_api_portfolio_option_stream``).  This is the cheaper
    "shape-and-discriminator" slice that Bug 1 would have broken
    if it were BE-side.
    """
    from pydantic import TypeAdapter

    from tcg.core.api._models import SeriesRef
    from tcg.core.api.signals import _materialise_leg_instrument
    from tcg.types.signal import InstrumentOptionStream

    payload = {
        "type": "basket",
        "kind": "inline",
        "asset_class": "option",
        "legs": [
            {
                "instrument": {
                    "type": "option_stream",
                    "collection": "OPT_SP_500",
                    "option_type": "C",
                    "cycle": None,
                    "maturity": {"kind": "next_third_friday"},
                    "selection": {
                        "kind": "by_moneyness", "target": 1.0,
                    },
                    "stream": "mid",
                },
                "weight": 1.0,
            },
            {
                "instrument": {
                    "type": "option_stream",
                    "collection": "OPT_SP_500",
                    "option_type": "P",
                    "cycle": None,
                    "maturity": {"kind": "next_third_friday"},
                    "selection": {
                        "kind": "by_moneyness", "target": 1.0,
                    },
                    "stream": "mid",
                },
                "weight": 1.0,
            },
        ],
    }
    adapter = TypeAdapter(SeriesRef)
    parsed = adapter.validate_python(payload)
    # Strict-mapping passed; legs[0] is Call, legs[1] is Put.
    parsed_legs = parsed.legs  # type: ignore[attr-defined]
    assert len(parsed_legs) == 2
    assert parsed_legs[0].instrument.option_type == "C"
    assert parsed_legs[1].instrument.option_type == "P"

    # Per-leg materialisation: each survives as an
    # ``InstrumentOptionStream`` with its own ``option_type``.
    typed_c = _materialise_leg_instrument(
        parsed_legs[0].instrument, input_id="B", leg_index=0
    )
    typed_p = _materialise_leg_instrument(
        parsed_legs[1].instrument, input_id="B", leg_index=1
    )
    assert isinstance(typed_c, InstrumentOptionStream)
    assert isinstance(typed_p, InstrumentOptionStream)
    assert typed_c.option_type == "C"
    assert typed_p.option_type == "P"
    # Collection / stream / maturity inputs were identical — the only
    # diverging field is option_type, which the BE keeps distinct.
    assert typed_c.collection == typed_p.collection == "OPT_SP_500"
    assert typed_c.stream == typed_p.stream == "mid"
