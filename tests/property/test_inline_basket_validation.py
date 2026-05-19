"""Hypothesis property test: inline-basket asset_class/instrument_id mismatch.

For an inline ``{type:"basket", kind:"inline"}`` signal input, the BE
validates that each leg's ``instrument_id`` can be bucketed under the
declared ``asset_class`` (Wave-P Q1).  This file fuzzes the rejection
surface: for each (asset_class, wrong-prefix instrument_id) pair drawn
from curated pools, POST ``/api/signals/compute`` and assert HTTP 400
with the validation envelope and a message that mentions
``asset_class`` (or ``mismatch``).

Note on ``index``: the resolver always returns the single ``INDEX``
collection regardless of the leg's id prefix, so ``index`` legs cannot
trigger this mismatch path.  The strategy only generates
``future`` / ``option`` / ``equity`` classes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.app import create_app
from tcg.types.persistence import BasketDoc, Category


# ---------------------------------------------------------------------------
# Curated id pools.  Per-class pools list ids that DO belong to that
# class; mismatch pools list ids that DO NOT bucket under the class.
# ---------------------------------------------------------------------------

# Ids that the BE's prefix-bucketer rejects for ``asset_class="future"``
# (anything not starting with ``FUT_``).
_NON_FUTURE_IDS = [
    "SPY",
    "QQQ",
    "OPT_SPX_C500_20260101",
    "INDEX_SPX",
    "BARE",
    "AAPL",
]

# Ids that the BE's prefix-bucketer rejects for ``asset_class="option"``
# (anything not starting with ``OPT_``).
_NON_OPTION_IDS = [
    "SPY",
    "ES_MAR26",  # missing FUT_ prefix and not OPT_
    "FUT_ES_MAR26",  # starts with FUT_, not OPT_
    "INDEX_SPX",
    "AAPL",
]

# Ids that the fake MarketDataService probe will MISS for
# ``asset_class="equity"`` (anything not in the seeded equity pool).
# Includes ids whose prefix would mark them as future/option/index in
# other classes, plus pure unknowns.
_NON_EQUITY_IDS = [
    "FUT_ES_MAR26",
    "OPT_SPX_C500_20260101",
    "UNKNOWN_TICKER_XYZ",
    "ZZZ",
]


_MISMATCH_PAIRS = st.one_of(
    st.tuples(st.just("future"), st.sampled_from(_NON_FUTURE_IDS)),
    st.tuples(st.just("option"), st.sampled_from(_NON_OPTION_IDS)),
    st.tuples(st.just("equity"), st.sampled_from(_NON_EQUITY_IDS)),
)


# ---------------------------------------------------------------------------
# Test-double scaffolding (mirrors test_signals_basket_compute.py).
# ---------------------------------------------------------------------------


class _BasketRepo:
    """Minimal stand-in for ``WriteRepository`` — never reached on the
    error path, but ``_resolve_basket_inputs`` still requires the dep."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}

    def seed(self, doc: Any) -> None:
        self._store[(doc.type, doc.id)] = doc

    async def get_by_id(self, doc_type: str, doc_id: str) -> Any:
        return self._store.get((doc_type, doc_id))


def _make_client() -> TestClient:
    """Wire a TestClient with a MarketDataService that ONLY knows the
    two seeded equity tickers ``SPY``/``QQQ`` — every other id misses
    the probe, which guarantees the equity-mismatch path triggers.
    """
    svc = MagicMock()

    async def fake_get_prices(
        collection: str, instrument_id: str, *, start=None, end=None, provider=None
    ):
        # The mismatch tests never expect a hit; returning None for
        # everything keeps the probe deterministic.
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)

    repo = _BasketRepo()
    now = datetime.now(timezone.utc)
    repo.seed(
        BasketDoc(
            id="basket-unused",
            type="basket",
            name="Unused",
            category=Category.RESEARCH,
            created_at=now,
            updated_at=now,
            legs=(
                {"instrument_id": "SPY", "collection": "ETF", "weight": 1.0},
            ),
        )
    )

    app = create_app()
    app.state.market_data = svc
    app.dependency_overrides[get_write_repository] = lambda: repo
    return TestClient(app)


def _inline_body(asset_class: str, instrument_id: str) -> dict:
    return {
        "spec": {
            "id": "sig-mismatch-fuzz",
            "name": "Mismatch fuzz",
            "inputs": [
                {
                    "id": "B",
                    "instrument": {
                        "type": "basket",
                        "kind": "inline",
                        "asset_class": asset_class,
                        "legs": [
                            {"instrument_id": instrument_id, "weight": 1.0}
                        ],
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


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@given(pair=_MISMATCH_PAIRS)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_inline_basket_asset_class_id_mismatch_rejected(pair: tuple[str, str]) -> None:
    """Every mismatched (asset_class, instrument_id) pair must return
    HTTP 400 with the validation envelope and a message mentioning
    ``asset_class`` or ``mismatch``."""
    asset_class, instrument_id = pair
    client = _make_client()
    body = _inline_body(asset_class, instrument_id)
    resp = client.post("/api/signals/compute", json=body)
    assert resp.status_code == 400, (
        f"expected 400 for ({asset_class!r}, {instrument_id!r}); "
        f"got {resp.status_code}: {resp.text}"
    )
    payload = resp.json()
    assert payload.get("error_type") == "validation", payload
    msg = payload.get("message", "").lower()
    assert "asset_class" in msg or "mismatch" in msg, payload
