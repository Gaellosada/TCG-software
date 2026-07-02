"""API wire-layer tests for the two default-off signal features.

Both features route malformed input through the uniform HTTP-400 envelope
(``SignalValidationError``) rather than a Pydantic 422 — mirroring the
existing ``count``/``window``/``links`` permissive-then-validated pattern.

Feature 1 — ``Input.position_cap`` on the wire (``_InputIn.position_cap``):
  valid pair threads to the typed ``Input``; omitted ⇒ None; malformed
  (not a pair / non-numeric / non-finite / low>high / bool) ⇒ 400.

Feature 2 — ``CrossCondition.count_mode`` on the wire (``_ConditionIn.count_mode``):
  valid value threads; omitted ⇒ "rolling"; invalid string ⇒ 400.
"""

from __future__ import annotations

import pytest

from tcg.core.api.signals import SignalIn, parse_signal
from tcg.engine.signal_exec import SignalValidationError

SPX_INPUT = {
    "id": "X",
    "instrument": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"},
}


def _spec_with_input(input_dict: dict) -> SignalIn:
    return SignalIn.model_validate(
        {
            "id": "sig",
            "name": "",
            "inputs": [input_dict],
            "rules": {"entries": [], "exits": [], "resets": []},
        }
    )


def _cross_entry_spec(cross_extra: dict) -> SignalIn:
    cond = {
        "op": "cross_above",
        "lhs": {"kind": "instrument", "input_id": "X"},
        "rhs": {"kind": "constant", "value": 100.0},
        **cross_extra,
    }
    return SignalIn.model_validate(
        {
            "id": "sig",
            "name": "",
            "inputs": [SPX_INPUT],
            "rules": {
                "entries": [
                    {
                        "id": "E",
                        "name": "Entry",
                        "input_id": "X",
                        "weight": 100.0,
                        "conditions": [cond],
                    }
                ],
                "exits": [],
                "resets": [],
            },
        }
    )


# --------------------------------------------------------------------------- #
# Feature 1 — position_cap wire parsing
# --------------------------------------------------------------------------- #


def test_position_cap_valid_pair_threads_to_input():
    sig = parse_signal(_spec_with_input({**SPX_INPUT, "position_cap": [0.0, 1.0]}))
    assert sig.inputs[0].position_cap == (0.0, 1.0)


def test_position_cap_omitted_is_none():
    sig = parse_signal(_spec_with_input(dict(SPX_INPUT)))
    assert sig.inputs[0].position_cap is None


def test_position_cap_negative_low_allowed_short_or_flat():
    sig = parse_signal(_spec_with_input({**SPX_INPUT, "position_cap": [-1.0, 0.0]}))
    assert sig.inputs[0].position_cap == (-1.0, 0.0)


@pytest.mark.parametrize(
    "bad",
    [
        [0.0],  # not a pair (too short)
        [0.0, 1.0, 2.0],  # too long
        [1.0, 0.0],  # low > high
        ["a", 1.0],  # non-numeric
        [True, 1.0],  # bool bound
        [float("inf"), 1.0],  # non-finite
        [float("nan"), 1.0],  # NaN
        "0,1",  # not a list
    ],
)
def test_position_cap_malformed_rejected(bad):
    with pytest.raises(SignalValidationError):
        parse_signal(_spec_with_input({**SPX_INPUT, "position_cap": bad}))


# --------------------------------------------------------------------------- #
# Feature 2 — count_mode wire parsing
# --------------------------------------------------------------------------- #


def test_count_mode_valid_since_reset_threads():
    sig = parse_signal(_cross_entry_spec({"count": 2, "count_mode": "since_reset"}))
    cond = sig.rules.entries[0].conditions[0]
    assert cond.count_mode == "since_reset"
    assert cond.count == 2


def test_count_mode_omitted_defaults_to_rolling():
    sig = parse_signal(_cross_entry_spec({"count": 2}))
    assert sig.rules.entries[0].conditions[0].count_mode == "rolling"


def test_count_mode_explicit_rolling():
    sig = parse_signal(_cross_entry_spec({"count_mode": "rolling"}))
    assert sig.rules.entries[0].conditions[0].count_mode == "rolling"


@pytest.mark.parametrize("bad", ["reset", "SINCE_RESET", "", 1, None, True])
def test_count_mode_invalid_rejected(bad):
    with pytest.raises(SignalValidationError):
        parse_signal(_cross_entry_spec({"count_mode": bad}))
