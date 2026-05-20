"""Hypothesis property test: strict per-class mapping on inline baskets.

Iter-3 rewrite.  Fuzzes ``(asset_class, leg.instrument.type)`` pairs and
asserts:

* Matching pair (per ``_ASSET_CLASS_TO_INSTRUMENT_TYPE``) → request
  validation succeeds (HTTP 200 or, on the compute path, a data-not-
  found error envelope for non-seeded ids — never a validation
  rejection).
* Mismatched pair → request validation rejects at Pydantic time with
  the standard 422/400 envelope and a message naming the leg index.
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


_ASSET_CLASS_TO_INSTRUMENT_TYPE = {
    "equity": "spot",
    "index": "spot",
    "future": "continuous",
    "option": "option_stream",
}

_ALL_ASSET_CLASSES = list(_ASSET_CLASS_TO_INSTRUMENT_TYPE.keys())
_ALL_INSTRUMENT_TYPES = ["spot", "continuous", "option_stream"]


# ---------------------------------------------------------------------------
# Test-double scaffolding.
# ---------------------------------------------------------------------------


class _BasketRepo:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}

    def seed(self, doc: Any) -> None:
        self._store[(doc.type, doc.id)] = doc

    async def get_by_id(self, doc_type: str, doc_id: str) -> Any:
        return self._store.get((doc_type, doc_id))


def _make_client() -> TestClient:
    svc = MagicMock()

    async def fake_get_prices(
        collection: str, instrument_id: str, *, start=None, end=None, provider=None
    ):
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)
    svc.get_continuous = AsyncMock(return_value=None)
    svc.list_option_expirations_filtered = AsyncMock(return_value=[])

    repo = _BasketRepo()
    now = datetime.now(timezone.utc)
    repo.seed(
        BasketDoc(
            id="basket-unused",
            type="basket",
            name="Unused",
            category=Category.RESEARCH,
            asset_class="equity",
            created_at=now,
            updated_at=now,
            legs=(),
        )
    )

    app = create_app()
    app.state.market_data = svc
    app.dependency_overrides[get_write_repository] = lambda: repo
    return TestClient(app)


def _leg_for_type(instrument_type: str) -> dict:
    if instrument_type == "spot":
        return {
            "instrument": {
                "type": "spot",
                "collection": "ETF",
                "instrument_id": "SPY",
            },
            "weight": 1.0,
        }
    if instrument_type == "continuous":
        return {
            "instrument": {
                "type": "continuous",
                "collection": "FUT_VIX",
                "adjustment": "ratio",
                "cycle": "HMUZ",
                "rollOffset": 0,
                "strategy": "front_month",
            },
            "weight": 1.0,
        }
    if instrument_type == "option_stream":
        return {
            "instrument": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "option_type": "C",
                "cycle": None,
                "maturity": {"kind": "next_third_friday"},
                "selection": {"kind": "by_moneyness", "target": 1.0},
                "stream": "mid",
            },
            "weight": 1.0,
        }
    raise AssertionError(f"unknown instrument_type {instrument_type!r}")


def _inline_body(asset_class: str, instrument_type: str) -> dict:
    return {
        "spec": {
            "id": "sig-strict-fuzz",
            "name": "Strict fuzz",
            "inputs": [
                {
                    "id": "B",
                    "instrument": {
                        "type": "basket",
                        "kind": "inline",
                        "asset_class": asset_class,
                        "legs": [_leg_for_type(instrument_type)],
                    },
                }
            ],
            "rules": {"entries": [], "exits": []},
        },
        "indicators": [],
        "instruments": {},
    }


@given(
    asset_class=st.sampled_from(_ALL_ASSET_CLASSES),
    instrument_type=st.sampled_from(_ALL_INSTRUMENT_TYPES),
)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_inline_basket_strict_per_class_mapping_property(
    asset_class: str, instrument_type: str
) -> None:
    """Every (asset_class, instrument_type) pair: matched → not rejected
    at request validation; mismatched → 400/422 with envelope."""
    expected = _ASSET_CLASS_TO_INSTRUMENT_TYPE[asset_class]
    client = _make_client()
    body = _inline_body(asset_class, instrument_type)
    resp = client.post("/api/signals/compute", json=body)

    if instrument_type == expected:
        # Matching pair: the request *might* fail downstream with a
        # data-not-found error (we stub get_prices/list_option_expirations
        # to None/[]) but it must NOT be rejected at request validation
        # time on the strict mapping.  Look for the strict-mapping-
        # specific marker phrase rather than just "leg 0" — the latter
        # also appears in legitimate data-error messages (e.g. iter-4
        # Bug 2 fix routes option_stream baskets through the BE date
        # resolver, which raises "basket leg 0: no option expirations
        # found" when the test stub returns []).  That's a downstream
        # data error, not a strict-mapping rejection.
        if resp.status_code in (400, 422):
            payload = resp.json()
            msg = payload.get("message", "").lower()
            assert "requires instrument.type=" not in msg, (
                f"matched pair ({asset_class!r}, {instrument_type!r}) "
                f"rejected with strict-mapping mismatch message: {payload}"
            )
        # Other status codes (e.g. 200, 500) are fine — the property
        # only cares about strict-mapping validation behaviour.
    else:
        # Mismatched pair: must surface as validation rejection with the
        # strict-mapping marker phrase.
        assert resp.status_code in (400, 422), (
            f"mismatched pair ({asset_class!r}, {instrument_type!r}) "
            f"NOT rejected at request validation; got "
            f"{resp.status_code}: {resp.text}"
        )
        payload = resp.json()
        msg = payload.get("message", "").lower()
        assert "requires instrument.type=" in msg, (
            f"mismatch envelope missing strict-mapping marker: {payload}"
        )
