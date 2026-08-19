"""Core-layer tests for the F2.2 regime -> per-day SIDE plumbing.

Deterministic, NO dwh. The daily-signal FETCH (``_fetch_regime_series``) and the
per-day option simulation (``_process_day``) are stubbed so the wiring under test
is exactly: run_backtest -> resolve as-of decisions -> thread the resolved side
into simulate_day / skip flat days / emit the per-day readout. Covers:

* long/short days set the RIGHT side into ``_process_day`` (multi-day mixed);
* a flat decision SKIPS the day like an exclude (no _process_day call, not in
  total_days, no warning), and still emits a regime readout explaining WHY;
* NO look-ahead: a divergent signal DATED day D never changes D's side;
* default-off regression: side_mode off => every day uses the static side, NO
  ``regime`` key, and the cache hash is identical to a regime-absent body;
* cache participation when side_mode is on (gates/thresholds change the key);
* fetch failure DEGRADES to the static side (fallback), never a skip/crash;
* schema round-trip of the new decision fields + the 3-window ladder validator.
"""

from __future__ import annotations

from typing import Any

import pytest

from tcg.core.api import intraday_backtest as ib
from tcg.types.intraday import DayPnl, DayResult


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
class _NoTradeReader:
    """IntradayV2Reader stub: the pre-loop calls succeed; _process_day is
    monkeypatched so no real per-day dwh access happens."""

    async def list_option_roots(self) -> list[dict[str, Any]]:
        return []

    async def list_expirations(self, oids: Any, start: Any) -> list[Any]:
        return []

    async def get_option_tick_size(self) -> float:
        return 0.05

    async def get_es_future_tick_size(self) -> float:
        return 0.25


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"start_date": "2025-02-03", "end_date": "2025-02-05"}
    base.update(over)
    return base


def _ok_pnl() -> DayPnl:
    return DayPnl(
        option_pnl_pts=1.0,
        hedge_pnl_pts=0.0,
        total_pnl_pts=1.0,
        total_pnl_usd=50.0,
    )


def _install_process_recorder(monkeypatch: pytest.MonkeyPatch) -> dict[int, str]:
    """Replace _process_day with a recorder of ``{date_int: side}`` that returns
    a traded (``ok``) DayResult, so the resolved per-day side is observable."""
    seen: dict[int, str] = {}

    async def _fake_process_day(reader, req, plan, all_exps, exp_to_objs,
                                tick_size, es_tick, side):  # noqa: ANN001
        seen[plan.date_int] = side
        return DayResult(date=plan.date_int, status="ok", pnl=_ok_pnl())

    monkeypatch.setattr(ib, "_process_day", _fake_process_day)
    return seen


def _install_signal_map(
    monkeypatch: pytest.MonkeyPatch,
    rv_by_date: dict[int, dict[str, float | None]],
    passthrough: dict[str, dict[int, float]] | None = None,
) -> None:
    """Stub _fetch_regime_series so the FULL daily map + real resolver run over
    exactly the injected signals (no RV math, no dwh)."""
    pt = passthrough or {"vvix": {}}

    async def _fake_series(daily_reader, req, day_dates):  # noqa: ANN001
        return rv_by_date, pt, req.regime.rv_windows

    monkeypatch.setattr(ib, "_fetch_regime_series", _fake_series)


