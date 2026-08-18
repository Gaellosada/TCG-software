"""HTTP + unit tests for the durable intraday-backtest result cache.

Deterministic, NO dwh: ``run_backtest`` (the per-day orchestration that touches
the reader) is monkeypatched with a fast fake, and the on-disk cache is isolated
to a per-test tmp file by the autouse root-conftest fixture
(``_isolate_intraday_result_cache``) — so ``ib._intraday_result_cache`` is that
fresh per-test instance. Nothing here needs a real database.

Covers:
* cache-key stability (equal bodies ⇒ equal key; ``use_cache`` toggle is inert;
  a ``INTRADAY_COMPUTE_VERSION`` bump changes the key);
* ``/cache/get`` on a miss returns ``{"cached": false}`` and NEVER computes;
* a completed ``use_cache=True`` run stores the result, and a later ``/cache/get``
  returns it with ``from_cache: true``;
* the ``use_cache=False`` run path neither reads nor writes the cache;
* the ``/run-async`` HIT fast-path serves a stored result without recomputing.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tcg.core.api import intraday_backtest as ib
from tcg.core.app import create_app

# Mon 2025-02-03 .. Fri 2025-02-14 => 10 weekdays, inside the intraday window.
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
    """TestClient as a context manager so ONE event loop survives across polls
    (background asyncio tasks need it). A no-op lifespan avoids the DB-opening
    startup; the faked ``run_backtest`` never reads ``app.state.dwh_pool``."""
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
    """Patch ``run_backtest`` with a fast fake that records invocation count and
    returns a fresh COPY of the pinned result each call (so a stored/mutated dict
    can never alias the fake's constant)."""
    recorded: dict[str, Any] = {"called": 0}

    async def _fake(reader: Any, req: Any, progress_cb: Any = None) -> dict[str, Any]:
        recorded["called"] += 1
        total = ib.count_trading_days(req)
        if progress_cb is not None:
            for i in range(1, total + 1):
                progress_cb(i, total)
        return {**_PINNED_RESULT}

    monkeypatch.setattr(ib, "run_backtest", _fake)
    return recorded


def _poll_until_terminal(
    client: TestClient, job_id: str, timeout_s: float = 5.0
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get(f"/api/intraday-backtest/progress/{job_id}")
        assert resp.status_code == 200, resp.text
        snap = resp.json()
        if snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.005)
    raise AssertionError("job never reached a terminal state")


# --------------------------------------------------------------------------- #
# Cache-key stability (pure — no client, no DB)
# --------------------------------------------------------------------------- #
def test_equal_requests_produce_equal_key() -> None:
    a = ib.RunRequest(**_body())
    b = ib.RunRequest(**_body())
    assert ib._intraday_cache_key(a) == ib._intraday_cache_key(b)


def test_use_cache_toggle_does_not_change_key() -> None:
    on = ib.RunRequest(**_body(use_cache=True))
    off = ib.RunRequest(**_body(use_cache=False))
    assert ib._intraday_cache_key(on) == ib._intraday_cache_key(off)


def test_compute_version_bump_changes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    req = ib.RunRequest(**_body())
    before = ib._intraday_cache_key(req)
    monkeypatch.setattr(ib, "INTRADAY_COMPUTE_VERSION", "9.9.9")
    after = ib._intraday_cache_key(req)
    assert before != after


def test_different_body_changes_key() -> None:
    a = ib.RunRequest(**_body(straddle_side="long"))
    b = ib.RunRequest(**_body(straddle_side="short"))
    assert ib._intraday_cache_key(a) != ib._intraday_cache_key(b)


def test_intraday_and_portfolio_caches_are_distinct_files() -> None:
    # Different sqlite filename so the two caches never share the LRU domain.
    assert "intraday_results" in ib._default_cache_path()
    assert ib._default_cache_path() != __import__(
        "tcg.core.api.portfolio", fromlist=["_default_cache_path"]
    )._default_cache_path()


# --------------------------------------------------------------------------- #
# /cache/get MISS never computes
# --------------------------------------------------------------------------- #
def test_cache_get_miss_returns_not_cached_and_never_computes(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    resp = client.post("/api/intraday-backtest/cache/get", json=_body())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cached": False}
    # The read path must never trigger a backtest.
    assert fake_run["called"] == 0
    assert ib._intraday_result_cache.count() == 0


def test_cache_status_miss(client: TestClient, fake_run: dict[str, Any]) -> None:
    resp = client.post("/api/intraday-backtest/cache/status", json=_body())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cached": False}
    assert fake_run["called"] == 0


# --------------------------------------------------------------------------- #
# Store-on-completion + subsequent read paths
# --------------------------------------------------------------------------- #
def test_completed_run_is_cached_and_served(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    # First run (use_cache default True): miss -> compute -> store.
    job_id = client.post(
        "/api/intraday-backtest/run-async", json=_body()
    ).json()["job_id"]
    final = _poll_until_terminal(client, job_id)
    assert final["status"] == "done"
    assert final["result"] == _PINNED_RESULT  # fresh serve: no from_cache marker
    assert fake_run["called"] == 1
    assert ib._intraday_result_cache.count() == 1

    # /cache/status now reports cached.
    status = client.post("/api/intraday-backtest/cache/status", json=_body())
    assert status.json() == {"cached": True}

    # /cache/get returns the full result + from_cache:true, still no recompute.
    got = client.post("/api/intraday-backtest/cache/get", json=_body())
    assert got.status_code == 200
    payload = got.json()
    assert payload["from_cache"] is True
    assert payload["aggregate"] == _PINNED_RESULT["aggregate"]
    assert payload["days"] == _PINNED_RESULT["days"]
    assert fake_run["called"] == 1  # get never computes


def test_run_async_hit_fast_path_skips_compute(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    # Prime the cache with a completed run.
    first = client.post("/api/intraday-backtest/run-async", json=_body()).json()["job_id"]
    _poll_until_terminal(client, first)
    assert fake_run["called"] == 1

    # A second identical run hits the cache: job is already 'done', no new compute.
    second_resp = client.post("/api/intraday-backtest/run-async", json=_body())
    assert second_resp.status_code == 200
    second = second_resp.json()["job_id"]
    final = _poll_until_terminal(client, second)
    assert final["status"] == "done"
    assert final["days_done"] == _EXPECTED_TOTAL
    assert final["total_days"] == _EXPECTED_TOTAL
    assert final["result"]["from_cache"] is True
    assert fake_run["called"] == 1  # NOT recomputed


# --------------------------------------------------------------------------- #
# use_cache=False bypasses the cache on both ends
# --------------------------------------------------------------------------- #
def test_use_cache_false_neither_reads_nor_writes(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    job_id = client.post(
        "/api/intraday-backtest/run-async", json=_body(use_cache=False)
    ).json()["job_id"]
    final = _poll_until_terminal(client, job_id)
    assert final["status"] == "done"
    assert fake_run["called"] == 1
    # Nothing was written.
    assert ib._intraday_result_cache.count() == 0

    # A second use_cache=False run also recomputes (no read either).
    job2 = client.post(
        "/api/intraday-backtest/run-async", json=_body(use_cache=False)
    ).json()["job_id"]
    _poll_until_terminal(client, job2)
    assert fake_run["called"] == 2
    assert ib._intraday_result_cache.count() == 0


def test_use_cache_false_still_serves_a_prior_cached_entry_via_run_only_when_true(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    # Populate the cache via a True run.
    j1 = client.post("/api/intraday-backtest/run-async", json=_body()).json()["job_id"]
    _poll_until_terminal(client, j1)
    assert fake_run["called"] == 1
    assert ib._intraday_result_cache.count() == 1

    # A use_cache=False run must NOT read the cache: it recomputes fresh.
    j2 = client.post(
        "/api/intraday-backtest/run-async", json=_body(use_cache=False)
    ).json()["job_id"]
    _poll_until_terminal(client, j2)
    assert fake_run["called"] == 2


# --------------------------------------------------------------------------- #
# /cache/clear empties the store
# --------------------------------------------------------------------------- #
def test_cache_clear_empties_store_and_next_status_misses(
    client: TestClient, fake_run: dict[str, Any]
) -> None:
    # Populate the cache via a completed run.
    job_id = client.post(
        "/api/intraday-backtest/run-async", json=_body()
    ).json()["job_id"]
    _poll_until_terminal(client, job_id)
    assert fake_run["called"] == 1
    assert ib._intraday_result_cache.count() == 1

    status = client.post("/api/intraday-backtest/cache/status", json=_body())
    assert status.json() == {"cached": True}

    r = client.post("/api/intraday-backtest/cache/clear")
    assert r.status_code == 200, r.text
    assert r.json() == {"cleared": True}
    assert ib._intraday_result_cache.count() == 0

    status_after = client.post("/api/intraday-backtest/cache/status", json=_body())
    assert status_after.json() == {"cached": False}

    # A subsequent /cache/get must miss (recompute is only triggered by run-async,
    # which is out of scope here) and never surface the cleared entry.
    got_after = client.post("/api/intraday-backtest/cache/get", json=_body())
    assert got_after.json() == {"cached": False}
    assert fake_run["called"] == 1  # clear + status/get never compute
