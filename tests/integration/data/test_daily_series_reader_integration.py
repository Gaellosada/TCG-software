"""Live-dwh integration test for the P0.3 generic daily-series seam.

Fetches a REAL daily series through :class:`DailySeriesReader` against the dwh
``tcg_instruments`` schema — the same seam F2.1 will use to compute realized vol
from ``IND_SP_500`` daily closes and to gate on VVIX.

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by the
``DWH_*`` connection variables being present/reachable. If the dwh is unreachable
(the known egress-IP / RDS-security-group blocker logged in the task PROBLEMS.md),
the pool fixture SKIPS — and a skip is NOT coverage. Never report a skip/timeout
here as a pass; the SQL-shape + coercion guarantees rest on the unit suite.

Run once from an allowlisted IP:
    pytest tests/integration/data/test_daily_series_reader_integration.py \
        --run-integration
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.daily_series import DailySeriesReader


@pytest.fixture
async def reader():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield DailySeriesReader(pool)
    await pool.close()


@pytest.mark.integration
async def test_ind_vvix_daily_series_real_path(reader):
    """``IND_VVIX`` daily closes via the generic seam (the F5 VVIX gate feed)."""
    series = await reader.read_series(
        "IND_VVIX", start=date(1980, 1, 1), end=date(2050, 12, 31)
    )
    assert series.symbol == "IND_VVIX"
    assert series.field == "close"
    assert len(series) > 0, "IND_VVIX must return a non-empty daily series"
    # ORDER BY trade_date => ascending YYYYMMDD ints; all values finite/positive.
    assert series.dates == sorted(series.dates)
    assert all(v > 0 for v in series.values), "VVIX closes must be positive"


@pytest.mark.integration
async def test_ind_sp_500_daily_closes_for_rv_computation(reader):
    """``IND_SP_500`` daily closes — the raw input F2.1 derives realized vol from.

    P0.3 returns the RAW close series only; NO realized-vol logic lives here.
    """
    series = await reader.read_series(
        "IND_SP_500", start=date(2024, 1, 1), end=date(2026, 7, 31)
    )
    assert series.symbol == "IND_SP_500"
    assert len(series) > 100, "expected a multi-year daily close history"
    assert series.dates == sorted(series.dates)
    assert all(v > 0 for v in series.values)


@pytest.mark.integration
async def test_date_range_filter_narrows_the_series(reader):
    """A tighter range must return a strict subset span (partition-pruned read)."""
    wide = await reader.read_series(
        "IND_SP_500", start=date(2024, 1, 1), end=date(2026, 7, 31)
    )
    narrow = await reader.read_series(
        "IND_SP_500", start=date(2025, 1, 1), end=date(2025, 3, 31)
    )
    assert len(narrow) < len(wide)
    assert all(20250101 <= d <= 20250331 for d in narrow.dates)