# --------------------------------------------------------------------------- #
# Per-day side plumbing: long/short/flat resolved as-of the PRIOR daily close
# --------------------------------------------------------------------------- #
async def test_multi_day_mixed_regimes_thread_the_right_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Daily signals (keyed by DAILY date). 20250203..05 are the backtest days;
    # each day is decided as-of the latest daily close STRICTLY before it.
    rv = {
        20250131: {"h20": 0.20, "h30": 0.18, "h100": 0.15},  # backward -> long
        20250203: {"h20": 0.12, "h30": 0.16, "h100": 0.18},  # not back  -> short
        20250204: {"h20": 0.03, "h30": 0.03, "h100": 0.03},  # < floor   -> flat
        20250205: {"h20": 0.25, "h30": 0.20, "h100": 0.15},  # LOOK-AHEAD trap
    }
    _install_signal_map(monkeypatch, rv)
    seen = _install_process_recorder(monkeypatch)

    req = ib.RunRequest(
        **_body(
            straddle_side="short",
            regime={"side_mode": "regime_driven", "extremely_low_h20": 0.05},
        )
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=object())

    # 02-03 as-of 01-31 (long); 02-04 as-of 02-03 (short); 02-05 as-of 02-04 (flat).
    assert seen == {20250203: "long", 20250204: "short"}
    # The flat day was NOT processed (no side threaded) and is emitted as skipped.
    feb5 = next(d for d in result["days"] if d["date"] == "2025-02-05")
    assert feb5["status"] == "skipped"
    assert feb5["skip_reason"] == "regime_flat"
    # Readout explains WHY each day is long/short/flat + the as-of date used.
    feb3 = next(d for d in result["days"] if d["date"] == "2025-02-03")
    assert feb3["regime"] == {
        "state": "hvol_on", "side": "long", "asof": 20250131,
        "gate": None,
        "signals": {"h20": 0.20, "h30": 0.18, "h100": 0.15, "vvix": None},
    }
    assert feb5["regime"]["side"] == "flat"
    assert feb5["regime"]["state"] == "extremely_low"
    assert feb5["regime"]["asof"] == 20250204
    # A regime flat is NOT a data-quality warning.
    assert result["warnings"] == []
    # Aggregate counts only the two traded days.
    assert result["aggregate"]["n_traded"] == 2


async def test_no_look_ahead_day_d_uses_prior_close_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 02-03's own signal is strongly HVOL-OFF (would be short); the PRIOR close
    # (01-31) is backwardated (long). The decision must stay LONG.
    rv = {
        20250131: {"h20": 0.20, "h30": 0.18, "h100": 0.15},  # prior -> long
        20250203: {"h20": 0.05, "h30": 0.18, "h100": 0.25},  # same-day trap -> short
    }
    _install_signal_map(monkeypatch, rv)
    seen = _install_process_recorder(monkeypatch)

    req = ib.RunRequest(
        **{"start_date": "2025-02-03", "end_date": "2025-02-03",
           "straddle_side": "short",
           "regime": {"side_mode": "regime_driven"}}
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=object())
    assert seen[20250203] == "long"  # prior close wins, NOT 02-03's own signal
    feb3 = next(d for d in result["days"] if d["date"] == "2025-02-03")
    assert feb3["regime"]["asof"] == 20250131


async def test_vvix_gate_vetoes_a_day_to_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rv = {
        20250131: {"h20": 0.20, "h30": 0.18, "h100": 0.15},  # long base
        20250203: {"h20": 0.20, "h30": 0.18, "h100": 0.15},
    }
    # VVIX high on the PRIOR close (01-31) -> gate vetoes 02-03 to flat.
    _install_signal_map(monkeypatch, rv, passthrough={"vvix": {20250131: 130.0}})
    seen = _install_process_recorder(monkeypatch)

    req = ib.RunRequest(
        **{"start_date": "2025-02-03", "end_date": "2025-02-03",
           "straddle_side": "short",
           "regime": {
               "side_mode": "regime_driven",
               "gates": [{"enabled": True, "signal": "vvix",
                          "above": 120.0, "action": "flat"}],
           }}
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=object())
    assert 20250203 not in seen  # flat -> not traded
    feb3 = next(d for d in result["days"] if d["date"] == "2025-02-03")
    assert feb3["regime"]["side"] == "flat"
    assert feb3["regime"]["gate"] == "vvix"
    assert feb3["regime"]["state"] == "hvol_on"  # underlying regime preserved


async def test_fetch_failure_degrades_to_static_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(daily_reader, req, day_dates):  # noqa: ANN001
        raise RuntimeError("dwh unreachable")

    monkeypatch.setattr(ib, "_fetch_regime_series", _boom)
    seen = _install_process_recorder(monkeypatch)

    req = ib.RunRequest(
        **{"start_date": "2025-02-03", "end_date": "2025-02-04",
           "straddle_side": "short",
           "regime": {"side_mode": "regime_driven"}}
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=object())
    # Every day trades the STATIC side (fallback), never a skip.
    assert seen == {20250203: "short", 20250204: "short"}
    feb3 = next(d for d in result["days"] if d["date"] == "2025-02-03")
    assert feb3["regime"]["state"] == "fallback"
    assert feb3["regime"]["side"] == "short"
    assert feb3["regime"]["asof"] is None


