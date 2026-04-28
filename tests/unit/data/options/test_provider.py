"""Unit tests for ``tcg.data.options._provider`` — provider selection.

Phase-1 regression notes:
- Real DB has OPT_BTC and OPT_ETH on provider ``COINAPI`` (not the
  ``INTERNAL`` / ``DERIBIT`` originally hard-coded). The selection
  algorithm must therefore (a) prefer per-root priority providers when
  data is present, (b) fall back to scanning whatever providers ARE in
  ``eodDatas``. Tests under ``TestRealDBRegression`` pin this.
"""

from __future__ import annotations

from tcg.data.options._provider import (
    has_greeks_for_root,
    provider_priority,
    select_provider,
)


def _bars(*dates: int) -> list[dict]:
    return [{"date": d} for d in dates]


class TestSelectProvider:
    # ---- Per-root priority hits ----

    def test_btc_priority_picks_coinapi_first(self):
        # When both COINAPI and INTERNAL are present, COINAPI wins.
        eod = {"INTERNAL": _bars(20240101), "COINAPI": _bars(20240101)}
        assert select_provider("OPT_BTC", eod) == "COINAPI"

    def test_btc_falls_through_to_internal(self):
        eod = {"INTERNAL": _bars(20240101)}
        assert select_provider("OPT_BTC", eod) == "INTERNAL"

    def test_eth_priority_picks_coinapi_first(self):
        eod = {"DERIBIT": _bars(20240101), "COINAPI": _bars(20240101)}
        assert select_provider("OPT_ETH", eod) == "COINAPI"

    def test_eth_falls_through_to_deribit_then_internal(self):
        assert select_provider("OPT_ETH", {"DERIBIT": _bars(1)}) == "DERIBIT"
        assert select_provider("OPT_ETH", {"INTERNAL": _bars(1)}) == "INTERNAL"

    def test_vix_cboe(self):
        assert select_provider("OPT_VIX", {"CBOE": _bars(1)}) == "CBOE"

    def test_others_pick_ivolatility(self):
        eod = {"IVOLATILITY": _bars(1)}
        for coll in (
            "OPT_SP_500",
            "OPT_NASDAQ_100",
            "OPT_GOLD",
            "OPT_T_NOTE_10_Y",
            "OPT_T_BOND",
            "OPT_EURUSD",
            "OPT_JPYUSD",
        ):
            assert select_provider(coll, eod) == "IVOLATILITY", coll

    # ---- No-fallback contract: unknown providers drop the row ----

    def test_unknown_provider_returns_none_for_btc(self):
        # An uncurated provider must surface as None — drop the row
        # rather than silently pick something we have not validated.
        eod = {"BRAND_NEW_PROVIDER": _bars(1)}
        assert select_provider("OPT_BTC", eod) is None

    def test_unknown_provider_returns_none_for_sp500(self):
        eod = {"BRAND_NEW_PROVIDER": _bars(1)}
        assert select_provider("OPT_SP_500", eod) is None

    def test_skips_empty_provider_within_priority(self):
        eod = {"COINAPI": [], "DERIBIT": _bars(1)}
        assert select_provider("OPT_BTC", eod) == "DERIBIT"

    def test_all_empty_returns_none(self):
        assert select_provider("OPT_BTC", {"COINAPI": [], "DERIBIT": []}) is None

    def test_no_eod_datas_returns_none(self):
        assert select_provider("OPT_BTC") is None
        assert select_provider("OPT_SP_500", None) is None
        assert select_provider("OPT_ETH", {}) is None


class TestRealDBRegression:
    """Pins the bug-fix from 2026-04-28 — production DB has COINAPI for
    OPT_BTC and OPT_ETH (not INTERNAL / DERIBIT). Without this, every
    BTC/ETH chain query returns zero rows because ``select_provider``
    can't find data under the legacy provider names.
    """

    def test_btc_with_only_coinapi_returns_coinapi(self):
        # Mirrors what /api/options/roots returns from the production DB.
        eod = {"COINAPI": [{"date": 20240315, "bid": 100, "ask": 101}]}
        assert select_provider("OPT_BTC", eod) == "COINAPI", (
            "Real DB has OPT_BTC under COINAPI; selection must follow."
        )

    def test_eth_with_only_coinapi_returns_coinapi(self):
        eod = {"COINAPI": [{"date": 20240315, "bid": 50, "ask": 51}]}
        assert select_provider("OPT_ETH", eod) == "COINAPI"


class TestProviderPriority:
    def test_priority_lists_for_known_roots(self):
        assert provider_priority("OPT_BTC") == ("COINAPI", "DERIBIT", "INTERNAL")
        assert provider_priority("OPT_ETH") == ("COINAPI", "DERIBIT", "INTERNAL")
        assert provider_priority("OPT_VIX") == ("CBOE",)
        assert provider_priority("OPT_SP_500") == ("IVOLATILITY",)

    def test_priority_for_unknown_root_is_empty(self):
        assert provider_priority("OPT_UNKNOWN") == ()


class TestHasGreeksForRoot:
    def test_blocked(self):
        assert has_greeks_for_root("OPT_VIX") is False
        assert has_greeks_for_root("OPT_ETH") is False

    def test_allowed(self):
        for coll in (
            "OPT_SP_500",
            "OPT_BTC",
            "OPT_NASDAQ_100",
            "OPT_GOLD",
            "OPT_T_NOTE_10_Y",
            "OPT_T_BOND",
            "OPT_EURUSD",
            "OPT_JPYUSD",
        ):
            assert has_greeks_for_root(coll) is True, coll
