"""Portfolio ``option_stream`` HOLD leg — fixed-contract dollar-P&L (Part B).

Proves a portfolio hold-mode option PRICE leg (mid/bs_mid + ``hold_between_rolls``)
reproduces the validated S1 signal curve by:

  (a) SHARING the same accumulator the signal path uses
      (:func:`tcg.engine.hold_pnl._compound_with_hold`), and
  (b) applying DIRECTION exactly ONCE — the synthetic ``100·equity_ratio`` already
      bakes in ``sign(weight)`` and ``nav_times``; the aggregation then uses
      ``|weight|`` as the portfolio share (a signed weight would double-short).

All tests are PURE: the option resolver is replaced by a synthetic fetcher (the
SAME APR→MAY roll fixture the signal-side oracle test pins), so there is no
dwh/DB dependency.  The oracle ``_oracle_ratio`` is the dollar-NAV Java-faithful
short-put reference from
``tests/engine/options/test_signal_exec_option_hold_pnl.py`` (re-declared here — a
test module is not importable as a package).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError as PydanticValidationError

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.portfolio import (
    LegSpec,
    _evaluate_option_stream_leg,
)
from tcg.core.api.portfolio import router as portfolio_router
from tcg.engine import compute_weighted_portfolio
from tcg.types.errors import TCGError, ValidationError
from tcg.types.signal import InstrumentOptionStream

# Async tests auto-marked (asyncio_mode="auto").

# ── The SAME APR→MAY hold fixture + dollar-NAV oracle the signal test pins.
#   APR K4400 held mids: 30,28,26,24(roll-day OLD mid); MAY K4450: 18(open),20,19
#   values[t] = owner-of-step mid LEVEL (OLD on roll day) = [30,28,26,24,20,19]
#   is_roll = [1,0,0,1,0,0]; roll_premium = [30,·,·,18,·,·]
_DATES_INT = np.array(
    [20240327, 20240328, 20240329, 20240401, 20240402, 20240403], dtype=np.int64
)
_HELD_PREMIUM = np.array([30.0, 28.0, 26.0, 24.0, 20.0, 19.0])
_IS_ROLL = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
_ROLL_PREMIUM = np.array([30.0, np.nan, np.nan, 18.0, np.nan, np.nan])
_OWNER_PREV = np.array([np.nan, 30.0, 28.0, 26.0, 18.0, 20.0])
_OWNER_CUR = np.array([np.nan, 28.0, 26.0, 24.0, 20.0, 19.0])


def _oracle_ratio(
    owner_prev,
    owner_cur,
    is_roll,
    roll_premium,
    *,
    nav_times,
    weight,
    base_nav=1_000_000.0,
):
    """Dollar-NAV oracle → base-1 ratio (verbatim from the signal-side test)."""
    T = len(owner_cur)
    nav = np.empty(T, dtype=np.float64)
    nav[0] = base_nav
    sign = 1.0 if weight > 0 else -1.0
    qty = nav_times * base_nav / roll_premium[0]
    for t in range(1, T):
        dprem = owner_cur[t] - owner_prev[t]
        if not np.isfinite(dprem):
            dprem = 0.0
        nav[t] = nav[t - 1] + sign * qty * dprem
        if bool(is_roll[t]):
            qty = nav_times * nav[t] / roll_premium[t]
    return nav / nav[0]


def _fake_make_signal_fetcher(svc, start, end):
    """Synthetic fetcher over the APR→MAY fixture (dwh-free), matching the real
    ``make_signal_fetcher`` shape: a callable + ``.fetch_hold_roll_info``."""

    async def fetch(instrument, field):
        assert isinstance(instrument, InstrumentOptionStream)
        assert instrument.hold_between_rolls is True
        return _DATES_INT, _HELD_PREMIUM.copy()

    async def fetch_hold_roll_info(instrument):
        assert isinstance(instrument, InstrumentOptionStream)
        return _DATES_INT, _IS_ROLL.copy(), _ROLL_PREMIUM.copy()

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
    return fetch


def _hold_put_leg(*, stream: str = "bs_mid", nav_times: float = 1.0) -> dict:
    return {
        "type": "option_stream",
        "collection": "OPT_SP_500",
        "option_type": "P",
        "cycle": None,
        "maturity": {"kind": "end_of_month", "offset_months": 0},
        "selection": {"kind": "by_delta", "target": -0.10, "tolerance": 0.20},
        "stream": stream,
        "hold_between_rolls": True,
        "nav_times": nav_times,
    }


# ── Fixtures: FastAPI app with the resolver replaced by the synthetic fetcher ──


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(
        "tcg.core.api.portfolio.make_signal_fetcher", _fake_make_signal_fetcher
    )
    svc = MagicMock()
    application = FastAPI()
    application.add_exception_handler(TCGError, tcg_error_handler)
    application.include_router(portfolio_router)
    application.state.market_data = svc
    application.state.app_db_repo = object()  # resolved but never invoked here
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _compute(client, weight, *, nav_times=1.0, stream="bs_mid") -> dict:
    body = {
        "legs": {"P": _hold_put_leg(stream=stream, nav_times=nav_times)},
        "weights": {"P": weight},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-03-01",
        "end": "2024-04-30",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── THE acceptance oracle ──────────────────────────────────────────────────


async def test_single_short_hold_put_matches_signal_oracle(client):
    """A single short hold-put leg (weight −100, nav_times 1) equity curve equals
    100·(signal-path dollar-NAV oracle) — the shared accumulator + direction once."""
    data = await _compute(client, -100)
    equity = np.array(data["portfolio_equity"], dtype=np.float64)
    expected = 100.0 * _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=1.0, weight=-100.0
    )
    assert equity.shape == expected.shape
    max_abs = float(np.max(np.abs(equity - expected)))
    max_rel = float(np.max(np.abs(equity - expected) / np.abs(expected)))
    np.testing.assert_allclose(equity, expected, rtol=1e-9, atol=1e-9)
    # Surface the diff so the runner log records how tight the match is.
    print(f"[equivalence] max_abs={max_abs:.3e} max_rel={max_rel:.3e}")


async def test_direction_applied_once_short_vs_long(client):
    """Short (w<0) matches the SHORT oracle, long (w>0) the LONG oracle; a short
    leg must NOT collapse onto the long curve (proves no double-short)."""
    short = np.array((await _compute(client, -100))["portfolio_equity"])
    long_ = np.array((await _compute(client, +100))["portfolio_equity"])
    exp_short = 100.0 * _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=1.0, weight=-100.0
    )
    exp_long = 100.0 * _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=1.0, weight=+100.0
    )
    np.testing.assert_allclose(short, exp_short, rtol=1e-9, atol=1e-9)
    np.testing.assert_allclose(long_, exp_long, rtol=1e-9, atol=1e-9)
    assert not np.allclose(short, exp_long)  # short ≠ long (direction applied)


async def test_navtimes_scales_pnl(client):
    """nav_times > 1 (premium-notional leverage) scales the P&L per the oracle."""
    data = await _compute(client, -100, nav_times=2.0)
    equity = np.array(data["portfolio_equity"], dtype=np.float64)
    expected = 100.0 * _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=2.0, weight=-100.0
    )
    np.testing.assert_allclose(equity, expected, rtol=1e-9, atol=1e-9)


@pytest.mark.parametrize("stream", ["mid", "bs_mid"])
async def test_both_premium_streams_take_hold_path(client, stream):
    """Both premium streams (mid, bs_mid) route through the hold P&L path."""
    data = await _compute(client, -100, stream=stream)
    equity = np.array(data["portfolio_equity"], dtype=np.float64)
    expected = 100.0 * _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=1.0, weight=-100.0
    )
    np.testing.assert_allclose(equity, expected, rtol=1e-9, atol=1e-9)


# ── Direct leg-evaluator test (no HTTP, tightest tolerance) ─────────────────


async def test_evaluate_option_stream_leg_hold_returns_synthetic(monkeypatch):
    monkeypatch.setattr(
        "tcg.core.api.portfolio.make_signal_fetcher", _fake_make_signal_fetcher
    )
    leg = LegSpec(**_hold_put_leg())
    dates, values, mode = await _evaluate_option_stream_leg(
        "P", leg, -100.0, MagicMock(), date(2024, 3, 1), date(2024, 4, 30)
    )
    assert mode == "price_hold"
    np.testing.assert_array_equal(dates, _DATES_INT)
    expected = 100.0 * _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=1.0, weight=-100.0
    )
    np.testing.assert_allclose(values, expected, rtol=1e-12, atol=1e-12)


# ── The abs-weight wiring in isolation (why direction must be applied once) ──


def test_abs_weight_round_trips_signed_weight_double_shorts():
    """Feeding the hold synthetic (which already carries the short) with |weight|
    round-trips to itself; a signed (negative) weight re-shorts it (the bug)."""
    equity_ref = _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=1.0, weight=-100.0
    )
    synthetic = 100.0 * equity_ref  # the hold leg's price series
    res_abs = compute_weighted_portfolio(
        {"P": synthetic}, {"P": 100.0}, "none", "normal", _DATES_INT
    )
    np.testing.assert_allclose(
        res_abs.portfolio_equity, synthetic, rtol=1e-12, atol=1e-12
    )
    res_signed = compute_weighted_portfolio(
        {"P": synthetic}, {"P": -100.0}, "none", "normal", _DATES_INT
    )
    assert not np.allclose(res_signed.portfolio_equity, synthetic)


# ── Wire validation + back-compat ───────────────────────────────────────────


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_legspec_nav_times_must_be_positive_finite(bad):
    with pytest.raises(PydanticValidationError) as ei:
        LegSpec(**_hold_put_leg(nav_times=bad))
    assert "nav_times" in str(ei.value)


def test_legspec_hold_defaults_off_and_navtimes_one():
    """Back-compat: the hold/nav_times field DEFAULTS are still off/1.0.  A
    premium (mid/bs_mid) leg now requires hold, so this is exercised through a
    LEVEL stream (iv) — the only option stream that legitimately runs hold-off."""
    leg = LegSpec(
        type="option_stream",
        collection="OPT_SP_500",
        option_type="P",
        maturity={"kind": "end_of_month", "offset_months": 0},
        selection={"kind": "by_delta", "target": -0.10},
        stream="iv",
    )
    assert leg.hold_between_rolls is False
    assert leg.nav_times == 1.0


@pytest.mark.parametrize("stream", ["mid", "bs_mid"])
def test_legspec_premium_leg_without_hold_rejected(stream):
    """A portfolio option PRICE leg (premium stream) without hold-mode is
    rejected at construction: a rolled option's daily-reselect %-return is not a
    valid equity series (``validate_option_price_leg_requires_hold``)."""
    with pytest.raises(ValidationError) as ei:
        LegSpec(**{**_hold_put_leg(stream=stream), "hold_between_rolls": False})
    assert ei.value.error_type == "validation_error"
    assert "hold" in ei.value.message.lower()


@pytest.mark.parametrize("stream", ["iv", "delta", "gamma", "vega", "theta", "volume"])
def test_legspec_level_stream_accepts_hold_off(stream):
    """A LEVEL stream (iv/greeks/volume) is a display-only overlay, exempt from
    the hold requirement — it constructs fine with hold off."""
    leg = LegSpec(
        type="option_stream",
        collection="OPT_SP_500",
        option_type="P",
        maturity={"kind": "end_of_month", "offset_months": 0},
        selection={"kind": "by_delta", "target": -0.10},
        stream=stream,
    )
    assert leg.hold_between_rolls is False


async def test_all_nan_premium_rejected_loudly(monkeypatch):
    """An empty resolve (all-NaN premium) fails with a leg-context error rather
    than a misleading flat-100 leg."""

    def _nan_fetcher(svc, start, end):
        async def fetch(instrument, field):
            return _DATES_INT, np.full(_DATES_INT.shape, np.nan)

        async def fetch_hold_roll_info(instrument):
            return _DATES_INT, _IS_ROLL.copy(), _ROLL_PREMIUM.copy()

        fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
        return fetch

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _nan_fetcher)
    leg = LegSpec(**_hold_put_leg())
    with pytest.raises(TCGError) as ei:
        await _evaluate_option_stream_leg(
            "P", leg, -100.0, MagicMock(), date(2024, 3, 1), date(2024, 4, 30)
        )
    assert "all option stream values are NaN" in str(ei.value)