# --------------------------------------------------------------------------- #
# Default-off regression: byte-identity + cache hash
# --------------------------------------------------------------------------- #
async def test_side_mode_off_uses_static_side_and_no_regime_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # side_mode off must not fetch decisions, must thread the static side, and
    # must add NO regime key (days byte-identical to the pre-feature baseline).
    def _fail(*a: Any, **k: Any) -> None:
        raise AssertionError("_fetch_regime_series must NOT be called when off")

    monkeypatch.setattr(ib, "_fetch_regime_series", _fail)
    seen = _install_process_recorder(monkeypatch)

    req = ib.RunRequest(**_body(straddle_side="long"))  # regime default off
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=object())
    assert set(seen.values()) == {"long"}  # static side for every day
    assert all("regime" not in d for d in result["days"])


def test_cache_key_off_equals_regime_absent_even_with_decision_fields() -> None:
    # A regime block that is INERT (side_mode off, emit off) — even carrying
    # non-default thresholds/gates — hashes identically to a regime-absent body.
    from tcg.core.cache import canonical_hash

    inert = ib.RunRequest(
        **_body(
            regime={
                "side_mode": "off",
                "hvol_tolerance": 0.3,
                "extremely_low_h20": 0.9,
                "gates": [{"enabled": True, "signal": "vvix",
                           "above": 200.0, "action": "flat"}],
            }
        )
    )
    absent = ib.RunRequest(**_body())
    payload = ib._strip_use_cache(absent.model_dump(mode="json"))
    payload.pop("regime", None)
    payload.pop("allowlist", None)  # F3.2 default-off block also stripped
    pre_feature = canonical_hash(
        {"_cv": ib.INTRADAY_COMPUTE_VERSION, "body": payload}
    )
    assert ib._intraday_cache_key(inert) == pre_feature
    assert ib._intraday_cache_key(inert) == ib._intraday_cache_key(absent)


def test_cache_key_regime_driven_participates() -> None:
    off = ib.RunRequest(**_body())
    on = ib.RunRequest(**_body(regime={"side_mode": "regime_driven"}))
    assert ib._intraday_cache_key(off) != ib._intraday_cache_key(on)
    # A threshold change changes the key.
    on2 = ib.RunRequest(
        **_body(regime={"side_mode": "regime_driven", "extremely_low_h20": 0.05})
    )
    assert ib._intraday_cache_key(on) != ib._intraday_cache_key(on2)
    # A gate change changes the key.
    on3 = ib.RunRequest(
        **_body(regime={"side_mode": "regime_driven",
                        "gates": [{"enabled": True, "signal": "vvix",
                                   "above": 120.0, "action": "flat"}]})
    )
    assert ib._intraday_cache_key(on) != ib._intraday_cache_key(on3)


# --------------------------------------------------------------------------- #
# Schema round-trip + validation
# --------------------------------------------------------------------------- #
def test_decision_fields_round_trip() -> None:
    req = ib.RunRequest(
        **_body(regime={
            "side_mode": "regime_driven",
            "hvol_tolerance": 0.05,
            "extremely_low_h20": 0.06,
            "gates": [{"enabled": True, "signal": "vix1d",
                       "above": 20.0, "action": "short"}],
        })
    )
    dumped = req.model_dump(mode="json")
    reparsed = ib.RunRequest(**dumped)
    assert reparsed.regime.side_mode == "regime_driven"
    assert reparsed.regime.hvol_tolerance == 0.05
    assert reparsed.regime.extremely_low_h20 == 0.06
    assert reparsed.regime.gates[0].signal == "vix1d"
    assert reparsed.regime.gates[0].action == "short"


def test_regime_defaults_side_off() -> None:
    req = ib.RunRequest(**_body())
    assert req.regime.side_mode == "off"
    assert req.regime.hvol_tolerance == 0.0
    assert req.regime.extremely_low_h20 == 0.0
    assert req.regime.gates == []
    assert req.regime.is_active is False


def test_regime_driven_requires_three_windows() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        ib.RunRequest(
            **_body(regime={"side_mode": "regime_driven", "rv_windows": [20, 30]})
        )
    # off with a non-3 window list is fine (ladder never consulted).
    ok = ib.RunRequest(**_body(regime={"side_mode": "off", "rv_windows": [20, 30]}))
    assert ok.regime.rv_windows == [20, 30]


def test_negative_thresholds_rejected() -> None:
    with pytest.raises(Exception):
        ib.RunRequest(**_body(regime={"hvol_tolerance": -0.1}))
    with pytest.raises(Exception):
        ib.RunRequest(**_body(regime={"extremely_low_h20": -1.0}))
