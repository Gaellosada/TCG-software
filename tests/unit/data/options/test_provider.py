"""Unit tests for the Motor-free ``tcg.data.options._provider`` surface.

After the dwh SQL cutover, ``_provider`` keeps only the greek-gating block
list (``has_greeks_for_root`` / ``_GREEKS_BLOCKED_ROOTS``) and the cold-start
coverage-ratio baseline (``_SEED_RATIOS``). The former Mongo machinery
(``select_provider``, ``provider_priority``, the ``$sample`` ratio cache) was
removed with the Mongo readers; its tests are gone with it.
"""

from __future__ import annotations

from tcg.data.options._provider import (
    _GREEKS_BLOCKED_ROOTS,
    _SEED_RATIOS,
    has_greeks_for_root,
)


class TestHasGreeksForRoot:
    def test_eth_blocked(self):
        # OPT_ETH has no curated greeks vendor wired in → blocked at the layer.
        assert has_greeks_for_root("OPT_ETH") is False

    def test_vix_not_blocked(self):
        # OPT_VIX is no longer blanket-blocked; stored CBOE greeks pass through.
        assert has_greeks_for_root("OPT_VIX") is True

    def test_other_roots_allowed(self):
        for coll in (
            "OPT_SP_500",
            "OPT_NASDAQ_100",
            "OPT_GOLD",
            "OPT_BTC",
            "OPT_T_NOTE_10_Y",
            "OPT_T_BOND",
            "OPT_EURUSD",
            "OPT_JPYUSD",
        ):
            assert has_greeks_for_root(coll) is True, coll

    def test_block_list_is_exactly_eth(self):
        assert _GREEKS_BLOCKED_ROOTS == frozenset({"OPT_ETH"})


class TestSeedRatios:
    def test_covers_all_ten_roots(self):
        assert len(_SEED_RATIOS) == 10

    def test_blocked_roots_have_zero_ratio(self):
        # ETH (blocked) and VIX (no CBOE greeks) seed to 0.0.
        assert _SEED_RATIOS["OPT_ETH"] == 0.0
        assert _SEED_RATIOS["OPT_VIX"] == 0.0

    def test_ratios_in_unit_interval(self):
        for root, r in _SEED_RATIOS.items():
            assert 0.0 <= r <= 1.0, (root, r)
