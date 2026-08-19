"""Core-layer tests for the F2.1 vol-regime signal provider.

Deterministic, NO dwh. Covers:
* pure per-day assembly join (RV + VVIX by date; missing VVIX -> None; a THIRD
  passthrough symbol — the VIX1D drop-in — works with NO structural change);
* the async ``_fetch_regime_signals`` over a STUBBED DailySeriesReader (dwh is
  unreachable — see PROBLEMS.md);
* the serializer: emit ON attaches ``regime``; OFF leaves the day byte-identical;
* ``run_backtest`` end-to-end with a stub daily reader (emit ON attaches signals;
  OFF fetches nothing and adds no key);
* cache-key: OFF hashes identically regardless of inert sub-config AND equals a
  regime-absent body; ON changes the key; a version bump changes the key.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from tcg.core.api import intraday_backtest as ib
from tcg.types.daily_series import DailySeries, DailySeriesPoint


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
class _StubDailyReader:
    """Canned DailySeriesReader: maps symbol -> {date_int: value}. Records the
    (symbol, start, end, field) of every ``read_series`` call so the fetch's
    date-range/lookback behavior is assertable without a database."""

    def __init__(self, data: dict[str, dict[int, float]]) -> None:
        self._data = data
        self.calls: list[dict[str, Any]] = []

    async def read_series(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        field: str = "close",
    ) -> DailySeries:
        self.calls.append(
            {"symbol": symbol, "start": start, "end": end, "field": field}
        )
        series = self._data.get(symbol, {})
        points = tuple(
            DailySeriesPoint(date=d, value=v) for d, v in sorted(series.items())
        )
        return DailySeries(symbol=symbol, field=field, points=points)


class _NoTradeReader:
    """Minimal IntradayV2Reader stub: no roots/expiries => every day skips. The
    regime signals still attach (skipped days carry a date)."""

    async def list_option_roots(self) -> list[dict[str, Any]]:
        return []

    async def list_expirations(self, oids: Any, start: Any) -> list[Any]:
        return []

    async def get_option_tick_size(self) -> float:
        return 0.05

    async def get_es_future_tick_size(self) -> float:
        return 0.25

    async def fetch_es_future_1m(self, *a: Any, **k: Any) -> list[Any]:
        return []


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"start_date": "2025-02-03", "end_date": "2025-02-07"}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Pure assembly: build_regime_signal_map
# --------------------------------------------------------------------------- #
def test_build_map_joins_rv_and_vvix_by_date() -> None:
    rv = {
        20250203: {"h20": 0.15, "h30": 0.16},
        20250204: {"h20": 0.17, "h30": 0.18},
    }
    passthrough = {"vvix": {20250203: 95.0, 20250204: 96.0}}
    out = ib.build_regime_signal_map(
        [20250203, 20250204], rv, passthrough, windows=[20, 30]
    )
    assert out[20250203] == {"h20": 0.15, "h30": 0.16, "vvix": 95.0}
    assert out[20250204] == {"h20": 0.17, "h30": 0.18, "vvix": 96.0}


def test_missing_vvix_date_is_none_not_crash() -> None:
    rv = {20250203: {"h20": 0.15}}
    passthrough = {"vvix": {}}  # VVIX absent for the date
    out = ib.build_regime_signal_map([20250203], rv, passthrough, windows=[20])
    assert out[20250203] == {"h20": 0.15, "vvix": None}


def test_missing_rv_date_yields_none_keys() -> None:
    # A backtest date with no RV entry (e.g. a holiday not in the close series).
    out = ib.build_regime_signal_map(
        [20250203], rv_by_date={}, passthrough_by_name={"vvix": {}}, windows=[20, 30]
    )
    assert out[20250203] == {"h20": None, "h30": None, "vvix": None}


def test_third_passthrough_symbol_is_structural_no_change() -> None:
    # The VIX1D drop-in: a NEW passthrough name flows through the SAME loop with
    # no signature/logic change — proves F2.3 is additive.
    rv = {20250203: {"h20": 0.15}}
    passthrough = {"vvix": {20250203: 95.0}, "vix1d": {20250203: 12.5}}
    out = ib.build_regime_signal_map([20250203], rv, passthrough, windows=[20])
    assert out[20250203] == {"h20": 0.15, "vvix": 95.0, "vix1d": 12.5}


# --------------------------------------------------------------------------- #
# Async fetch over a stubbed reader
# --------------------------------------------------------------------------- #
async def test_fetch_regime_signals_computes_rv_and_joins_vvix() -> None:
    # A rising IND_SP_500 with enough history to warm up a small window, plus
    # VVIX only on the two backtest days.
    sp = {20250101 + i: 100.0 + i for i in range(40)}  # 20250101..20250140-ish ints
    # NOTE: these are raw YYYYMMDD-shaped ints only for keying; RV cares about
    # order + values, not calendar validity. Use real dates for day_dates below.
    real_sp = {
        int(d.strftime("%Y%m%d")): 100.0 + i
        for i, d in enumerate(
            [date(2025, 1, 2), date(2025, 1, 3)]
            + [date(2025, 1, d) for d in range(6, 32)]
            + [date(2025, 2, d) for d in range(3, 8)]
        )
    }
    vvix = {20250203: 95.0, 20250204: 96.0, 20250205: 97.0}
    reader = _StubDailyReader({"IND_SP_500": real_sp, "IND_VVIX": vvix})
    req = ib.RunRequest(**_body(regime={"emit_signals": True, "rv_windows": [2, 3]}))
    day_dates = [20250203, 20250204, 20250205]

    out = await ib._fetch_regime_signals(reader, req, day_dates)

    # RV present (enough history) and VVIX joined for each day.
    for d in day_dates:
        assert set(out[d]) == {"h2", "h3", "vvix"}
        assert out[d]["h2"] is not None
    assert out[20250203]["vvix"] == 95.0
    # Two reads: IND_SP_500 AND VVIX both with a lookback START before the range.
    # (F2.2 change: VVIX now also reads with lookback so the no-look-ahead as-of
    # side decision has a PRIOR daily close available for the first backtest day.
    # Extra VVIX history is harmless for the F2.1 display path, which only reads
    # the backtest-day entries.)
    symbols = [c["symbol"] for c in reader.calls]
    assert symbols == ["IND_SP_500", "IND_VVIX"]
    sp_call = reader.calls[0]
    assert sp_call["start"] < date(2025, 2, 3)  # lookback applied
    assert reader.calls[1]["start"] < date(2025, 2, 3)  # VVIX lookback too (F2.2)
    assert reader.calls[1]["end"] == date(2025, 2, 5)  # end still the range end


# --------------------------------------------------------------------------- #
# Serializer: ON attaches, OFF byte-identical
# --------------------------------------------------------------------------- #
def _skipped_day() -> Any:
    from tcg.types.intraday import DayResult

    return DayResult(date=20250203, status="skipped", skip_reason="no_expiry")


def test_serialize_day_off_has_no_regime_key() -> None:
    out = ib._serialize_day(_skipped_day())
    assert "regime" not in out


def test_serialize_day_on_attaches_regime() -> None:
    rmap = {20250203: {"h20": 0.15, "h30": 0.16, "h100": None, "vvix": 95.0}}
    out = ib._serialize_day(_skipped_day(), rmap)
    assert out["regime"] == {"h20": 0.15, "h30": 0.16, "h100": None, "vvix": 95.0}


def test_serialize_day_off_is_byte_identical_to_pre_feature() -> None:
    # Passing None must produce EXACTLY the dict a no-regime serializer made.
    a = ib._serialize_day(_skipped_day())
    b = ib._serialize_day(_skipped_day(), None)
    assert a == b
    assert "regime" not in a


# --------------------------------------------------------------------------- #
# run_backtest end-to-end (stub readers, no dwh)
# --------------------------------------------------------------------------- #
async def test_run_backtest_emit_off_no_regime_and_no_daily_fetch() -> None:
    daily = _StubDailyReader({})
    req = ib.RunRequest(**_body())  # regime default off
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=daily)
    assert all("regime" not in d for d in result["days"])
    assert daily.calls == []  # emit off => NO daily fetch


async def test_run_backtest_emit_on_attaches_signals() -> None:
    real_sp = {
        int(d.strftime("%Y%m%d")): 100.0 + i
        for i, d in enumerate(
            [date(2025, 1, d) for d in range(2, 32)]
            + [date(2025, 2, d) for d in range(3, 8)]
        )
    }
    vvix = {20250203: 95.0, 20250204: 96.0}
    daily = _StubDailyReader({"IND_SP_500": real_sp, "IND_VVIX": vvix})
    req = ib.RunRequest(
        **_body(regime={"emit_signals": True, "rv_windows": [2, 3]})
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=daily)
    for d in result["days"]:
        assert "regime" in d
        assert set(d["regime"]) == {"h2", "h3", "vvix"}
    # A day with VVIX present carries it.
    feb3 = next(d for d in result["days"] if d["date"] == "2025-02-03")
    assert feb3["regime"]["vvix"] == 95.0


async def test_run_backtest_emit_on_without_reader_emits_nulls() -> None:
    req = ib.RunRequest(
        **_body(regime={"emit_signals": True, "rv_windows": [20, 30, 100]})
    )
    result = await ib.run_backtest(_NoTradeReader(), req, daily_reader=None)
    for d in result["days"]:
        assert d["regime"] == {"h20": None, "h30": None, "h100": None, "vvix": None}


# --------------------------------------------------------------------------- #
# Cache-key participation
# --------------------------------------------------------------------------- #
def test_cache_key_off_ignores_inert_regime_subconfig() -> None:
    a = ib.RunRequest(**_body())  # regime default off
    b = ib.RunRequest(
        **_body(
            regime={
                "emit_signals": False,
                "rv_windows": [5, 7],
                "sp500_symbol": "OTHER",
                "vvix_symbol": "OTHER",
            }
        )
    )
    assert ib._intraday_cache_key(a) == ib._intraday_cache_key(b)


def test_cache_key_off_equals_pre_feature_regime_absent_body() -> None:
    # The exact hash a pre-F2.1 body (no ``regime`` key at all) would produce:
    # strip use_cache + regime from the dump and hash with the SAME salt.
    from tcg.core.cache import canonical_hash

    req = ib.RunRequest(**_body())  # regime default off
    payload = ib._strip_use_cache(req.model_dump(mode="json"))
    payload.pop("regime", None)  # pre-feature body has no regime key
    pre_feature = canonical_hash(
        {"_cv": ib.INTRADAY_COMPUTE_VERSION, "body": payload}
    )
    assert ib._intraday_cache_key(req) == pre_feature


def test_cache_key_on_participates() -> None:
    off = ib.RunRequest(**_body())
    on = ib.RunRequest(**_body(regime={"emit_signals": True}))
    assert ib._intraday_cache_key(off) != ib._intraday_cache_key(on)
    # And ON windows/symbols DO change the key.
    on2 = ib.RunRequest(**_body(regime={"emit_signals": True, "rv_windows": [10]}))
    assert ib._intraday_cache_key(on) != ib._intraday_cache_key(on2)


def test_cache_key_version_bump_changes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    req = ib.RunRequest(**_body(regime={"emit_signals": True}))
    before = ib._intraday_cache_key(req)
    monkeypatch.setattr(ib, "INTRADAY_COMPUTE_VERSION", "9.9.9")
    assert before != ib._intraday_cache_key(req)


def test_regime_defaults_off() -> None:
    req = ib.RunRequest(**_body())
    assert req.regime.emit_signals is False
    assert req.regime.rv_windows == [20, 30, 100]
    assert req.regime.sp500_symbol == "IND_SP_500"
    assert req.regime.vvix_symbol == "IND_VVIX"


def test_regime_window_below_two_rejected() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        ib.RunRequest(**_body(regime={"emit_signals": True, "rv_windows": [1]}))
