"""E2E Path 1 — Portfolio leg = signal whose input is an inline basket.

Verifies BOTH:

1. The wire-shape chain ``portfolio compute body → SignalLegSpec
   → SignalIn → BasketRefInline`` survives Pydantic validation when an
   option basket carries one Call + one Put leg.

2. The runtime path through ``_evaluate_signal_leg`` calls
   ``_resolve_basket_inputs`` BEFORE ``parse_signal`` (mirroring
   ``compute_signal``), so a portfolio signal leg whose input is an
   inline basket does NOT crash at ``_parse_input``'s continuous-branch
   fallback with ``AttributeError: 'BasketRefInline' object has no
   attribute 'collection'``.

Background — Wave I follow-up (iter 4):
Wave I logged a real bug at ``tcg.core.api.portfolio._evaluate_signal_leg``:
the original code called ``parse_signal(leg.signal_spec.spec)`` without
``resolved_inputs=``, so ``_parse_input`` fell through the basket branch
and crashed.  The fix mirrors ``compute_signal``'s pattern — call
``_resolve_basket_inputs`` first, thread the result via
``resolved_inputs=``.  The runtime path test below would fail with that
AttributeError before the fix; it now reaches the
``_parse_input``-resolved branch and surfaces the expected
``InstrumentBasket`` legs (which then proceed to the per-leg fetcher;
the test stops short of full materialisation to keep it independent of
the option-stream resolver infra).

Sign 7 — does NOT touch ``frontend/src/pages/Portfolio/*`` picker
logic (Sign 7 is FE-only).
Sign 9 — no Mongo probe; uses a stub repo + fake market data, matching
iter-3's BE pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from pydantic import ValidationError as PydanticValidationError

from tcg.types.market import PriceSeries


# ---------------------------------------------------------------------------
# Wire-shape pin (kept from iter-4 Wave I — pins the Pydantic envelope)
# ---------------------------------------------------------------------------


def _signal_spec_with_option_basket() -> dict:
    """SignalIn dict whose only input is an inline option basket
    with one Call leg and one Put leg."""
    return {
        "id": "sig-portfolio-cp",
        "name": "Portfolio E2E — Option Basket C+P",
        "inputs": [
            {
                "id": "B",
                "instrument": {
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
                },
            }
        ],
        "rules": {"entries": [], "exits": []},
    }


def test_signal_leg_spec_accepts_inline_option_basket_C_and_P_legs():
    """Path 1 wire-shape: a SignalLegSpec wrapping a SignalIn whose
    sole input is an inline option basket with Call + Put legs
    survives Pydantic validation; the two legs surface as distinct
    OptionStreamRef instances with distinct ``option_type``.
    """
    from tcg.core.api.portfolio import SignalLegSpec

    body = {
        "spec": _signal_spec_with_option_basket(),
        "indicators": [],
    }
    parsed = SignalLegSpec.model_validate(body)

    assert len(parsed.spec.inputs) == 1
    inp = parsed.spec.inputs[0]
    assert inp.id == "B"

    basket = inp.instrument
    # BasketRefInline carries `kind="inline"` + `asset_class`.
    assert getattr(basket, "kind", None) == "inline"
    assert getattr(basket, "asset_class", None) == "option"
    assert len(basket.legs) == 2

    types = [leg.instrument.type for leg in basket.legs]
    assert types == ["option_stream", "option_stream"], (
        f"Path 1 shape collapse: expected two option_stream legs, "
        f"got types={types!r}"
    )
    option_types = [leg.instrument.option_type for leg in basket.legs]
    assert sorted(option_types) == ["C", "P"], (
        f"option_type collapse on the Path 1 wire shape: "
        f"expected {{C, P}}, got {option_types!r}"
    )


def test_signal_leg_spec_rejects_strict_mismatched_inline_basket():
    """Path 1 negative: an inline option basket with a spot leg
    (strict-mismatch) MUST be rejected by Pydantic
    ``BasketRefInline._check_strict_per_class_mapping`` before
    ``parse_signal`` is reached."""
    from tcg.core.api.portfolio import SignalLegSpec

    spec = _signal_spec_with_option_basket()
    # Corrupt: replace the Call leg with a spot leg.
    spec["inputs"][0]["instrument"]["legs"][0] = {
        "instrument": {
            "type": "spot",
            "collection": "ETF",
            "instrument_id": "SPY",
        },
        "weight": 1.0,
    }
    body = {"spec": spec, "indicators": []}
    with pytest.raises(PydanticValidationError) as exc_info:
        SignalLegSpec.model_validate(body)
    msg = str(exc_info.value).lower()
    assert "requires instrument.type=" in msg, (
        f"strict-mapping marker missing from rejection envelope: {msg}"
    )


# ---------------------------------------------------------------------------
# Runtime-path: _evaluate_signal_leg must call _resolve_basket_inputs.
# ---------------------------------------------------------------------------
#
# Stub repo + fake market data — mirrors test_signals_basket_compute.py's
# iter-3 BE pattern. No Mongo probe (Sign 9 honoured).


_EQUITY_DATES = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108, 20240109],
    dtype=np.int64,
)
_SPY_CLOSES = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
_QQQ_CLOSES = np.array([200.0, 201.0, 200.0, 202.0, 203.0, 204.0])


def _equity_price_series(closes: np.ndarray) -> PriceSeries:
    n = closes.shape[0]
    return PriceSeries(
        dates=_EQUITY_DATES,
        open=closes - 1.0,
        high=closes + 1.0,
        low=closes - 2.0,
        close=closes,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


class _StubBasketRepo:
    """Inline-only baskets short-circuit before any repo read.

    The stub records every ``get_by_id`` call so the test can assert
    the saved-basket path was/wasn't consulted.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_by_id(self, doc_type: str, doc_id: str) -> Any:  # noqa: D401
        self.calls.append((doc_type, doc_id))
        return None


