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


# ── Per-roll trade rows for a hold-mode option leg (display-only) ───────────


async def test_hold_option_leg_emits_per_roll_trade_rows(client):
    """A hold-mode option leg emits ONE display-only trade row per held contract
    (segment), labelled open/rolling/end, sized in contracts off the PREMIUM, with
    a per-segment realised P&L — and the equity stays byte-identical to the oracle
    (display-only)."""
    from tcg.core.api.portfolio import _leg_multiplier_and_unit

    data = await _compute(client, -100)
    # Equity is UNCHANGED by the roll rows (display-only hard gate for options).
    equity = np.array(data["portfolio_equity"], dtype=np.float64)
    expected_eq = 100.0 * _oracle_ratio(
        _OWNER_PREV, _OWNER_CUR, _IS_ROLL, _ROLL_PREMIUM, nav_times=1.0, weight=-100.0
    )
    np.testing.assert_allclose(equity, expected_eq, rtol=1e-9, atol=1e-9)

    # DATES_INT = [0327,0328,0329,0401,0402,0403]; IS_ROLL = [1,0,0,1,0,0].
    # Interior roll (excl. initial open) = 20240401 → bar 3 → segments [0,2],[3,5].
    rows = sorted(data["trades"], key=lambda t: t["open_bar"])
    assert len(rows) == 2
    r0, r1 = rows
    assert (r0["entry_block_name"], r0["exit_block_name"]) == ("open", "rolling")
    assert (r1["entry_block_name"], r1["exit_block_name"]) == ("rolling", "end")
    # The interior segment's displayed close bar is the ROLL bar (3, == the next
    # segment's open / the bar its P&L telescopes to), not the prior interior bar
    # (2); the final segment's close is the last bar (5).  See
    # ``test_hold_option_roll_row_close_references_realise_bar``.
    assert (r0["open_bar"], r0["close_bar"]) == (0, 3)
    assert (r1["open_bar"], r1["close_bar"]) == (3, 5)
    for r in rows:
        assert r["direction"] == "short"
        assert r["roll_hover"] == "rolling OPT_SP_500"
        assert r["entry_block_id"] == "roll:P"
        assert r["quantity_unit"] == "contracts"
        assert "_roll_row" not in r

    m_opt, _unit = _leg_multiplier_and_unit("OPT_SP_500")
    assert m_opt is not None
    # COUNT is sized off the ROLL-DAY premium (ROLL_PREMIUM = [30,·,·,18,·,·]) — the
    # basis the accumulator sized against, finite at each segment open (a roll bar) —
    # NOT the daily held premium (which is NaN at a real option's later opens).
    roll_prem = _ROLL_PREMIUM
    # segment P&L is the leg's equity change across the segment: synthetic[boundary] −
    # synthetic[open], where the boundary is the next segment's open (roll bar) — or
    # the last bar for the final segment — so segments TELESCOPE.  opens=[0,3].
    boundaries = [3, len(equity) - 1]
    for r, boundary in zip(rows, boundaries):
        ob = r["open_bar"]
        exp_q = abs(r["signed_weight"]) * equity[ob] / (roll_prem[ob] * m_opt)
        assert r["quantity"] == pytest.approx(exp_q)
        exp_pnl = equity[boundary] - equity[ob]
        assert r["segment_pnl"] == pytest.approx(exp_pnl)
        # The displayed OPEN PRICE is the option's roll-day entry PREMIUM (the basis
        # the count was sized against) — NOT the base-100 synthetic equity. Reported
        # bug: it showed 100 (positions[label] = 100·equity_ratio at bar 0).
        assert r["open_price"] == pytest.approx(roll_prem[ob])
        assert r["open_price"] != pytest.approx(100.0)
        # And it reconciles with the displayed count: qty·price·M ≈ |w|·NAV_open.
        assert r["quantity"] * r["open_price"] * m_opt == pytest.approx(
            abs(r["signed_weight"]) * equity[ob]
        )
    # The per-segment P&L reconciles to the leg's total equity change.
    assert sum(r["segment_pnl"] for r in rows) == pytest.approx(equity[-1] - 100.0)


