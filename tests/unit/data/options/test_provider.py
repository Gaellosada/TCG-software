"""Unit tests for ``tcg.data.options._provider`` — provider selection."""

from __future__ import annotations

from tcg.data.options._provider import (
    has_greeks_for_root,
    select_provider,
)


class TestSelectProvider:
    def test_btc_internal(self):
        assert select_provider("OPT_BTC") == "INTERNAL"
        # eod_datas argument is ignored for BTC:
        assert select_provider("OPT_BTC", {"IVOLATILITY": [{"date": 1}]}) == "INTERNAL"

    def test_vix_cboe(self):
        assert select_provider("OPT_VIX") == "CBOE"

    def test_eth_first_non_empty_priority(self):
        # DERIBIT first by priority order, even when later providers are present.
        eod = {"INTERNAL": [{"date": 1}], "DERIBIT": [{"date": 1}]}
        assert select_provider("OPT_ETH", eod) == "DERIBIT"

    def test_eth_falls_back_through_priority(self):
        # No DERIBIT, has INTERNAL → INTERNAL.
        eod = {"INTERNAL": [{"date": 1}]}
        assert select_provider("OPT_ETH", eod) == "INTERNAL"

    def test_eth_skips_empty_provider(self):
        # DERIBIT present but empty → fall through to INTERNAL.
        eod = {"DERIBIT": [], "INTERNAL": [{"date": 1}]}
        assert select_provider("OPT_ETH", eod) == "INTERNAL"

    def test_eth_unknown_provider_used_when_only_one(self):
        # Edge: a provider not in the priority list is still used as fallback.
        eod = {"NEW_VENDOR": [{"date": 1}]}
        assert select_provider("OPT_ETH", eod) == "NEW_VENDOR"

    def test_eth_all_empty_returns_none(self):
        eod = {"DERIBIT": [], "INTERNAL": []}
        assert select_provider("OPT_ETH", eod) is None

    def test_eth_no_eod_datas_returns_none(self):
        assert select_provider("OPT_ETH") is None
        assert select_provider("OPT_ETH", None) is None
        assert select_provider("OPT_ETH", {}) is None

    def test_others_default_ivolatility(self):
        for coll in (
            "OPT_SP_500",
            "OPT_NASDAQ_100",
            "OPT_GOLD",
            "OPT_T_NOTE_10_Y",
            "OPT_T_BOND",
            "OPT_EURUSD",
            "OPT_JPYUSD",
        ):
            assert select_provider(coll) == "IVOLATILITY", coll


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
