"""F4 cash-rate leg — end-to-end through the REAL ``POST /api/portfolio/compute``.

Drives ``_compute_portfolio_uncached`` (§4.7 fetch, §5.5 accrual, §7 combine,
§8 metrics) with only the market-data FETCH mocked — no DB, no live warehouse.
The cash-leg accrual, calendar adoption, combine and metrics are all REAL.

Not marked ``integration`` (that marker means "requires live app-data
PostgreSQL", which this does not): mocking the fetch keeps it in the DB-free
suite while exercising the true compute path, so real output can be pasted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.data.service import DefaultMarketDataService
from tcg.types.market import PriceSeries


def _busday_ints(start: str, n: int) -> np.ndarray:
    """``n`` sequential NumPy business days from ``start`` as YYYYMMDD int64."""
    days = np.busday_offset(np.datetime64(start), np.arange(n), roll="forward")
    ymd = days.astype("datetime64[D]").astype(str)  # 'YYYY-MM-DD'
    return np.array([int(s.replace("-", "")) for s in ymd], dtype=np.int64)


def _price_series(dates: np.ndarray, vals: np.ndarray) -> PriceSeries:
    return PriceSeries(
        dates=dates,
        open=vals,
        high=vals,
        low=vals,
        close=vals,
        volume=np.full(len(dates), 1000.0, dtype=np.float64),
    )


def _make_app(
    dates: np.ndarray,
    inst_closes: dict[str, np.ndarray],
    series_by_symbol: dict[str, PriceSeries] | None = None,
):
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.types.errors import TCGError

    async def _aligned(legs_spec):
        return dates, {
            label: _price_series(dates, inst_closes[label]) for label in legs_spec
        }

    async def _get_prices(collection, instrument_id, *, start=None, end=None, provider=None):
        if series_by_symbol and instrument_id in series_by_symbol:
            return series_by_symbol[instrument_id]
        return None

    svc = MagicMock()
    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)
    svc.get_prices = AsyncMock(side_effect=_get_prices)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = svc
    app.state.app_db_repo = object()
    return app


async def _post(app, body):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post("/api/portfolio/compute", json=body)


def _ann_ret(equity: np.ndarray, n_per_year: int = 252) -> float:
    total = equity[-1] / equity[0]
    years = (len(equity) - 1) / n_per_year
    return total ** (1.0 / years) - 1.0


def _daily_vol(equity: np.ndarray) -> float:
    r = equity[1:] / equity[:-1] - 1.0
    return float(np.std(r))


# ── 1. Flat cash leg accrues through the real path ──────────────────────────


@pytest.mark.asyncio
async def test_flat_cash_leg_accrues_through_real_compute() -> None:
    dates = _busday_ints("2020-01-02", 252)
    flat = np.full(len(dates), 100.0)  # 0-return anchor leg supplies the calendar
    app = _make_app(dates, {"anchor": flat})
    body = {
        "legs": {
            "anchor": {"type": "instrument", "collection": "INDEX", "symbol": "anchor"},
            "cash": {"type": "cash_rate", "cash_rate": {"kind": "flat", "rate_pct": 5.0}},
        },
        "weights": {"anchor": 0.0001, "cash": 100.0},
        "rebalance": "none",
        "start": "2019-01-01",
        "end": "2021-12-31",
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    cash_eq = np.array(data["raw_leg_equities"]["cash"])
    assert np.all(np.isfinite(cash_eq))
    # ``raw_leg_equities`` is seeded at |norm_weight|·100 by the combine, so the
    # LEVEL is weight-scaled; the accrual SHAPE (drift/vol) and ann_ret (a ratio)
    # are what matter here — the exact base-100 accrual is proven by the DB-free
    # unit oracle (tests/engine/test_cash_rate.py).
    assert cash_eq[0] > 0.0
    # Monotonically non-decreasing (all-positive drift) and near-zero vol.
    assert np.all(np.diff(cash_eq) >= -1e-12)
    assert _daily_vol(cash_eq) < 1e-9
    # ~1 trading-year of a 5 %/yr compound accrual -> ann_ret ~= 5 %.
    assert _ann_ret(cash_eq) == pytest.approx(0.05, rel=1e-6)


# ── 2. Validation smoke vs §5.7 profile (2006–2026, flat leg) ───────────────


@pytest.mark.asyncio
async def test_flat_cash_leg_20yr_profile_all_positive_near_zero_vol() -> None:
    """SPEC §5.7 QUALITATIVE profile: over ~2006–2026 the flat leg is
    all-positive, near-zero vol, sane ann_ret. The exact monthly PnL table is
    PENDING from Gael (do NOT fabricate) — only the shape is asserted.
    """
    dates = _busday_ints("2006-08-01", 5040)  # ~20 trading years
    flat = np.full(len(dates), 100.0)
    app = _make_app(dates, {"anchor": flat})
    body = {
        "legs": {
            "anchor": {"type": "instrument", "collection": "INDEX", "symbol": "anchor"},
            "cash": {"type": "cash_rate", "cash_rate": {"kind": "flat", "rate_pct": 1.0}},
        },
        "weights": {"anchor": 0.0001, "cash": 100.0},
        "rebalance": "none",
        "start": "2006-01-01",
        "end": "2026-12-31",
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code == 200, resp.text
    cash_eq = np.array(resp.json()["raw_leg_equities"]["cash"])
    assert cash_eq[-1] > cash_eq[0]  # all-positive drift over the whole span
    assert np.all(np.diff(cash_eq) >= -1e-12)  # never falls
    assert _daily_vol(cash_eq) < 1e-9  # near-zero vol
    assert _ann_ret(cash_eq) == pytest.approx(0.01, rel=1e-6)  # ~1 %/yr


# ── 3. Series-source cash leg reindexes a real (fake-fetched) rate series ────


@pytest.mark.asyncio
async def test_series_cash_leg_uses_rate_series() -> None:
    """A SERIES source reads (collection, symbol), holds the rate piecewise, and
    accrues faster where the rate is higher. Bars before the series' first date
    fall back to the flat ``rate_pct``.
    """
    dates = _busday_ints("2007-01-02", 600)
    flat = np.full(len(dates), 100.0)
    # A rate series (PERCENT): 5% early, stepping to 1% partway, quoted only on a
    # sparse grid starting AFTER the portfolio's first bar (to exercise fallback).
    rate_dates = dates[[50, 300]]
    rate_series = _price_series(rate_dates, np.array([5.0, 1.0]))  # percent
    app = _make_app(
        dates, {"anchor": flat}, series_by_symbol={"RATE_USD": rate_series}
    )
    body = {
        "legs": {
            "anchor": {"type": "instrument", "collection": "INDEX", "symbol": "anchor"},
            "cash": {
                "type": "cash_rate",
                "cash_rate": {
                    "kind": "series",
                    "collection": "FUT_RATE",
                    "symbol": "RATE_USD",
                    "unit": "percent",
                    "rate_pct": 2.0,  # fallback before the series starts
                },
            },
        },
        "weights": {"anchor": 0.0001, "cash": 100.0},
        "rebalance": "none",
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code == 200, resp.text
    cash_eq = np.array(resp.json()["raw_leg_equities"]["cash"])
    assert np.all(np.diff(cash_eq) >= -1e-12)
    # Per-bar growth factor: fallback 2% before bar 50, 5% on [50,300), 1% after.
    factors = cash_eq[1:] / cash_eq[:-1]
    f_fallback = float(np.mean(factors[1:49]))   # bars 2..49 -> 2 %/yr
    f_high = float(np.mean(factors[60:290]))     # bars in the 5 %/yr window
    f_low = float(np.mean(factors[350:590]))     # bars in the 1 %/yr window
    assert f_high > f_fallback > f_low > 1.0
    assert f_high == pytest.approx((1.05) ** (1 / 252), rel=1e-9)
    assert f_low == pytest.approx((1.01) ** (1 / 252), rel=1e-9)
    assert f_fallback == pytest.approx((1.02) ** (1 / 252), rel=1e-9)


# ── 4. A flat-only cash portfolio has no calendar -> clear rejection ─────────


@pytest.mark.asyncio
async def test_flat_only_cash_portfolio_rejected() -> None:
    app = _make_app(np.array([20200101], dtype=np.int64), {})
    body = {
        "legs": {"cash": {"type": "cash_rate", "cash_rate": {"kind": "flat"}}},
        "weights": {"cash": 100.0},
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code == 400, resp.text
    assert "no calendar of its own" in resp.text
