"""Item F — reject a wrong-signed ``target_delta`` at the API model.

A ``ByDelta`` target whose SIGN contradicts the option type (e.g. ``+0.10`` on a
PUT) is a malformed spec: PUT premia carry non-positive delta, CALL premia
non-negative.  ``OptionStreamRef`` is the layer that owns BOTH ``option_type``
and the selection, so the rule is enforced there via
``reject_contradicting_delta_sign``.

This is a NO-OP for every correctly-signed production selection (put=-0.10,
call=+0.10, …) and rejects ONLY the degenerate contradicting shape — cleanly,
as a Pydantic ``ValidationError`` (surfaced by FastAPI as a 422), never a 500.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tcg.core.api._models import OptionStreamRef
from tcg.core.api._models_options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    reject_contradicting_delta_sign,
)


def _ref(option_type: str, selection: dict) -> OptionStreamRef:
    return OptionStreamRef.model_validate(
        {
            "type": "option_stream",
            "collection": "OPT_SP_500",
            "option_type": option_type,
            "cycle": None,
            "maturity": {"kind": "next_third_friday"},
            "selection": selection,
            "stream": "mid",
        }
    )


# ── The reusable helper (direct) ───────────────────────────────────────────


def test_helper_rejects_positive_delta_on_put():
    with pytest.raises(ValueError, match="PUT"):
        reject_contradicting_delta_sign("P", ByDelta(target_delta=0.10))


def test_helper_rejects_negative_delta_on_call():
    with pytest.raises(ValueError, match="CALL"):
        reject_contradicting_delta_sign("C", ByDelta(target_delta=-0.10))


def test_helper_allows_correctly_signed():
    # No raise for the correctly-signed production shapes.
    reject_contradicting_delta_sign("P", ByDelta(target_delta=-0.10))
    reject_contradicting_delta_sign("C", ByDelta(target_delta=0.10))


def test_helper_allows_zero_for_either_type():
    reject_contradicting_delta_sign("P", ByDelta(target_delta=0.0))
    reject_contradicting_delta_sign("C", ByDelta(target_delta=0.0))


def test_helper_noop_for_non_by_delta():
    # ByStrike / ByMoneyness carry no delta sign → never rejected.
    reject_contradicting_delta_sign("P", ByStrike(strike=4500.0))
    reject_contradicting_delta_sign("C", ByMoneyness(target_K_over_S=1.0))


# ── Enforced on the OptionStreamRef model (request ingress) ────────────────


def test_model_rejects_positive_delta_on_put():
    with pytest.raises(ValidationError, match="PUT"):
        _ref("P", {"kind": "by_delta", "target": 0.10})


def test_model_rejects_negative_delta_on_call():
    with pytest.raises(ValidationError, match="CALL"):
        _ref("C", {"kind": "by_delta", "target": -0.10})


def test_model_accepts_correctly_signed_put():
    ref = _ref("P", {"kind": "by_delta", "target": -0.10})
    assert ref.option_type == "P"
    assert isinstance(ref.selection, ByDelta)
    assert ref.selection.target_delta == -0.10


def test_model_accepts_correctly_signed_call():
    ref = _ref("C", {"kind": "by_delta", "target": 0.10})
    assert ref.selection.target_delta == 0.10


def test_model_accepts_zero_delta_on_put():
    ref = _ref("P", {"kind": "by_delta", "target": 0.0})
    assert ref.selection.target_delta == 0.0


def test_model_accepts_non_by_delta_selections():
    # A by_strike / by_moneyness leg is unaffected regardless of option_type.
    assert _ref("P", {"kind": "by_strike", "strike": 4500.0}).option_type == "P"
    assert _ref("C", {"kind": "by_moneyness", "target": 1.0}).option_type == "C"
