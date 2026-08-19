"""Run-level tests for F4.1 laddered multi-entry: the per-entry loop, sizing,
max-concurrent, the serializer (per-rung rows + retained day aggregate), side
uniformity, and per-entry cost/hedge — driven through the REAL ``run_backtest``
pipeline against a deterministic in-memory reader (NO dwh).

The strongest independence proof runs the SAME reader three ways: ladder OFF at
10:00 (P1), ladder OFF at 11:00 (P2), and a 2-rung ladder {10:00, 11:00}; the
laddered day aggregate MUST equal P1 + P2 (equal-contracts) — i.e. the rungs are
exactly the two single straddles summed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from tcg.core.api import intraday_backtest as ib
from tcg.types.intraday import IntradayBar

_DAY = date(2025, 2, 3)  # a Monday in-window; 0DTE expiry == the day
_ET_OFFSET = 5  # winter ET->UTC


def _utc(h: int, m: int) -> datetime:
    return datetime(2025, 2, 3, h + _ET_OFFSET, m, tzinfo=timezone.utc)


def _minute_bars(
    start_h: int, start_m: int, end_h: int, end_m: int, price_fn
) -> list[IntradayBar]:
    """One bar per minute over [start, end] (ET clock), price from ``price_fn``
    (minutes-since-09:30 -> (mid, bid, ask))."""
    out: list[IntradayBar] = []
    t = _utc(start_h, start_m)
    end = _utc(end_h, end_m)
    open_ts = _utc(9, 30)
    while t <= end:
        mins = (t - open_ts).total_seconds() / 60.0
        mid, bid, ask = price_fn(mins)
        out.append(IntradayBar(ts=t, price=mid, bid=bid, ask=ask,
                               bid_size=50.0, ask_size=50.0))
        t += timedelta(minutes=1)
    return out


class _FakeReader:
    """Deterministic intraday reader. One 0DTE expiry == the trading day, one ATM
    strike (5000), a flat ES path, and a declining option-mark path so a later
    entry fills at a different price (=> P1 != P2)."""

    def __init__(self, *, quotes: bool = True) -> None:
        self.quotes = quotes
        self.option_calls: list[int] = []

    async def list_option_roots(self) -> list[dict[str, Any]]:
        return [{"object_id": 100, "symbol": "ES"}]

    async def list_expirations(self, oids: Any, start: Any) -> list[Any]:
        return [(100, _DAY)]

    async def get_option_tick_size(self) -> float:
        return 0.05

    async def get_es_future_tick_size(self) -> float:
        return 0.25

    async def fetch_es_future_1m(self, win_start, win_end, on_or_after=None):
        bars = _minute_bars(9, 28, 15, 47, lambda _m: (5000.0, 4999.75, 5000.25))
        return [b for b in bars if win_start <= b.ts <= win_end]

    async def list_strikes(self, oid: int, expiry: date) -> list[float]:
        return [5000.0]

    async def get_option_contract_id(self, oid, expiry, strike, kind) -> int:
        return 1 if kind == "call" else 2

    async def fetch_option_1m(self, cid: int, win_start, win_end):
        self.option_calls.append(cid)
        # Declining marks (theta): mid = 30 - 0.02*minutes; a small spread when
        # two-sided quotes are enabled, else last-trade-only (no bid/ask).
        def price_fn(m: float):
            mid = 30.0 - 0.02 * m
            if self.quotes:
                return (mid, mid - 0.10, mid + 0.10)
            return (mid, None, None)

        bars = _minute_bars(9, 28, 15, 47, price_fn)
        return [b for b in bars if win_start <= b.ts <= win_end]


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "start_date": "2025-02-03",
        "end_date": "2025-02-03",
        "expiry_mode": "0DTE",
        "entry": {"time": "10:00"},
        "exit": {"time": "15:45"},
        "hedge": {"enabled": False},
    }
    base.update(over)
    return base


async def _run(reader: _FakeReader, **over: Any) -> dict[str, Any]:
    req = ib.RunRequest(**_body(**over))
    return await ib.run_backtest(reader, req, daily_reader=None)


def _day0(result: dict[str, Any]) -> dict[str, Any]:
    return result["days"][0]


# --------------------------------------------------------------------------- #
# Per-entry independence: laddered day == sum of the single straddles
# --------------------------------------------------------------------------- #
async def test_ladder_day_is_sum_of_independent_single_entries() -> None:
    r1 = _day0(await _run(_FakeReader(), entry={"time": "10:00"}))
    r2 = _day0(await _run(_FakeReader(), entry={"time": "11:00"}))
    p1 = r1["pnl"]["total_pnl_usd"]
    p2 = r2["pnl"]["total_pnl_usd"]
    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert p1 != p2  # a later entry fills differently -> genuinely independent

    lad = _day0(
        await _run(
            _FakeReader(),
            entry={"time": "10:00"},
            ladder={"enabled": True, "interval_minutes": 60,
                    "last_entry_cutoff": "11:00"},
        )
    )
    # 2 rungs {10:00, 11:00}, equal-contracts contracts=1 => aggregate == P1 + P2.
    assert len(lad["entries"]) == 2
    assert lad["pnl"]["total_pnl_usd"] == pytest.approx(p1 + p2)
    # Day aggregate == sum of the per-rung weighted contributions (req 5).
    assert lad["pnl"]["total_pnl_usd"] == pytest.approx(
        sum(e["weighted_pnl_usd"] for e in lad["entries"])
    )
    # Per-rung raw pnl matches the standalone single-entry sims.
    assert lad["entries"][0]["pnl"]["total_pnl_usd"] == pytest.approx(p1)
    assert lad["entries"][1]["pnl"]["total_pnl_usd"] == pytest.approx(p2)


# --------------------------------------------------------------------------- #
# Retained day aggregate (NON-NEGOTIABLE): shape A1/A2/A3 rely on
# --------------------------------------------------------------------------- #
async def test_ladder_day_aggregate_row_keeps_shape() -> None:
    lad = _day0(
        await _run(
            _FakeReader(),
            ladder={"enabled": True, "interval_minutes": 60,
                    "last_entry_cutoff": "12:00"},
        )
    )
    # One-row-per-day aggregate with the exact keys weekday/regime/event views use.
    assert lad["date"] == "2025-02-03"
    assert lad["status"] == "ok"
    for k in ("total_pnl_usd", "total_pnl_pts", "option_pnl_pts", "hedge_pnl_pts"):
        assert isinstance(lad["pnl"][k], float)
    # usd == pts * multiplier invariant holds on the aggregate.
    assert lad["pnl"]["total_pnl_usd"] == pytest.approx(lad["pnl"]["total_pnl_pts"] * 50.0)
    # The run-level aggregate counts the laddered day as ONE traded day.
    assert lad["entries"]  # per-rung detail present alongside the aggregate


async def test_run_aggregate_totals_match_day_rows() -> None:
    result = await _run(
        _FakeReader(),
        ladder={"enabled": True, "interval_minutes": 60, "last_entry_cutoff": "12:00"},
    )
    day_usd = sum(
        d["pnl"]["total_pnl_usd"] for d in result["days"] if d.get("pnl")
    )
    assert result["aggregate"]["total_pnl_usd"] == pytest.approx(day_usd)
    assert result["aggregate"]["n_traded"] == 1  # one laddered day == one row


# --------------------------------------------------------------------------- #
# Ladder OFF byte-identity (no ``entries`` key at all)
# --------------------------------------------------------------------------- #
async def test_ladder_off_day_has_no_entries_key() -> None:
    r = _day0(await _run(_FakeReader()))
    assert "entries" not in r
    assert r["status"] == "ok"


# --------------------------------------------------------------------------- #
# Sizing: equal_contracts vs equal_notional
# --------------------------------------------------------------------------- #
async def test_equal_contracts_uses_constant_weight() -> None:
    lad = _day0(
        await _run(
            _FakeReader(),
            ladder={"enabled": True, "interval_minutes": 60,
                    "last_entry_cutoff": "11:00",
                    "sizing": {"mode": "equal_contracts", "contracts": 3.0}},
        )
    )
    assert [e["contracts"] for e in lad["entries"]] == [3.0, 3.0]
    # Each rung's contribution is contracts * raw pnl.
    for e in lad["entries"]:
        assert e["weighted_pnl_usd"] == pytest.approx(3.0 * e["pnl"]["total_pnl_usd"])


async def test_equal_notional_equalizes_premium_notional() -> None:
    lad = _day0(
        await _run(
            _FakeReader(),
            ladder={"enabled": True, "interval_minutes": 60,
                    "last_entry_cutoff": "11:00",
                    "sizing": {"mode": "equal_notional", "contracts": 1.0}},
        )
    )
    e0, e1 = lad["entries"]
    mult = 50.0
    # contracts_i * straddle_price_i * mult is EQUAL across rungs (common notional).
    n0 = e0["contracts"] * e0["entry"]["straddle_price"] * mult
    n1 = e1["contracts"] * e1["entry"]["straddle_price"] * mult
    assert n0 == pytest.approx(n1)
    # First traded rung is the auto reference => weight == contracts (1.0).
    assert e0["contracts"] == pytest.approx(1.0)
    # A cheaper (later) straddle gets MORE contracts to match the notional.
    assert e1["entry"]["straddle_price"] < e0["entry"]["straddle_price"]
    assert e1["contracts"] > e0["contracts"]
    # Day total == sum of weighted contributions.
    assert lad["pnl"]["total_pnl_usd"] == pytest.approx(
        e0["weighted_pnl_usd"] + e1["weighted_pnl_usd"]
    )


# --------------------------------------------------------------------------- #
# max_concurrent caps entries (hold-to-settlement consequence)
# --------------------------------------------------------------------------- #
async def test_max_concurrent_caps_opened_rungs() -> None:
    lad = _day0(
        await _run(
            _FakeReader(),
            ladder={"enabled": True, "interval_minutes": 60,
                    "last_entry_cutoff": "13:00", "max_concurrent": 2},
        )
    )
    # Schedule is 10/11/12/13:00 (4 rungs) but only 2 may be OPEN at once, and
    # (hold-to-settlement) none close intraday => rungs 3 & 4 are capped.
    statuses = [e["status"] for e in lad["entries"]]
    assert statuses == ["ok", "ok", "skipped", "skipped"]
    assert lad["entries"][2]["skip_reason"] == "max_concurrent"
    # Aggregate == sum of the two rungs that actually opened.
    opened = [e for e in lad["entries"] if e["status"] == "ok"]
    assert lad["pnl"]["total_pnl_usd"] == pytest.approx(
        sum(e["weighted_pnl_usd"] for e in opened)
    )


# --------------------------------------------------------------------------- #
# Regime side applied uniformly across a day's rungs
# --------------------------------------------------------------------------- #
async def test_side_is_uniform_across_rungs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_sides: list[str] = []
    real = ib.simulate_day

    def _spy(**kw: Any):
        seen_sides.append(kw["side"])
        return real(**kw)

    monkeypatch.setattr(ib, "simulate_day", _spy)
    await _run(
        _FakeReader(),
        straddle_side="short",
        ladder={"enabled": True, "interval_minutes": 60, "last_entry_cutoff": "12:00"},
    )
    # 3 rungs, all the SAME (day-level) side.
    assert len(seen_sides) == 3
    assert set(seen_sides) == {"short"}


# --------------------------------------------------------------------------- #
# Cost + hedge apply PER entry
# --------------------------------------------------------------------------- #
async def test_cost_applies_per_rung() -> None:
    lad = _day0(
        await _run(
            _FakeReader(),
            cost={"enabled": True, "fallback_cost_pts": 0.0},
            ladder={"enabled": True, "interval_minutes": 60, "last_entry_cutoff": "11:00"},
        )
    )
    # Each rung pays its own half-spread cost (>0 with two-sided quotes).
    for e in lad["entries"]:
        assert e["pnl"]["cost_pts"] > 0.0
    # Day aggregate cost == sum of per-rung costs (equal-contracts weight 1).
    assert lad["pnl"]["cost_pts"] == pytest.approx(
        sum(e["pnl"]["cost_pts"] for e in lad["entries"])
    )


async def test_hedge_state_is_per_rung() -> None:
    lad = _day0(
        await _run(
            _FakeReader(),
            hedge={"enabled": True, "triggers": {"interval_minutes": 30}},
            ladder={"enabled": True, "interval_minutes": 60, "last_entry_cutoff": "11:00"},
        )
    )
    # Each rung ran its OWN hedge lifecycle (independent hedge trades).
    for e in lad["entries"]:
        assert isinstance(e["hedge_trades"], list)
        assert len(e["hedge_trades"]) >= 1