def _equity_inline_basket_signal_spec() -> dict:
    """SignalIn dict whose only input is an inline EQUITY basket with
    two spot legs.  Equity legs hit the existing ``svc.get_prices``
    resolver — no option-chain materialisation needed — so the test
    can drive the runtime path end-to-end without standing up the
    options infra.
    """
    return {
        "id": "sig-portfolio-equity-basket",
        "name": "Portfolio E2E — Equity Basket (runtime path)",
        "inputs": [
            {
                "id": "EQB",
                "instrument": {
                    "type": "basket",
                    "kind": "inline",
                    "asset_class": "equity",
                    "legs": [
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
                    ],
                },
            }
        ],
        "rules": {"entries": [], "exits": []},
    }


@pytest.fixture
def fake_market_data() -> MagicMock:
    svc = MagicMock()

    async def fake_get_prices(
        collection: str,
        instrument_id: str,
        *,
        start=None,
        end=None,
        provider=None,
    ):
        if instrument_id == "SPY":
            return _equity_price_series(_SPY_CLOSES)
        if instrument_id == "QQQ":
            return _equity_price_series(_QQQ_CLOSES)
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)
    return svc


async def test_evaluate_signal_leg_resolves_inline_basket_at_runtime(
    fake_market_data: MagicMock,
):
    """Runtime regression — ``_evaluate_signal_leg`` must call
    ``_resolve_basket_inputs`` BEFORE ``parse_signal``.

    Pre-fix (asserted below via :func:`parse_signal` directly): the
    raw ``BasketRefInline`` input falls through ``_parse_input``'s
    continuous-branch fallback and raises
    ``AttributeError: 'BasketRefInline' object has no attribute 'collection'``.

    Post-fix: ``_evaluate_signal_leg`` first dispatches inputs through
    ``_resolve_basket_inputs`` (mirroring ``compute_signal``), the
    basket is materialised into typed-leg snapshots, and evaluation
    proceeds without an AttributeError.  The signal has no rules so
    ``evaluate_signal`` legitimately returns an empty index — the
    point of this test is the absence of the AttributeError, NOT the
    shape of the eval result.
    """
    from tcg.core.api.portfolio import LegSpec, SignalLegSpec, _evaluate_signal_leg

    # --- pre-fix probe: bare ``parse_signal`` on the same payload MUST
    #     surface the AttributeError that the fix avoids ------------
    from tcg.core.api.signals import SignalIn, parse_signal

    spec_dict = _equity_inline_basket_signal_spec()
    bare_sig_in = SignalIn.model_validate(spec_dict)
    with pytest.raises(AttributeError) as exc_info:
        parse_signal(bare_sig_in)
    assert "BasketRefInline" in str(exc_info.value) or "collection" in str(
        exc_info.value
    ), (
        f"pre-fix probe expected AttributeError mentioning BasketRefInline "
        f"or collection — got {exc_info.value!r}"
    )

    # --- post-fix: _evaluate_signal_leg goes through _resolve_basket_inputs
    repo = _StubBasketRepo()
    leg = LegSpec(
        type="signal",
        signal_spec=SignalLegSpec.model_validate(
            {"spec": spec_dict, "indicators": []}
        ),
    )

    # No AttributeError — the call returns a well-formed
    # _SignalLegEvalResult dataclass (index + synthetic same shape).
    result = await _evaluate_signal_leg(
        "eqsig",
        leg,
        fake_market_data,
        start_date=None,
        end_date=None,
        repo=repo,
    )
    assert result.index.shape == result.synthetic.shape

    # Inline-only short-circuit (preserved from iter-1): no saved
    # basket → repo never consulted.
    assert repo.calls == [], (
        f"inline-only signal triggered an unexpected repo read: {repo.calls!r}"
    )
