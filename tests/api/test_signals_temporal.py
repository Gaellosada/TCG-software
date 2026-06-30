"""API parser + validation tests for block temporal composition.

Covers the new optional wire fields:
  * ``CrossCondition`` ``count``/``window`` (cross_count) — defaults, threading,
    and HTTP-400 rejections;
  * block-level ``links`` (temporal chain) — threading onto ``Block.links``,
    reset-block rejection, and the bounded-state validation (finite positive
    windows, single contiguous forward chain, no out-of-range index).

These assert the LOCKED error-message contract (mirroring the existing
reset/input_id/weight rejections) and verify typed fields via ``parse_signal``.
"""

from __future__ import annotations

import pytest

from tcg.core.api.signals import SignalIn, SignalValidationError, parse_signal

SPX_INPUT = {
    "id": "X",
    "instrument": {"type": "spot", "collection": "INDEX", "instrument_id": "SPX"},
}


def _cross(level: float, *, count=None, window=None) -> dict:
    c: dict = {
        "op": "cross_above",
        "lhs": {"kind": "instrument", "input_id": "X"},
        "rhs": {"kind": "constant", "value": level},
    }
    if count is not None:
        c["count"] = count
    if window is not None:
        c["window"] = window
    return c


def _entry_block(conditions, *, links=None, bid="E", name="Entry") -> dict:
    blk: dict = {
        "id": bid,
        "name": name,
        "input_id": "X",
        "weight": 100.0,
        "conditions": conditions,
    }
    if links is not None:
        blk["links"] = links
    return blk


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


# --------------------------------------------------------------------------- #
# cross_count count/window
# --------------------------------------------------------------------------- #


def test_cross_count_threads_into_condition():
    sig = parse_signal(
        _signal(entries=[_entry_block([_cross(100.0, count=3, window=20)])])
    )
    cond = sig.rules.entries[0].conditions[0]
    assert cond.count == 3
    assert cond.window == 20


def test_cross_count_defaults_when_omitted():
    sig = parse_signal(_signal(entries=[_entry_block([_cross(100.0)])]))
    cond = sig.rules.entries[0].conditions[0]
    assert cond.count == 1
    assert cond.window == 1


@pytest.mark.parametrize("count", [0, -1])
def test_cross_count_count_below_one_rejected(count):
    with pytest.raises(SignalValidationError, match="count must be an integer >= 1"):
        parse_signal(_signal(entries=[_entry_block([_cross(100.0, count=count)])]))


@pytest.mark.parametrize("window", [0, -3])
def test_cross_count_window_below_one_rejected(window):
    with pytest.raises(SignalValidationError, match="window must be an integer >= 1"):
        parse_signal(_signal(entries=[_entry_block([_cross(100.0, window=window)])]))


# --------------------------------------------------------------------------- #
# links — threading
# --------------------------------------------------------------------------- #


def test_links_thread_onto_entry_block():
    sig = parse_signal(
        _signal(entries=[_entry_block([_cross(100.0), _cross(95.0)], links={"1": 5})])
    )
    assert sig.rules.entries[0].links == {1: 5}


def test_links_thread_onto_three_condition_chain():
    sig = parse_signal(
        _signal(
            entries=[
                _entry_block(
                    [_cross(100.0), _cross(95.0), _cross(90.0)], links={"1": 3, "2": 4}
                )
            ]
        )
    )
    assert sig.rules.entries[0].links == {1: 3, 2: 4}


def test_links_thread_onto_exit_block():
    entry = _entry_block([_cross(100.0)], bid="A", name="long")
    exit_blk = {
        "id": "X1",
        "name": "ex",
        "target_entry_block_names": ["long"],
        "conditions": [_cross(100.0), _cross(95.0)],
        "links": {"1": 3},
    }
    sig = parse_signal(_signal(entries=[entry], exits=[exit_blk]))
    assert sig.rules.exits[0].links == {1: 3}


def test_empty_and_absent_links_become_none():
    sig_absent = parse_signal(_signal(entries=[_entry_block([_cross(100.0)])]))
    assert sig_absent.rules.entries[0].links is None
    sig_empty = parse_signal(_signal(entries=[_entry_block([_cross(100.0)], links={})]))
    assert sig_empty.rules.entries[0].links is None


# --------------------------------------------------------------------------- #
# links — validation (HTTP 400 via SignalValidationError)
# --------------------------------------------------------------------------- #


def test_links_rejected_on_reset_block():
    reset = {
        "id": "R1",
        "name": "r",
        "conditions": [_cross(100.0), _cross(95.0)],
        "links": {"1": 5},
    }
    with pytest.raises(SignalValidationError, match="reset blocks must not set links"):
        parse_signal(_signal(resets=[reset]))


def test_links_window_zero_rejected():
    with pytest.raises(SignalValidationError, match="window for index 1 must be >= 1"):
        parse_signal(
            _signal(
                entries=[_entry_block([_cross(100.0), _cross(95.0)], links={"1": 0})]
            )
        )