async def test_hold_option_roll_row_close_references_realise_bar(client):
    """REGRESSION (display-only close bar/price lag): an interior option roll row's
    displayed CLOSE must reference the bar its P&L telescopes to — the ROLL bar
    (``close_boundary``, where the resolver books the held contract's roll-day
    realise mid) — NOT the prior interior bar (``close_boundary - 1``).

    Before the fix ``close_price``/``close_bar`` were measured one bar early, so a
    roll day carrying a large premium move showed a stale pre-move close (price AND
    date) alongside the post-move ``segment_pnl`` — the reported "big move, ~0 P&L,
    dates that don't line up" artifact.  Equity is untouched (display-only).

    Fixture ``HELD_PREMIUM = [30,28,26,24,20,19]``, interior roll at bar 3 →
    segments ``[0,2]`` and ``[3,5]``.  The held (OLD) contract's roll-day realise mid
    is ``HELD_PREMIUM[3] = 24`` (the value segment 0's P&L telescopes through); the
    prior interior bar is ``HELD_PREMIUM[2] = 26``.  They differ, so the fix is
    observable.  The final segment is unchanged (``close_boundary == close_bar``).
    """
    data = await _compute(client, -100)
    rows = sorted(
        (t for t in data["trades"] if t.get("entry_block_id") == "roll:P"),
        key=lambda t: t["open_bar"],
    )
    assert len(rows) == 2
    r0, r1 = rows
    # Interior segment: close references the ROLL bar (3), NOT the interior bar (2).
    assert r0["close_bar"] == 3
    assert r0["close_price"] == pytest.approx(_HELD_PREMIUM[3])  # 24.0 (realise mid)
    assert r0["close_price"] != pytest.approx(_HELD_PREMIUM[2])  # not 26.0 (one early)
    # Final segment is unchanged: close_boundary == close_bar == last bar.
    assert r1["close_bar"] == len(_HELD_PREMIUM) - 1
    assert r1["close_price"] == pytest.approx(_HELD_PREMIUM[-1])  # 19.0
    # Internal consistency: the displayed close price is the held-premium value at
    # the row's OWN close_bar — open/close/pnl now all reconcile at the same bar.
    for r in rows:
        assert r["close_price"] == pytest.approx(_HELD_PREMIUM[r["close_bar"]])


