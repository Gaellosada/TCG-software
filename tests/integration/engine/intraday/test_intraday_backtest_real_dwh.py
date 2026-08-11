"""Live-dwh integration test for the intraday ATM-straddle backtest.

Gated by ``--run-integration`` (tests/integration/conftest.py) AND by dwh
reachability (``load_dwh_config`` / ``pool.connect()`` -> skip on failure).

Proves the end-to-end data wiring on a SMALL real window (2 trading days in
2025): the real ``IntradayV2Reader`` (full-timestamptz 1m reads over
``tcg_instruments_v2``, read-only ``tcg_read``) feeding the pure engine via the
router's ``run_backtest``. No mocks in the exercised path.
"""

from __future__ import annotations

import pytest

from tcg.core.api.intraday_backtest import RunRequest, run_backtest
from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.intraday_v2 import IntradayV2Reader
from tcg.engine.intraday_backtest import resolve_et_to_utc
from datetime import date

pytestmark = pytest.mark.integration

# A small window in mid-2025, well inside the intraday window (2025-01..2026-07).
_START = "2025-06-16"  # Monday
_END = "2025-06-17"  # Tuesday


@pytest.fixture
async def reader():
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
    try:
        yield IntradayV2Reader(pool)
    finally:
        await pool.close()


async def test_option_roots_present(reader):
    roots = await reader.list_option_roots()
    assert roots, "expected ES option roots (OPT_SP_500_*) in tcg_instruments_v2"
    assert all(r["symbol"].startswith("OPT_SP_500") for r in roots)


async def test_es_future_intraday_bars_present(reader):
    day = date(2025, 6, 16)
    start_ts = resolve_et_to_utc(day, "09:30")
    end_ts = resolve_et_to_utc(day, "16:00")
    bars = await reader.fetch_es_future_1m(start_ts, end_ts, on_or_after=day)
    assert bars, "expected front ES-future 1m bars for a 2025 trading day"
    # Full timestamptz preserved (not truncated to midnight).
    assert any(b.ts.hour != 0 for b in bars)
    assert all(b.price > 0 for b in bars)


async def test_run_backtest_end_to_end(reader):
    req = RunRequest(
        start_date=_START,
        end_date=_END,
        entry_time="10:00",
        exit_time="15:00",
        expiry_mode="NDTE",
        dte=0,
        straddle_side="long",
        snap_tolerance_minutes=30.0,
    )
    result = await run_backtest(reader, req)

    days = result["days"]
    assert len(days) == 2, "two weekdays in the range"
    assert result["aggregate"]["n_days"] == 2

    ok_days = [d for d in days if d["status"] == "ok"]
    # At least one day should trade on real marks; if the front strikes were
    # too thin at the chosen times both may skip — surface that loudly rather
    # than passing vacuously.
    assert ok_days, (
        "no day traded on real marks; days="
        + ", ".join(f"{d['date']}:{d['status']}/{d['skip_reason']}" for d in days)
    )
    d0 = ok_days[0]
    assert d0["strike"] and d0["strike"] > 0
    assert d0["entry"]["underlying"] > 0
    assert d0["entry"]["straddle_price"] > 0
    assert d0["exit"]["straddle_price"] > 0
    assert d0["pnl"]["total_pnl_usd"] == pytest.approx(
        d0["pnl"]["total_pnl_pts"] * 50.0
    )