def test_links_key_zero_rejected():
    # The head (index 0) carries no link.
    with pytest.raises(SignalValidationError, match="out of range"):
        parse_signal(
            _signal(
                entries=[_entry_block([_cross(100.0), _cross(95.0)], links={"0": 5})]
            )
        )


def test_links_key_out_of_range_rejected():
    with pytest.raises(SignalValidationError, match="out of range"):
        parse_signal(
            _signal(
                entries=[_entry_block([_cross(100.0), _cross(95.0)], links={"2": 5})]
            )
        )


def test_links_non_contiguous_chain_rejected():
    # 3 conditions, link only on index 2 (missing index 1) -> not contiguous.
    with pytest.raises(SignalValidationError, match="one contiguous forward chain"):
        parse_signal(
            _signal(
                entries=[
                    _entry_block(
                        [_cross(100.0), _cross(95.0), _cross(90.0)], links={"2": 4}
                    )
                ]
            )
        )


def test_links_partial_chain_rejected():
    # 3 conditions, link only on index 1 -> does not span the whole block.
    with pytest.raises(SignalValidationError, match="one contiguous forward chain"):
        parse_signal(
            _signal(
                entries=[
                    _entry_block(
                        [_cross(100.0), _cross(95.0), _cross(90.0)], links={"1": 3}
                    )
                ]
            )
        )


def test_parse_links_helper_defensive_checks():
    # ``_parse_links`` is the AUTHORITATIVE window validator: every malformed
    # window value is rejected here (bool subclasses int; None/str/float/
    # list/dict are not int) so the model layer can stay permissive and route
    # all rejections through the app's HTTP-400 envelope. No 500 ever.
    from tcg.core.api.signals import _parse_links

    for bad in [None, "abc", 1.5, True, False, [1], {"x": 1}]:
        with pytest.raises(SignalValidationError, match="must be an integer"):
            _parse_links({"1": bad}, 2, path="p")
    with pytest.raises(SignalValidationError, match="not an integer condition index"):
        _parse_links({"abc": 5}, 2, path="p")  # non-integer key
    # valid normalises str keys -> int keys
    assert _parse_links({"1": 5}, 2, path="p") == {1: 5}
    assert _parse_links(None, 2, path="p") is None
    assert _parse_links({}, 2, path="p") is None


# --------------------------------------------------------------------------- #
# links — malformed-window rejection over the HTTP boundary (MINOR-1)
#
# The block ``links`` field must route EVERY invalid window through the app's
# uniform HTTP-400 ``error_type='validation'`` envelope — the same envelope as
# window=0/-3, partial/non-contiguous chains, links-on-reset, and count<1.
# A null / non-int / float / bool window must NOT be intercepted by Pydantic as
# a 422, nor (for ``true``, which ``bool`` makes an ``int`` subclass) silently
# coerced to a window of 1 and accepted — both are nonsense and must 400.
# --------------------------------------------------------------------------- #


@pytest.fixture
def http_app():
    from unittest.mock import AsyncMock, MagicMock

    import numpy as np
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.signals import router as signals_router
    from tcg.types.errors import TCGError
    from tcg.types.market import PriceSeries

    dates = np.array(
        [20240102, 20240103, 20240104, 20240105, 20240108, 20240109], dtype=np.int64
    )
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
    series = PriceSeries(
        dates=dates,
        open=closes - 1.0,
        high=closes + 1.0,
        low=closes - 2.0,
        close=closes,
        volume=np.full(dates.shape[0], 1000.0, dtype=np.float64),
    )

    svc = MagicMock()

    async def fake_get_prices(collection, instrument_id, start=None, end=None):
        return series if instrument_id == "SPX" else None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(signals_router)
    app.state.market_data = svc
    app.state.app_db_repo = object()
    return app


@pytest.fixture
async def http_client(http_app):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=http_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _two_cond_links_body(window) -> dict:
    """A minimal valid 2-condition entry block whose single link
    ``{"1": window}`` carries the supplied (possibly malformed) window."""
    return {
        "spec": {
            "id": "minor1",
            "name": "",
            "inputs": [SPX_INPUT],
            "rules": {
                "entries": [
                    _entry_block([_cross(100.0), _cross(95.0)], links={"1": window})
                ],
                "exits": [],
            },
        },
        "indicators": [],
        "instruments": {},
    }


