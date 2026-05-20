"""E2E Path 1 — Portfolio leg = signal whose input is an inline basket.

Verifies the wire-shape chain ``portfolio compute body → SignalLegSpec
→ SignalIn → BasketRefInline`` survives Pydantic validation when an
option basket carries one Call + one Put leg.

Audit finding (Wave-I, iter 4):
``tcg.core.api.portfolio._evaluate_signal_leg`` calls
``parse_signal(leg.signal_spec.spec)`` WITHOUT passing
``resolved_inputs=`` — basket inputs are never resolved through
``_resolve_basket_inputs``.  This means: at runtime today, a signal
leg in a portfolio whose input is an inline basket would fail at
``_parse_input`` because the function falls into the continuous
branch when given a ``BasketRefInline``.  This is a pre-existing
gap — out of scope for iter 4 per ORDERS (only the two named bugs)
and Sign 7 (Portfolio picker logic is locked).  Logged here so
future work can address it.

What this file pins TODAY:
- The polymorphic wire shape (iter-3) for an inline option basket
  with mixed Call + Put legs PASSES Pydantic strict-mapping
  validation when wrapped in a SignalLegSpec.
- Strict-mismatched baskets (asset_class option + a spot leg) are
  rejected at Pydantic validation, never reaching ``parse_signal``.

Sign 7 — does NOT touch ``frontend/src/pages/Portfolio/*`` picker
logic.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


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
    with pytest.raises(ValidationError) as exc_info:
        SignalLegSpec.model_validate(body)
    msg = str(exc_info.value).lower()
    assert "requires instrument.type=" in msg, (
        f"strict-mapping marker missing from rejection envelope: {msg}"
    )