async def test_hold_option_roll_rows_sign_correct_with_nan_tail_premium(monkeypatch):
    """REGRESSION (the reported bug): a profitable SHORT put whose held-premium
    series has TRAILING NaNs (real 10Δ-put shape — quotes only for the first
    contract) must show each roll segment's P&L correctly-signed and FINITE.

    Before the fix, ``segment_pnl`` was ``sign·qty·Δpremium·M`` off the DAILY held
    premium, which is NaN at the later segments' open/close bars → null; the FE then
    fell back to ``(close/open−1)·signed_weight`` on the leg SYNTHETIC (direction
    already baked) → double-inverted a profitable short to NEGATIVE.  The fix derives
    the segment P&L from the accumulator equity (``synthetic − 100``, telescoping)
    and sizes the count off the roll-day premium — both NaN-safe.
    """
    # Held premium quoted only for the first contract (bars 0–2), NaN after — the
    # first segment's CLOSE bar (3) and the second segment's OPEN bar (4) are both in
    # the NaN region, exactly where the old qty·Δpremium path nulled.  is_roll opens
    # at bar 0 and rolls (interior) at bar 4; roll_premium is finite at both.
    dates_int = np.array(
        [20240101, 20240102, 20240103, 20240104, 20240105, 20240106], dtype=np.int64
    )
    held_premium = np.array([30.0, 28.0, 26.0, np.nan, np.nan, np.nan])
    is_roll = np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
    roll_premium = np.array([30.0, np.nan, np.nan, np.nan, 15.0, np.nan])

    def _nan_tail_fetcher(svc, start, end):
        return make_hold_fetch(
            require_hold=True,
            held_premium=held_premium,
            is_roll=is_roll,
            roll_premium=roll_premium,
            dates_int=dates_int,
        )

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _nan_tail_fetcher)
    application = FastAPI()
    application.add_exception_handler(TCGError, tcg_error_handler)
    application.include_router(portfolio_router)
    application.state.market_data = MagicMock()
    application.state.app_db_repo = object()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = {
            "legs": {"P": _hold_put_leg()},
            "weights": {"P": -100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-01-06",
        }
        resp = await ac.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

    equity = np.array(data["portfolio_equity"], dtype=np.float64)
    # The short put is PROFITABLE: premium fell 30→26 before quotes stopped, so the
    # synthetic RISES above 100 (and holds flat once premium is NaN).
    assert equity[-1] > 100.0

    rows = sorted(
        (t for t in data["trades"] if t.get("entry_block_id") == "roll:P"),
        key=lambda t: t["open_bar"],
    )
    assert len(rows) == 2
    seg_pnls = [r["segment_pnl"] for r in rows]

    # (a) every segment P&L is FINITE (old code → None on the NaN bars).
    for v in seg_pnls:
        assert v is not None and np.isfinite(v)
    # (b) the profitable first segment (synthetic rises) is POSITIVE — the sign fix.
    assert seg_pnls[0] > 0.0
    # (c) the segments telescope to the leg's total equity change.
    assert sum(seg_pnls) == pytest.approx(equity[-1] - 100.0)
    # (d) the contract count is finite (sized off the roll-day premium, not the
    #     NaN daily premium — old code nulled segment 1's count).
    for r in rows:
        assert r["quantity"] is not None and np.isfinite(r["quantity"])


async def test_hold_option_roll_rows_no_fallback_flag_when_all_valid(client):
    """DETERMINISM: with a fetcher that exposes NO close→mid fallback side-channel
    (all settlements valid), every roll row carries ``open_price_fallback`` /
    ``close_price_fallback`` == False — the flag is additive and defaults off."""
    data = await _compute(client, -100)
    rows = [t for t in data["trades"] if t.get("entry_block_id") == "roll:P"]
    assert rows
    for r in rows:
        assert r["open_price_fallback"] is False
        assert r["close_price_fallback"] is False


async def test_hold_option_roll_rows_mark_close_mid_fallback(monkeypatch):
    """A hold option leg whose fetcher reports close→mid fallback markers threads
    them to the trade-log rows as ``open_price_fallback`` / ``close_price_fallback``
    booleans, aligned to the exact bar each displayed price was read from.

    Fixture (shared APR→MAY): opens=[0,3], boundaries=[3,5]; r0 open reads
    ROLL_PREMIUM[0], close reads HELD_PREMIUM[3]; r1 open reads ROLL_PREMIUM[3],
    close reads HELD_PREMIUM[5].  Markers set the open at bar 0 and the close at
    bar 3 → r0 flags BOTH True, r1 flags BOTH False.
    """
    roll_premium_fallback = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    close_mid_fallback = np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0])

    def _fb_fetcher(svc, start, end):
        return make_hold_fetch(
            require_hold=True,
            close_mid_fallback=close_mid_fallback,
            roll_premium_fallback=roll_premium_fallback,
        )

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _fb_fetcher)
    application = FastAPI()
    application.add_exception_handler(TCGError, tcg_error_handler)
    application.include_router(portfolio_router)
    application.state.market_data = MagicMock()
    application.state.app_db_repo = object()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = {
            "legs": {"P": _hold_put_leg()},
            "weights": {"P": -100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-03-01",
            "end": "2024-04-30",
        }
        resp = await ac.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

    rows = sorted(
        (t for t in data["trades"] if t.get("entry_block_id") == "roll:P"),
        key=lambda t: t["open_bar"],
    )
    assert len(rows) == 2
    r0, r1 = rows
    # r0: open (bar 0) and close (bar 3) both fell back to the mid.
    assert (r0["open_bar"], r0["close_bar"]) == (0, 3)
    assert r0["open_price_fallback"] is True
    assert r0["close_price_fallback"] is True
    # r1: open (bar 3) and close (bar 5) are on valid settlements → no fallback.
    assert (r1["open_bar"], r1["close_bar"]) == (3, 5)
    assert r1["open_price_fallback"] is False
    assert r1["close_price_fallback"] is False


async def test_hold_option_close_walk_back_over_nan_tail_carries_fallback_flag(
    monkeypatch,
):
    """INTERSECTION lock (R1 nit): the close price WALKS BACK over a NaN tail to an
    EARLIER bar AND that walked-back bar's close came from the mid fallback — the
    walk-back index and the fallback-flag index must stay in lock-step.

    Fixture: held premium quoted only bars 0–2 (NaN after); is_roll opens at bar 0,
    interior roll at bar 4.  Segment 0's close boundary is bar 4 (NaN), so
    ``_last_finite_in`` walks back to bar 2 (value 26.0) — the displayed close.  The
    close→mid fallback marker is set ONLY at bar 2 (the walked-back bar), and left 0
    at bar 3 (the interior ``close_bar``) and bar 4 (the boundary) — the two indices a
    naive impl might read.  So the test passes ONLY if the flag is read at the exact
    bar the walk-back landed on.
    """
    dates_int = np.array(
        [20240101, 20240102, 20240103, 20240104, 20240105, 20240106], dtype=np.int64
    )
    held_premium = np.array([30.0, 28.0, 26.0, np.nan, np.nan, np.nan])
    is_roll = np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
    roll_premium = np.array([30.0, np.nan, np.nan, np.nan, 15.0, np.nan])
    # Fallback set ONLY at bar 2 (where the close walks back to); NOT at bar 3
    # (interior close_bar) or bar 4 (boundary) — an off-by-one would miss it.
    close_mid_fallback = np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0])
    roll_premium_fallback = np.zeros(6, dtype=np.float64)

    def _walk_back_fb_fetcher(svc, start, end):
        return make_hold_fetch(
            require_hold=True,
            held_premium=held_premium,
            is_roll=is_roll,
            roll_premium=roll_premium,
            dates_int=dates_int,
            close_mid_fallback=close_mid_fallback,
            roll_premium_fallback=roll_premium_fallback,
        )

    monkeypatch.setattr(
        "tcg.core.api.portfolio.make_signal_fetcher", _walk_back_fb_fetcher
    )
    application = FastAPI()
    application.add_exception_handler(TCGError, tcg_error_handler)
    application.include_router(portfolio_router)
    application.state.market_data = MagicMock()
    application.state.app_db_repo = object()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = {
            "legs": {"P": _hold_put_leg()},
            "weights": {"P": -100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-01-06",
        }
        resp = await ac.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

    rows = sorted(
        (t for t in data["trades"] if t.get("entry_block_id") == "roll:P"),
        key=lambda t: t["open_bar"],
    )
    assert len(rows) == 2
    r0 = rows[0]
    # (a) close_price is the WALKED-BACK value (bar 2 = 26.0), NOT the NaN boundary
    #     bar 4 (which would em-dash / null); this confirms the walk-back happened.
    assert r0["close_price"] is not None
    assert r0["close_price"] == pytest.approx(held_premium[2])  # 26.0
    # (b) the fallback flag tracks that SAME walked-back bar — set at bar 2, so True.
    assert r0["close_price_fallback"] is True


async def test_multi_leg_option_roll_pnl_scales_with_weight(client):
    """MAJOR-defect regression: an option roll row's ``segment_pnl`` must be
    weight/NAV-scaled dollars (``|w|·NAV_open·leg_return``) — the SAME unit + scaling
    as the continuous-futures rows in the same column — NOT the weight-agnostic
    base-100 leg unit.  Two hold option legs (put −70, call −30) share the same
    premium fixture (identical leg return) and the same portfolio NAV at each open
    bar, so their per-segment P&L ratio must be purely the weight ratio 70:30.

    Before the fix (segment_pnl = synthetic[boundary] − synthetic[open], weight-
    agnostic) the two legs' identical synthetics gave IDENTICAL P&L → ratio 1; this
    test is the one that catches that.  After the fix the ratio is 70/30.
    """
    body = {
        "legs": {
            "P": _hold_put_leg(),
            "C": {**_hold_put_leg(), "option_type": "C"},
        },
        "weights": {"P": -70, "C": -30},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-03-01",
        "end": "2024-04-30",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    p_rows = sorted(
        (t for t in data["trades"] if t.get("entry_block_id") == "roll:P"),
        key=lambda t: t["open_bar"],
    )
    c_rows = sorted(
        (t for t in data["trades"] if t.get("entry_block_id") == "roll:C"),
        key=lambda t: t["open_bar"],
    )
    assert len(p_rows) == 2 and len(c_rows) == 2
    # Each segment: the −70 leg's P&L is 70/30× the −30 leg's (pure weight ratio).
    for pr, cr in zip(p_rows, c_rows):
        assert pr["segment_pnl"] == pytest.approx(cr["segment_pnl"] * (70.0 / 30.0))
        assert pr["segment_pnl"] != pytest.approx(cr["segment_pnl"])  # weight matters

    # …and the −70 leg's P&L is NOT its sole-−100 value (weight now scales it): a
    # single full-weight leg's seg0 = synthetic[boundary]−100, the −70 leg's is
    # 0.70×that.
    sole = await _compute(client, -100)
    sole_rows = sorted(
        (t for t in sole["trades"] if t.get("entry_block_id") == "roll:P"),
        key=lambda t: t["open_bar"],
    )
    assert p_rows[0]["segment_pnl"] == pytest.approx(0.70 * sole_rows[0]["segment_pnl"])
    assert p_rows[0]["segment_pnl"] != pytest.approx(sole_rows[0]["segment_pnl"])


async def test_hold_option_roll_rows_display_only_equity_metrics_byte_identical(
    client, monkeypatch
):
    """HARD GATE (option path): toggling the per-roll display rows must leave
    portfolio equity + ALL metrics + monthly/yearly returns BYTE-identical.  This
    mirrors the continuous-path byte-identity gate
    (``test_roll_rows_are_display_only_equity_metrics_byte_identical``), which the
    option path previously covered only with an oracle ``assert_allclose``.  We
    compare the SAME production compute path WITH vs WITHOUT the rows (by stubbing
    ``_build_roll_rows`` to emit nothing), so the numeric equality is exact — not
    subject to oracle-vs-production last-ULP drift in the synthetic itself."""
    # 1. WITH the rows (normal path): assert roll rows are actually produced, else
    #    the gate is vacuous.
    data_with = await _compute(client, -100)
    assert any(t.get("entry_block_id") == "roll:P" for t in data_with["trades"])

    # 2. WITHOUT the rows: stub the builder to emit nothing, recompute.
    #    The compute path is now backed by the on-disk result cache, so an
    #    identical body would be served from step 1's cached result and never
    #    re-run the (now-stubbed) builder — clear the per-test cache to force a
    #    genuine recompute. (The cache instance is the tmp one installed by the
    #    autouse ``_isolate_portfolio_result_cache`` fixture.)
    from tcg.core.api import portfolio as _portfolio_mod

    _portfolio_mod._result_cache.clear()
    monkeypatch.setattr("tcg.core.api.portfolio._build_roll_rows", lambda **kw: [])
    data_without = await _compute(client, -100)
    assert not any(t.get("entry_block_id") == "roll:P" for t in data_without["trades"])

    # 3. Every numeric output is byte-identical across the toggle.
    for key in ("portfolio_equity", "metrics", "monthly_returns", "yearly_returns"):
        assert data_with[key] == data_without[key], key


async def test_futures_notional_option_roll_row_count_follows_sizing_mode(monkeypatch):
    """D1: a ``sizing_mode == "futures_notional"`` option leg's roll-row COUNT is
    sized off the FUTURES notional ``|w|·NAV/(F_ref·m_fut)`` — DISTINCT from the
    premium-notional count ``|w|·NAV/(premium·m_opt)`` — while the per-segment P&L
    stays on the premium with the leg's own multiplier."""
    from tcg.core.api.portfolio import _leg_multiplier_and_unit
    from tcg.core.api.portfolio import router as portfolio_router

    # Reference-future price finite only at the roll bars (0 and 3), where each
    # roll-row segment opens; m_fut = m_opt = 50 (SP_500).
    roll_fref = np.array([4500.0, np.nan, np.nan, 4520.0, np.nan, np.nan])
    m_fut = 50.0

    def _fut_fetcher(svc, start, end):
        return make_hold_fetch(
            require_hold=True, roll_future_ref=roll_fref, multipliers=(m_fut, m_fut)
        )

    monkeypatch.setattr("tcg.core.api.portfolio.make_signal_fetcher", _fut_fetcher)
    application = FastAPI()
    application.add_exception_handler(TCGError, tcg_error_handler)
    application.include_router(portfolio_router)
    application.state.market_data = MagicMock()
    application.state.app_db_repo = object()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = {
            "legs": {"P": {**_hold_put_leg(), "sizing_mode": "futures_notional"}},
            "weights": {"P": -100},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-03-01",
            "end": "2024-04-30",
        }
        resp = await ac.post("/api/portfolio/compute", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()

    rows = sorted(
        (t for t in data["trades"] if t.get("entry_block_id") == "roll:P"),
        key=lambda t: t["open_bar"],
    )
    assert len(rows) == 2
    equity = np.array(data["portfolio_equity"], dtype=np.float64)
    m_opt, _unit = _leg_multiplier_and_unit("OPT_SP_500")
    roll_prem = _ROLL_PREMIUM
    boundaries = [3, len(equity) - 1]
    for r, boundary in zip(rows, boundaries):
        ob = r["open_bar"]
        # D1 count = futures-notional value.
        exp_fn = abs(r["signed_weight"]) * equity[ob] / (roll_fref[ob] * m_fut)
        assert r["quantity"] == pytest.approx(exp_fn)
        # …and it is DISTINCT from the premium-notional count it USED to show.
        exp_premium = abs(r["signed_weight"]) * equity[ob] / (roll_prem[ob] * m_opt)
        assert r["quantity"] != pytest.approx(exp_premium)
        # P&L is the leg equity change across the segment (accumulator-derived).
        assert r["segment_pnl"] == pytest.approx(equity[boundary] - equity[ob])
    assert sum(r["segment_pnl"] for r in rows) == pytest.approx(equity[-1] - 100.0)


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
    (
        dates,
        values,
        mode,
        roll_dates,
        premium,
        future_ref,
        m_fut,
        roll_premium,
        close_fallback,
        roll_open_fallback,
    ) = await _evaluate_option_stream_leg(
        "P", leg, -100.0, MagicMock(), date(2024, 3, 1), date(2024, 4, 30)
    )
    assert mode == "price_hold"
    # Interior roll boundaries = is_roll dates minus the initial open (20240327):
    # IS_ROLL = [1,0,0,1,0,0] over DATES_INT → the single interior roll is 20240401.
    assert roll_dates == [20240401]
    np.testing.assert_array_equal(np.asarray(premium), _HELD_PREMIUM)
    # A premium-notional (default) leg leaves the futures-notional side-channels
    # inert: no reference-future series and a NaN futures multiplier.
    assert future_ref is None
    assert np.isnan(m_fut)
    # The roll-day OPEN premium (finite at roll bars) is threaded out so the roll
    # row's contract COUNT is sized off it (not the mostly-NaN daily premium).
    np.testing.assert_array_equal(np.asarray(roll_premium), _ROLL_PREMIUM)
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
    (
        dates,
        values,
        mode,
        roll_dates,
        premium,
        future_ref,
        m_fut,
        roll_premium,
        close_fallback,
        roll_open_fallback,
    ) = await _evaluate_option_stream_leg(
        "P", leg, -100.0, MagicMock(), date(2024, 3, 1), date(2024, 4, 30)
    )
    assert mode == "price_hold"
    # futures_notional threads the reference-future series + resolved m_fut out for
    # the display-only roll-row sizing (D1).
    np.testing.assert_array_equal(np.asarray(future_ref), roll_fref)
    assert m_fut == pytest.approx(50.0)
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