@pytest.mark.parametrize(
    "window",
    [None, "abc", 1.5, True],
    ids=["null", "string", "float", "bool_true"],
)
async def test_http_malformed_link_window_returns_400_validation(http_client, window):
    """POST /api/signals/compute with a malformed link window must return the
    uniform HTTP-400 validation envelope (NOT 422, NOT a silently-coerced 200)."""
    resp = await http_client.post(
        "/api/signals/compute", json=_two_cond_links_body(window)
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error_type"] == "validation"


async def test_http_malformed_link_window_never_500(http_client):
    """No malformed ``links`` window value (incl. container types) yields a
    500 — every one is caught as a 400 validation error, never an unhandled
    exception."""
    for window in [None, "abc", 1.5, True, False, [1], {"x": 1}, 0, -3]:
        resp = await http_client.post(
            "/api/signals/compute", json=_two_cond_links_body(window)
        )
        assert resp.status_code == 400, (
            f"window={window!r} -> {resp.status_code}: {resp.text}"
        )
        assert resp.json()["error_type"] == "validation"


# --------------------------------------------------------------------------- #
# count/window — malformed-value rejection over the HTTP boundary (MINOR-A/B)
#
# ``_ConditionIn.count`` and ``_ConditionIn.window`` must route EVERY invalid
# value through the uniform HTTP-400 ``error_type='validation'`` envelope.
# Before the ``Any`` widening (MINOR-A/B fix), ``count:1.5`` produced a 422
# (Pydantic int_from_float) and ``count:true`` was silently coerced to 1 and
# returned 200 (because ``bool`` is a subclass of ``int``).  Both are
# wrong; the isinstance guards in ``_parse_condition`` own validation.
# --------------------------------------------------------------------------- #


def _cond_with_count(count) -> dict:
    """A cross_above condition dict with ``count`` ALWAYS present (explicit null
    when count is None — unlike ``_cross()``, which omits absent fields)."""
    return {
        "op": "cross_above",
        "lhs": {"kind": "instrument", "input_id": "X"},
        "rhs": {"kind": "constant", "value": 100.0},
        "count": count,
    }


def _cond_with_window(window) -> dict:
    """A cross_above condition dict with ``window`` ALWAYS present (explicit null
    when window is None — unlike ``_cross()``, which omits absent fields)."""
    return {
        "op": "cross_above",
        "lhs": {"kind": "instrument", "input_id": "X"},
        "rhs": {"kind": "constant", "value": 100.0},
        "window": window,
    }


def _single_cond_count_body(count) -> dict:
    """Minimal valid 1-condition entry block with ``count`` always explicit in
    the JSON (including null) so ``model_fields_set`` marks it as supplied."""
    return {
        "spec": {
            "id": "minorAB",
            "name": "",
            "inputs": [SPX_INPUT],
            "rules": {
                "entries": [_entry_block([_cond_with_count(count)])],
                "exits": [],
            },
        },
        "indicators": [],
        "instruments": {},
    }


def _single_cond_window_body(window) -> dict:
    """Minimal valid 1-condition entry block with ``window`` always explicit in
    the JSON (including null) so ``model_fields_set`` marks it as supplied."""
    return {
        "spec": {
            "id": "minorAB",
            "name": "",
            "inputs": [SPX_INPUT],
            "rules": {
                "entries": [_entry_block([_cond_with_window(window)])],
                "exits": [],
            },
        },
        "indicators": [],
        "instruments": {},
    }


@pytest.mark.parametrize(
    "count",
    [None, 1.5, "x", True, False, [1], 0, -1],
    ids=["null", "float", "string", "bool_true", "bool_false", "list", "zero", "neg"],
)
async def test_http_malformed_count_returns_400_validation(http_client, count):
    """POST /api/signals/compute with a malformed cross ``count`` must return the
    uniform HTTP-400 validation envelope (NOT 422, NOT a silently-coerced 200)."""
    resp = await http_client.post(
        "/api/signals/compute", json=_single_cond_count_body(count)
    )
    assert resp.status_code == 400, (
        f"count={count!r} -> {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_type"] == "validation"


@pytest.mark.parametrize(
    "window",
    [None, 1.5, "x", True, False, [1], 0, -1],
    ids=["null", "float", "string", "bool_true", "bool_false", "list", "zero", "neg"],
)
async def test_http_malformed_window_returns_400_validation(http_client, window):
    """POST /api/signals/compute with a malformed cross ``window`` must return the
    uniform HTTP-400 validation envelope (NOT 422, NOT a silently-coerced 200)."""
    resp = await http_client.post(
        "/api/signals/compute", json=_single_cond_window_body(window)
    )
    assert resp.status_code == 400, (
        f"window={window!r} -> {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_type"] == "validation"


async def test_http_malformed_count_window_never_500(http_client):
    """No malformed ``count`` or ``window`` value yields a 500 — every one
    is caught as a 400 validation error, never an unhandled exception."""
    for bad in [None, "abc", 1.5, True, False, [1], {"x": 1}, 0, -1]:
        for body_fn in [_single_cond_count_body, _single_cond_window_body]:
            resp = await http_client.post("/api/signals/compute", json=body_fn(bad))
            assert resp.status_code == 400, (
                f"value={bad!r} -> {resp.status_code}: {resp.text}"
            )
            assert resp.json()["error_type"] == "validation"
