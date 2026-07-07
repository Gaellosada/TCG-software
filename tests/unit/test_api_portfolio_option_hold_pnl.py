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
from unittest.mock import AsyncMock, MagicMock

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

from _hold_pnl_oracle import (
    DATES_INT as _DATES_INT,
    HELD_PREMIUM as _HELD_PREMIUM,
    IS_ROLL as _IS_ROLL,
    OWNER_CUR as _OWNER_CUR,
    OWNER_PREV as _OWNER_PREV,
    ROLL_PREMIUM as _ROLL_PREMIUM,
    make_hold_fetch,
    oracle_ratio as _oracle_ratio,
)

# Async tests auto-marked (asyncio_mode="auto").

# The APR→MAY hold fixture (``_DATES_INT`` / ``_HELD_PREMIUM`` / ``_IS_ROLL`` /
# ``_ROLL_PREMIUM`` / ``_OWNER_PREV`` / ``_OWNER_CUR``), the dollar-NAV
# ``_oracle_ratio``, and the synthetic ``make_hold_fetch`` builder are the SHARED
# hold-P&L helpers (``tests/_hold_pnl_oracle``) — the same fixture the signal-side
# oracle tests pin, so the portfolio path is measured against the same reference.


def _fake_make_signal_fetcher(svc, start, end):
    """Synthetic fetcher over the shared APR→MAY fixture (dwh-free), matching the
    real ``make_signal_fetcher`` shape (a callable + ``.fetch_hold_roll_info``).
    ``require_hold`` proves the portfolio path passes hold=True to the fetcher."""
    return make_hold_fetch(require_hold=True)


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


