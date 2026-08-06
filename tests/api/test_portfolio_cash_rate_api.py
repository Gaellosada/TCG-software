"""F4 cash-rate leg — end-to-end through the REAL ``POST /api/portfolio/compute``.

Drives ``_compute_portfolio_uncached`` (§4.7 fetch, §5.5 accrual, §7 combine,
§8 metrics) with only the market-data FETCH mocked — no DB, no live warehouse.
The cash-leg accrual, calendar adoption, combine and metrics are all REAL.

The cash-rate leg is now a SERIES source only (the flat constant was removed).
A rate leg reads its annualized-percent series through the v2 path
(``data_source='v2'``), so the mock is bound on ``app.state.market_data_v2_compat``.
A cash-ONLY portfolio is now VALID: the rate series supplies its own calendar.

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
    # Rate legs resolve through the v2 service; bind the same mock so the fetch
    # (get_prices) is served regardless of which source a leg selects.
    app.state.market_data_v2_compat = svc
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


def _rate_leg(collection="RATE", symbol="RATE_US_CMT_1M", **extra):
    return {
        "type": "cash_rate",
        "data_source": "v2",
        "cash_rate": {"collection": collection, "symbol": symbol, **extra},
    }


# ── 1. Series cash leg accrues through the real path (constant 5% series) ────


@pytest.mark.asyncio
async def test_series_cash_leg_accrues_through_real_compute() -> None:
    dates = _busday_ints("2020-01-02", 252)
    flat = np.full(len(dates), 100.0)  # 0-return anchor leg supplies the calendar
    rate = _price_series(dates, np.full(len(dates), 5.0))  # 5 %/yr, percent
    app = _make_app(dates, {"anchor": flat}, series_by_symbol={"RATE_US_CMT_1M": rate})
    body = {
        "legs": {
            "anchor": {"type": "instrument", "collection": "INDEX", "symbol": "anchor"},
            "cash": _rate_leg(),
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
    assert np.all(np.diff(cash_eq) >= -1e-12)   # all-positive drift
    assert _daily_vol(cash_eq) < 1e-9           # near-zero vol
    assert _ann_ret(cash_eq) == pytest.approx(0.05, rel=1e-6)  # ~5 %/yr


# ── 2. Cash-ONLY portfolio is now VALID (rate series supplies the calendar) ──


@pytest.mark.asyncio
async def test_cash_only_portfolio_accepted_series_supplies_calendar() -> None:
    """A rate-only portfolio (no companion instrument) computes: the fetched
    rate series' own dates ARE the trading calendar. This was REJECTED under the
    old flat leg; it must now succeed.
    """
    dates = _busday_ints("2006-08-01", 5040)  # ~20 trading years
    rate = _price_series(dates, np.full(len(dates), 1.0))  # 1 %/yr, percent
    app = _make_app(dates, {}, series_by_symbol={"RATE_US_CMT_1M": rate})
    body = {
        "legs": {"cash": _rate_leg()},
        "weights": {"cash": 100.0},
        "rebalance": "none",
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code == 200, resp.text
    cash_eq = np.array(resp.json()["raw_leg_equities"]["cash"])
    assert cash_eq[-1] > cash_eq[0]             # all-positive drift over the span
    assert np.all(np.diff(cash_eq) >= -1e-12)   # never falls
    assert _daily_vol(cash_eq) < 1e-9           # near-zero vol
    assert _ann_ret(cash_eq) == pytest.approx(0.01, rel=1e-6)  # ~1 %/yr


# ── 3. Series cash leg reindexes a real (fake-fetched) stepped rate series ───


@pytest.mark.asyncio
async def test_series_cash_leg_tracks_rate_path() -> None:
    """A SERIES source reads (collection, symbol), holds the rate piecewise, and
    accrues faster where the rate is higher. Bars before the series' first date
    accrue at 0 (the default fallback).
    """
    dates = _busday_ints("2007-01-02", 600)
    flat = np.full(len(dates), 100.0)
    # A rate series (PERCENT): 5% early, stepping to 1% partway, quoted only on a
    # sparse grid starting AFTER the portfolio's first bar (to exercise fallback).
    rate_dates = dates[[50, 300]]
    rate_series = _price_series(rate_dates, np.array([5.0, 1.0]))  # percent
    app = _make_app(
        dates, {"anchor": flat}, series_by_symbol={"RATE_US_CMT_1M": rate_series}
    )
    body = {
        "legs": {
            "anchor": {"type": "instrument", "collection": "INDEX", "symbol": "anchor"},
            "cash": _rate_leg(unit="percent"),
        },
        "weights": {"anchor": 0.0001, "cash": 100.0},
        "rebalance": "none",
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code == 200, resp.text
    cash_eq = np.array(resp.json()["raw_leg_equities"]["cash"])
    assert np.all(np.diff(cash_eq) >= -1e-12)
    # Per-bar growth factor: fallback 0% before bar 50, 5% on [50,300), 1% after.
    factors = cash_eq[1:] / cash_eq[:-1]
    f_fallback = float(np.mean(factors[1:49]))   # bars 2..49 -> 0 %/yr (factor 1.0)
    f_high = float(np.mean(factors[60:290]))     # bars in the 5 %/yr window
    f_low = float(np.mean(factors[350:590]))     # bars in the 1 %/yr window
    assert f_high > f_low > f_fallback
    assert f_high == pytest.approx((1.05) ** (1 / 252), rel=1e-9)
    assert f_low == pytest.approx((1.01) ** (1 / 252), rel=1e-9)
    assert f_fallback == pytest.approx(1.0, rel=1e-12)  # pre-series bars: no accrual


# ── 4. A rate series that returns no data -> clear rejection ────────────────


@pytest.mark.asyncio
async def test_series_cash_leg_no_data_rejected() -> None:
    app = _make_app(
        np.array([20200101], dtype=np.int64), {}, series_by_symbol={}
    )
    body = {
        "legs": {"cash": _rate_leg()},
        "weights": {"cash": 100.0},
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code == 400, resp.text
    assert "returned no data" in resp.text


# ── 5. Missing collection/symbol on the spec -> validation error ────────────


@pytest.mark.asyncio
async def test_series_cash_leg_requires_ref() -> None:
    app = _make_app(np.array([20200101], dtype=np.int64), {})
    body = {
        "legs": {"cash": {"type": "cash_rate", "cash_rate": {"unit": "percent"}}},
        "weights": {"cash": 100.0},
        "use_cache": False,
    }
    resp = await _post(app, body)
    assert resp.status_code in (400, 422), resp.text
    assert "collection" in resp.text and "symbol" in resp.text
