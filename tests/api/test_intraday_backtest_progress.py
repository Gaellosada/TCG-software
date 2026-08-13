"""HTTP-layer tests for the async intraday-backtest job/progress mechanism.

Deterministic, no dwh: ``run_backtest`` (the per-day orchestration) is
monkeypatched with a fast fake that ticks the progress callback over a KNOWN
set of days. This exercises the real job store + background task + the two new
endpoints (``/run-async`` and ``/progress/{job_id}``) without any DB.

Run against a plain ``TestClient(app)`` (NO context manager) so the DB-opening
lifespan never fires; ``app.state.dwh_pool`` is set to ``None`` by hand (the
monkeypatched ``run_backtest`` never touches the reader).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tcg.core.api import intraday_backtest as ib
from tcg.core.app import create_app

# Mon 2025-02-03 .. Fri 2025-02-14 => 10 weekdays, no exceptions => total 10.
_START = "2025-02-03"
_END = "2025-02-14"
_EXPECTED_TOTAL = 10

_PINNED_RESULT: dict[str, Any] = {
    "params_echo": {"start_date": _START, "end_date": _END},
    "window": {"min_date": "2025-01-01", "max_date": "2026-07-31"},
    "days": [{"date": "2025-02-03", "status": "ok", "strike": 5850.0}],
    "aggregate": {"n_days": 10, "n_traded": 1, "total_pnl_usd": 123.5},
    "warnings": [],
}


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"start_date": _START, "end_date": _END}
    base.update(over)
    return base


@pytest.fixture
def client() -> Any:
    # A background asyncio task only survives across requests when ONE event
    # loop stays alive for the client's lifetime — i.e. TestClient as a context
    # manager. That normally runs the DB-opening lifespan, so we swap in a
    # no-op lifespan (dwh_pool=None; the faked run_backtest never reads it).
    app = create_app()

    @asynccontextmanager
    async def _noop_lifespan(_app: Any) -> Any:
        _app.state.dwh_pool = None
        yield

    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``run_backtest`` with a fake that ticks progress over the real
    (validated) day count, sleeping between ticks so mid-run polls observe
    partial progress. Records the callback sequence for a monotonicity assert.
    """
    recorded: dict[str, Any] = {"seq": None, "called": 0}

    async def _fake(reader: Any, req: Any, progress_cb: Any = None) -> dict[str, Any]:
        recorded["called"] += 1
        total = ib.count_trading_days(req)  # same denominator the endpoint pins
        seq: list[int] = []
        for i in range(1, total + 1):
            if progress_cb is not None:
                progress_cb(i, total)
            seq.append(i)
            await asyncio.sleep(0.02)
        recorded["seq"] = seq
        return _PINNED_RESULT

    monkeypatch.setattr(ib, "run_backtest", _fake)
    return recorded


