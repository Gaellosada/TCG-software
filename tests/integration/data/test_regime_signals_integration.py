"""Live-dwh integration test for the F2.1 regime-signal provider.

Exercises the REAL end-to-end signal path: fetch IND_SP_500 + IND_VVIX through
the P0.3 :class:`DailySeriesReader` seam, COMPUTE realized vol H20/H30/H100 in the
pure engine, and join VVIX passthrough via the core assembly — no mocks in the
exercised path.

Gated by ``--run-integration`` AND by dwh reachability (the pool fixture SKIPS on
a connect failure). The dwh is UNREACHABLE from this egress IP (the known
egress-IP / RDS-security-group blocker logged in the task PROBLEMS.md), so this
test is EXPECTED to SKIP here — and a skip is NOT coverage. NEVER report a skip or
timeout as a pass; the F2.1 correctness rests on the db-less unit suite
(``tests/engine/test_regime.py`` + ``tests/api/test_intraday_regime.py``).

Run once from an allowlisted IP:
    pytest tests/integration/data/test_regime_signals_integration.py --run-integration
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.core.api.intraday_backtest import (
    RunRequest,
    _fetch_regime_signals,
)
from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.daily_series import DailySeriesReader

pytestmark = pytest.mark.integration


@pytest.fixture
async def daily_reader():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001 - retry once (rotating egress IP)
        try:
            await pool.connect()
        except Exception as exc2:  # noqa: BLE001
            pytest.skip(f"dwh not reachable (retried once): {exc2}")
    reader = DailySeriesReader(pool)
    # Reachability PROBE: connect() can return while the pool is still unusable
    # (rotating egress IP / pool-close race). A tiny read confirms the warehouse
    # actually answers; if it times out, SKIP (a skip is NOT coverage) rather
    # than surfacing the known blocker as a false test FAILURE.
    try:
        await reader.read_series(
            "IND_SP_500", start=date(2025, 1, 2), end=date(2025, 1, 3)
        )
    except Exception as exc:  # noqa: BLE001
        await pool.close()
        pytest.skip(f"dwh not reachable (probe read failed): {exc}")
    try:
        yield reader
    finally:
        await pool.close()


async def test_regime_signals_real_rv_and_vvix(daily_reader):
    """Real IND_SP_500 RV + IND_VVIX passthrough over a small 2025 window."""
    # A handful of real February 2025 trading days (weekdays inside the window).
    day_dates = [20250203, 20250204, 20250205, 20250206, 20250207]
    req = RunRequest(
        start_date="2025-02-03",
        end_date="2025-02-07",
        regime={"emit_signals": True, "rv_windows": [20, 30, 100]},
    )

    out = await _fetch_regime_signals(daily_reader, req, day_dates)

    assert set(out) == set(day_dates)
    for d in day_dates:
        sig = out[d]
        assert set(sig) == {"h20", "h30", "h100", "vvix"}
        # With a ~230-calendar-day lookback the RV windows must be warmed up
        # (H100 needs ~100 trading days of history before 2025-02-03).
        assert sig["h20"] is not None and sig["h20"] > 0
        assert sig["h30"] is not None and sig["h30"] > 0
        assert sig["h100"] is not None and sig["h100"] > 0
        # VVIX is a market level (tens); may be None on a non-trading date but on
        # a real session it should be present and positive.
        if sig["vvix"] is not None:
            assert sig["vvix"] > 0
