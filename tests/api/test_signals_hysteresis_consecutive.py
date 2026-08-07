"""API parser + validation + evaluate round-trip for the Phase-2 primitives.

Covers the new optional wire fields:
  * ``HysteresisCondition`` (op == "hysteresis"): operand/enter/exit operands +
    ``direction`` — parse threading, evaluate through the real engine path, and
    the HTTP-400 rejections (bad/absent direction, missing threshold operand).
  * ``CompareCondition.consecutive_days``: default, threading, evaluate, and the
    HTTP-400 rejections (non-int / < 1 / null).

Asserts the LOCKED error-message contract (mirrors the existing cross-count /
reset rejections). DB-free: the evaluate path uses an in-memory fetcher —
``parse_signal`` (real API parser) + ``evaluate_signal`` (real engine).
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.core.api.signals import SignalIn, SignalValidationError, parse_signal
from tcg.engine.signal_exec import evaluate_signal

SPX_INPUT = {
    "id": "X",
    "instrument": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"},
}


def _signal(entries=None, exits=None, resets=None) -> SignalIn:
    return SignalIn.model_validate(
        {
            "id": "sig",
            "name": "",
            "inputs": [SPX_INPUT],
            "rules": {
                "entries": entries or [],
                "exits": exits or [],
                "resets": resets or [],
            },
        }
    )


def _entry(conditions, *, bid="E", name="Entry") -> dict:
    return {
        "id": bid,
        "name": name,
        "input_id": "X",
        "weight": 100.0,
        "conditions": conditions,
    }


def _hyst(enter, exit_, direction, *, drop=None) -> dict:
    c = {
        "op": "hysteresis",
        "operand": {"kind": "instrument", "input_id": "X"},
        "enter": {"kind": "constant", "value": enter},
        "exit": {"kind": "constant", "value": exit_},
        "direction": direction,
    }
    if drop:
        c.pop(drop, None)
    return c


def _cmp(op, level, *, consecutive_days=None) -> dict:
    c = {
        "op": op,
        "lhs": {"kind": "instrument", "input_id": "X"},
        "rhs": {"kind": "constant", "value": level},
    }
    if consecutive_days is not None:
        c["consecutive_days"] = consecutive_days
    return c


def _fetcher(prices, dates):
    async def fetch(instrument, field):
        return dates, np.asarray(prices, dtype=np.float64)

    return fetch


# --------------------------------------------------------------------------- #
# hysteresis — parse threading
# --------------------------------------------------------------------------- #


def test_hysteresis_threads_into_condition():
    sig = parse_signal(_signal(entries=[_entry([_hyst(95.0, 75.0, "up")])]))
    cond = sig.rules.entries[0].conditions[0]
    assert cond.op == "hysteresis"
    assert cond.direction == "up"
    assert cond.enter.value == 95.0
    assert cond.exit.value == 75.0


@pytest.mark.parametrize("bad", [None, "sideways", "UP", "", 1])
def test_hysteresis_bad_direction_rejected(bad):
    entry = _entry([_hyst(95.0, 75.0, "up")])
    entry["conditions"][0]["direction"] = bad
    with pytest.raises(SignalValidationError, match="direction must be 'up' or 'down'"):
        parse_signal(_signal(entries=[entry]))


def test_hysteresis_missing_enter_rejected():
    with pytest.raises(SignalValidationError, match="operand required"):
        parse_signal(_signal(entries=[_entry([_hyst(95.0, 75.0, "up", drop="enter")])]))


def test_hysteresis_missing_exit_rejected():
    with pytest.raises(SignalValidationError, match="operand required"):
        parse_signal(_signal(entries=[_entry([_hyst(95.0, 75.0, "up", drop="exit")])]))


@pytest.mark.asyncio
async def test_hysteresis_evaluates_through_engine():
    sig = parse_signal(_signal(entries=[_entry([_hyst(95.0, 75.0, "up")])]))
    prices = [70.0, 96.0, 90.0, 74.0, 80.0, 97.0, 76.0]
    dates = np.arange(20240101, 20240101 + 7, dtype=np.int64)
    res = await evaluate_signal(sig, {}, _fetcher(prices, dates))
    ev = {e.block_id: e for e in res.events}
    assert list(ev["E"].fired_indices) == [3]


# --------------------------------------------------------------------------- #
# consecutive_days — parse threading + rejections
# --------------------------------------------------------------------------- #


def test_consecutive_days_threads_into_condition():
    sig = parse_signal(_signal(entries=[_entry([_cmp("lt", 3.5, consecutive_days=2)])]))
    cond = sig.rules.entries[0].conditions[0]
    assert cond.consecutive_days == 2


def test_consecutive_days_defaults_to_one():
    sig = parse_signal(_signal(entries=[_entry([_cmp("lt", 3.5)])]))
    assert sig.rules.entries[0].conditions[0].consecutive_days == 1


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, None])
def test_consecutive_days_invalid_rejected(bad):
    # Set the key EXPLICITLY (incl. explicit null) so it reaches model_fields_set
    # and the parser's guard, rather than folding to the absent-default of 1.
    cond = _cmp("lt", 3.5)
    cond["consecutive_days"] = bad
    with pytest.raises(
        SignalValidationError, match="consecutive_days must be an integer >= 1"
    ):
        parse_signal(_signal(entries=[_entry([cond])]))


@pytest.mark.asyncio
async def test_consecutive_days_evaluates_through_engine():
    sig = parse_signal(_signal(entries=[_entry([_cmp("lt", 3.5, consecutive_days=2)])]))
    # prices 5 3 4 2 1 → truth F T F T T → 2-consecutive fires only at t=4.
    prices = [5.0, 3.0, 4.0, 2.0, 1.0]
    dates = np.arange(20240101, 20240101 + 5, dtype=np.int64)
    res = await evaluate_signal(sig, {}, _fetcher(prices, dates))
    ev = {e.block_id: e for e in res.events}
    assert list(ev["E"].fired_indices) == [4]