def _poll_until_terminal(
    client: TestClient, job_id: str, timeout_s: float = 5.0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Poll progress until status is done/error; return (all snapshots, last)."""
    seen: list[dict[str, Any]] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get(f"/api/intraday-backtest/progress/{job_id}")
        assert resp.status_code == 200, resp.text
        snap = resp.json()
        seen.append(snap)
        if snap["status"] in ("done", "error"):
            return seen, snap
        time.sleep(0.005)
    raise AssertionError("job never reached a terminal state")


def test_run_async_returns_job_id(client: TestClient, fake_run: dict[str, Any]) -> None:
    resp = client.post("/api/intraday-backtest/run-async", json=_body())
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["job_id"], str)
    # Drain so the background task finishes cleanly.
    _poll_until_terminal(client, resp.json()["job_id"])


def test_progress_advances_monotonically_to_done(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    job_id = client.post("/api/intraday-backtest/run-async", json=_body()).json()["job_id"]
    seen, final = _poll_until_terminal(client, job_id)

    # total_days pinned correctly (exceptions-removed weekday count).
    assert all(s["total_days"] == _EXPECTED_TOTAL for s in seen)

    # days_done never goes backwards, starts low, ends at total.
    observed = [s["days_done"] for s in seen]
    assert observed == sorted(observed), observed
    assert observed[0] <= _EXPECTED_TOTAL
    # We should have caught at least one genuinely-running mid-state.
    assert any(s["status"] == "running" for s in seen)
    assert any(s["days_done"] < _EXPECTED_TOTAL for s in seen)

    # Terminal snapshot: done, full progress, pinned result echoed back.
    assert final["status"] == "done"
    assert final["days_done"] == _EXPECTED_TOTAL
    assert final["total_days"] == _EXPECTED_TOTAL
    assert final["error"] is None
    assert final["result"] == _PINNED_RESULT

    # The callback itself fired once per day, strictly 1..total.
    assert fake_run["seq"] == list(range(1, _EXPECTED_TOTAL + 1))


def test_unknown_job_id_404(client: TestClient, fake_run: dict[str, Any]) -> None:
    resp = client.get("/api/intraday-backtest/progress/does-not-exist")
    assert resp.status_code == 404


def test_terminal_job_dropped_after_first_fetch(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    job_id = client.post("/api/intraday-backtest/run-async", json=_body()).json()["job_id"]
    _poll_until_terminal(client, job_id)  # last poll observed 'done' -> dropped
    # A subsequent poll now 404s (finished job cleaned up).
    assert client.get(f"/api/intraday-backtest/progress/{job_id}").status_code == 404


def test_validation_400_before_job_creation(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    before = len(ib._JOBS)
    # exit_time <= entry_time is rejected synchronously by count_trading_days.
    resp = client.post(
        "/api/intraday-backtest/run-async",
        json=_body(entry={"time": "15:00"}, exit={"time": "10:00"}),
    )
    assert resp.status_code == 400
    assert "job_id" not in resp.json()
    # No job was created and the fake was never invoked.
    assert len(ib._JOBS) == before
    assert fake_run["called"] == 0


def test_out_of_window_400_before_job_creation(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    resp = client.post(
        "/api/intraday-backtest/run-async",
        json=_body(start_date="2024-01-01", end_date="2024-01-10"),
    )
    assert resp.status_code == 400
    assert fake_run["called"] == 0


# --------------------------------------------------------------------------- #
# run_backtest orchestration: excluded days are emitted (status "excluded"),
# not processed, not counted as traded, and never tick progress — while
# progress still reaches total_days (the non-excluded weekday count). Uses a
# fake reader with no expiries so every processed day skips (no_expiry); the
# point under test is the excluded/processed split, not the trading path.
# --------------------------------------------------------------------------- #
class _FakeReader:
    """Minimal IntradayV2Reader stub: no roots/expiries -> processed days skip."""

    async def list_option_roots(self) -> list[dict[str, Any]]:
        return []

    async def list_expirations(self, oids: Any, start: Any) -> list[Any]:
        return []

    async def get_option_tick_size(self) -> float:
        return 0.05

    async def fetch_es_future_1m(self, *a: Any, **k: Any) -> list[Any]:
        return []


async def test_run_backtest_emits_excluded_day_and_reaches_total() -> None:
    # Mon 2025-02-03 .. Fri 2025-02-07 => 5 weekdays; exclude 02-05 => 4 processed.
    req = ib.RunRequest(
        start_date="2025-02-03",
        end_date="2025-02-07",
        custom_days=[ib.CustomDay(date="2025-02-05", exclude=True)],
    )
    ticks: list[tuple[int, int]] = []
    result = await ib.run_backtest(
        _FakeReader(), req, progress_cb=lambda done, total: ticks.append((done, total))
    )

    days = result["days"]
    assert len(days) == 5, "all weekdays emitted, excluded included"

    excluded = [d for d in days if d["date"] == "2025-02-05"]
    assert len(excluded) == 1
    exday = excluded[0]
    # DISTINCT status "excluded" (not "skipped") so the calendar can flag it.
    assert exday["status"] == "excluded"
    assert exday["skip_reason"] == "excluded"
    assert exday["pnl"] is None  # never processed / not traded

    # Aggregate: excluded day is not traded.
    assert result["aggregate"]["n_days"] == 5
    assert result["aggregate"]["n_traded"] == 0

    # Progress ticked once per PROCESSED day (4), reaching total_days=4, and the
    # excluded day never ticked.
    assert ticks == [(1, 4), (2, 4), (3, 4), (4, 4)]
