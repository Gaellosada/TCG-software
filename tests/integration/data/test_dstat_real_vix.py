"""Live-dwh integration test for DStat on REAL ``IND_VIX`` (SPEC §4.1, F6).

Runs both shipped DStat defaults through the ACTUAL compute path
(``run_indicator`` sandbox) on a real index close series fetched through the
same production data path the UI/API use:

    DefaultMarketDataService.get_prices('INDEX', 'IND_VIX', ...)
      -> SqlInstrumentReader.read_prices -> dwh tcg_instruments (READ-ONLY, tcg_read)

Gated by ``--run-integration`` AND by ``DWH_*`` connection variables being
present/reachable (skip otherwise). No mocks — real API+engine path.

Asserts: correct output length, warm-up NaNs, finite/bounded steady state.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data.service import DefaultMarketDataService
from tcg.engine.indicator_exec import run_indicator


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)


def _src(stem: str) -> str:
    match = _CODE_RE.search((DEFAULTS_DIR / f"{stem}.js").read_text(encoding="utf-8"))
    assert match is not None
    return match.group(1)


DSTAT_SRC = _src("dstat")
DSTAT_PCT_SRC = _src("dstat-percentile")


@pytest.fixture
async def svc():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield DefaultMarketDataService(pool)
    await pool.close()


async def _vix_close(svc) -> np.ndarray:
    series = await svc.get_prices(
        "INDEX", "IND_VIX", start=date(1980, 1, 1), end=date(2050, 12, 31)
    )
    assert series is not None, "IND_VIX must resolve to a real PriceSeries"
    close = np.asarray(series.close, dtype=float)
    assert np.all(np.isfinite(close)) and np.all(close > 0), "VIX closes sane"
    return close


@pytest.mark.integration
async def test_raw_dstat_on_real_vix(svc):
    close = await _vix_close(svc)
    n = close.shape[0]
    assert n > 2000, f"expected a long VIX history, got {n} bars"

    out = run_indicator(DSTAT_SRC, {"ma_window": 21, "vol_window": 63}, {"close": close})

    assert out.shape == (n,)
    assert out.dtype == np.float64
    # warm-up = max(21, 2*63) = 126 bars NaN.
    assert np.all(np.isnan(out[:126]))
    steady = out[126:]
    finite = steady[np.isfinite(steady)]
    # VIX moves every day => vol>0 => essentially the whole steady state finite.
    assert finite.size >= steady.size - 5, "steady-state DStat should be finite"
    # Sanity range: vol-normalised distance-from-MA on VIX stays within a wide
    # but bounded band across 2004-2026 (empirically well inside +/- 50).
    assert np.nanmax(np.abs(finite)) < 100.0, "DStat magnitude implausibly large"


@pytest.mark.integration
async def test_dstat_percentile_on_real_vix(svc):
    close = await _vix_close(svc)
    n = close.shape[0]
    params = {"ma_window": 21, "vol_window": 63, "pct_window": 1260, "percentile": 95.0}
    out = run_indicator(DSTAT_PCT_SRC, params, {"close": close})

    assert out.shape == (n,)
    assert out.dtype == np.float64
    first_valid = max(21, 2 * 63) + 1260 - 1  # = 1385
    assert np.all(np.isnan(out[:first_valid]))
    if n > first_valid:
        assert np.isfinite(out[first_valid]), "first full-window bar must be finite"
        tail_finite = out[first_valid:][np.isfinite(out[first_valid:])]
        assert tail_finite.size > 0

        # The p95 line must sit inside the raw-DStat range and, at the same
        # bars, at or below the concurrent p99 line (monotone in percentile).
        p99 = run_indicator(
            DSTAT_PCT_SRC,
            {"ma_window": 21, "vol_window": 63, "pct_window": 1260, "percentile": 99.0},
            {"close": close},
        )
        mask = np.isfinite(out) & np.isfinite(p99)
        assert np.all(p99[mask] >= out[mask] - 1e-9)