async def test_evaluate_option_stream_leg_futures_notional(monkeypatch):
    """A portfolio option PRICE leg in futures_notional mode books the futures
    oracle equity (proves portfolio.py threads roll_future_ref + multipliers)."""
    from _hold_pnl_oracle import oracle_ratio_futures

    roll_fref = np.array([4500.0, np.nan, np.nan, 4520.0, np.nan, np.nan])

    def _fut_fetcher(svc, start, end):
        return make_hold_fetch(
            require_hold=True,
            roll_future_ref=roll_fref,
            multipliers=(50.0, 50.0),  # SP_500
        )

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _fut_fetcher)
    leg = LegSpec(**_hold_put_leg(), sizing_mode="futures_notional")
    dates, values, mode = await _evaluate_option_stream_leg(
        "P", leg, -100.0, MagicMock(), date(2024, 3, 1), date(2024, 4, 30)
    )
    assert mode == "price_hold"
    expected = 100.0 * oracle_ratio_futures(
        _OWNER_PREV,
        _OWNER_CUR,
        _IS_ROLL,
        roll_fref,
        nav_times=1.0,
        weight=-100.0,
        m_fut=50.0,
        m_opt=50.0,
    )
    np.testing.assert_allclose(values, expected, rtol=1e-10, atol=1e-10)


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
    than a misleading flat-100 leg.  This fetcher exposes NO diagnostics
    side-channel, so the message is the base one (``_diagnostic_hint`` adds
    nothing when diagnostics are absent)."""

    def _nan_fetcher(svc, start, end):
        return make_hold_fetch(held_premium=np.full(_DATES_INT.shape, np.nan))

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _nan_fetcher)
    leg = LegSpec(**_hold_put_leg())
    with pytest.raises(TCGError) as ei:
        await _evaluate_option_stream_leg(
            "P", leg, -100.0, MagicMock(), date(2024, 3, 1), date(2024, 4, 30)
        )
    assert "all option stream values are NaN" in str(ei.value)


async def test_all_nan_premium_error_names_dominant_cause(monkeypatch):
    """The hold-path all-NaN error THREADS the resolver's per-date diagnostics
    (surfaced by the fetcher's ``fetch_hold_diagnostics`` side-channel) and names
    the dominant cause + an actionable hint (reusing ``_diagnostic_hint``) — so a
    ByDelta leg with no stored deltas is steered to ByMoneyness rather than getting
    a blunt message."""

    def _diag_fetcher(svc, start, end):
        # All-NaN premium + diagnostics dominated by missing stored deltas.
        return make_hold_fetch(
            held_premium=np.full(_DATES_INT.shape, np.nan),
            diagnostics=["missing_delta_no_compute"] * 5 + ["missing_mid"],
        )

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _diag_fetcher)
    leg = LegSpec(**_hold_put_leg())
    with pytest.raises(TCGError) as ei:
        await _evaluate_option_stream_leg(
            "P", leg, -100.0, MagicMock(), date(2024, 3, 1), date(2024, 4, 30)
        )
    msg = str(ei.value)
    assert "all option stream values are NaN" in msg
    # Dominant cause named (5 of 6 dates) + the actionable ByMoneyness hint.
    assert "dominant cause: missing_delta_no_compute (5/6 dates)" in msg
    assert "By Moneyness" in msg


async def test_all_nan_premium_error_names_missing_cycle(monkeypatch):
    """When the empty resolve is caused by a cycle tag that doesn't exist for the
    root (e.g. 'Q' for OPT_SP_500), the all-NaN error is REPLACED by a targeted
    message that names the requested cycle and lists the root's real cycles —
    steering the user instead of a generic no_chain hint."""

    def _nan_fetcher(svc, start, end):
        return make_hold_fetch(held_premium=np.full(_DATES_INT.shape, np.nan))

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _nan_fetcher)
    svc = MagicMock()
    svc.get_available_cycles = AsyncMock(
        return_value=["M", "W1 Friday", "W2 Friday", "W3 Friday", "W4 Friday"]
    )
    leg = LegSpec(**{**_hold_put_leg(), "cycle": "Q"})
    with pytest.raises(TCGError) as ei:
        await _evaluate_option_stream_leg(
            "P", leg, -100.0, svc, date(2024, 3, 1), date(2024, 4, 30)
        )
    msg = str(ei.value)
    assert "Leg 'P': no contracts match cycle 'Q' for OPT_SP_500" in msg
    assert "available cycles: M, W1 Friday, W2 Friday, W3 Friday, W4 Friday" in msg
    # The generic all-NaN phrasing is REPLACED, not appended.
    assert "all option stream values are NaN" not in msg


async def test_all_nan_premium_weekly_union_is_not_flagged_as_missing_cycle(
    monkeypatch,
):
    """A 'W' leg on OPT_SP_500 must NOT be blamed on the cycle: expand_cycle('W')
    now includes the 'W# Friday' tags the root actually has, so the cycle IS
    available.  On an (unrelated) empty resolve the error falls back to the
    generic all-NaN message, never the 'no contracts match cycle W' hint."""

    def _nan_fetcher(svc, start, end):
        return make_hold_fetch(held_premium=np.full(_DATES_INT.shape, np.nan))

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _nan_fetcher)
    svc = MagicMock()
    svc.get_available_cycles = AsyncMock(
        return_value=["M", "W1 Friday", "W2 Friday", "W3 Friday", "W4 Friday"]
    )
    leg = LegSpec(**{**_hold_put_leg(), "cycle": "W"})
    with pytest.raises(TCGError) as ei:
        await _evaluate_option_stream_leg(
            "P", leg, -100.0, svc, date(2024, 3, 1), date(2024, 4, 30)
        )
    msg = str(ei.value)
    assert "no contracts match cycle" not in msg
    assert "all option stream values are NaN" in msg


# ── Findings 8 & 9: incompatible knobs rejected for a hold-mode price leg ────


@pytest.mark.parametrize(
    "rebalance", ["daily", "weekly", "monthly", "quarterly", "annually"]
)
async def test_hold_leg_rejects_rebalance_not_none(client, rebalance):
    """A hold-mode option price leg forbids rebalance != 'none': a wiped leg would
    be silently re-funded to its target weight at each boundary, draining the
    survivors (``metrics._compute_periodic_rebalance`` re-funds a 0-valued leg)."""
    body = {
        "legs": {"P": _hold_put_leg()},
        "weights": {"P": -100},
        "rebalance": rebalance,
        "return_type": "normal",
        "start": "2024-03-01",
        "end": "2024-04-30",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    j = resp.json()
    assert j["error_type"] == "validation_error"
    assert "rebalance='none'" in j["message"]


async def test_hold_leg_rejects_log_return_type(client):
    """A hold-mode option price leg forbids return_type='log': a leg wiped to zero
    (ln(0) = -inf) is held FLAT instead of going to zero, overstating equity."""
    body = {
        "legs": {"P": _hold_put_leg()},
        "weights": {"P": -100},
        "rebalance": "none",
        "return_type": "log",
        "start": "2024-03-01",
        "end": "2024-04-30",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    j = resp.json()
    assert j["error_type"] == "validation_error"
    assert "return_type='normal'" in j["message"]


async def test_hold_leg_allows_rebalance_none_and_normal(client):
    """The guard is scoped: the SAME hold leg with rebalance='none' + return
    'normal' still computes (200) — proves findings 8/9 reject only the
    incompatible knobs, not the hold leg itself."""
    data = await _compute(client, -100)
    assert len(data["portfolio_equity"]) == len(_DATES_INT)
