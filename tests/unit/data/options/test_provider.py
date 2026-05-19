"""Unit tests for ``tcg.data.options._provider`` — provider selection.

Phase-1 regression notes:
- Real DB has OPT_BTC and OPT_ETH on provider ``COINAPI`` (not the
  ``INTERNAL`` / ``DERIBIT`` originally hard-coded). The selection
  algorithm must therefore (a) prefer per-root priority providers when
  data is present, (b) fall back to scanning whatever providers ARE in
  ``eodDatas``. Tests under ``TestRealDBRegression`` pin this.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tcg.data.options import _provider
from tcg.data.options._provider import (
    _SEED_RATIOS,
    get_stored_greeks_ratios,
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
        # Only OPT_ETH stays blocked at the data layer. OPT_VIX was
        # unblocked in Phase 1 of the VIX greeks rollout — any stored
        # CBOE greeks now pass through with ``source="stored"``.
        assert has_greeks_for_root("OPT_ETH") is False

    def test_vix_no_longer_blocked(self):
        # Phase 1 of VIX greeks rollout: the data-layer blanket that
        # forced OPT_VIX to has_greeks=False has been lifted. The
        # engine compute path stays gated independently (see
        # ``tcg.engine.options.pricing._gating``).
        assert has_greeks_for_root("OPT_VIX") is True

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
            "OPT_VIX",
        ):
            assert has_greeks_for_root(coll) is True, coll


def _make_db_mock(
    *,
    ratios_per_root: dict[str, float] | None = None,
    raise_on_aggregate: bool = False,
) -> MagicMock:
    """Build a Motor-like async database mock that returns the requested
    coverage ratios from a stubbed ``$sample`` aggregation.

    ``db[root].aggregate(pipeline)`` yields a single doc with ``total`` and
    ``with_greeks`` matching ``ratios_per_root[root]`` (default 0.0).
    """
    ratios = ratios_per_root or {}

    class _FakeCursor:
        def __init__(self, doc: dict) -> None:
            self._doc = doc
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return self._doc

    class _FakeCollection:
        def __init__(self, root: str) -> None:
            self._root = root

        def aggregate(self, _pipeline):
            if raise_on_aggregate:
                raise RuntimeError("simulated mongo failure")
            ratio = ratios.get(self._root, 0.0)
            total = 500
            with_greeks = int(round(total * ratio))
            return _FakeCursor({"_id": None, "total": total, "with_greeks": with_greeks})

    class _FakeDB:
        def __getitem__(self, root: str) -> _FakeCollection:
            return _FakeCollection(root)

    return _FakeDB()


@pytest.mark.asyncio
class TestStoredGreeksRatioCache:
    """Lazy 24h TTL cache around ``get_stored_greeks_ratios``."""

    def setup_method(self) -> None:
        _provider._reset_ratio_cache_for_tests()

    async def test_cold_start_measures_and_caches(self, monkeypatch):
        db = _make_db_mock(
            ratios_per_root={"OPT_SP_500": 1.0, "OPT_JPYUSD": 0.30}
        )
        # Pin monotonic to a known value to make assertions deterministic.
        monkeypatch.setattr(_provider.time, "monotonic", lambda: 1000.0)
        out = await get_stored_greeks_ratios(db)
        assert out["OPT_SP_500"] == pytest.approx(1.0, abs=0.01)
        assert out["OPT_JPYUSD"] == pytest.approx(0.30, abs=0.01)
        # The cache is populated.
        assert _provider._ratio_cache is not None
        assert _provider._ratio_cache.measured_at == 1000.0

    async def test_within_ttl_returns_cache_without_remeasuring(self, monkeypatch):
        # Seed the cache with explicit values.
        _provider._ratio_cache = _provider._RatioCacheEntry(
            ratios={"OPT_SP_500": 0.5}, measured_at=1000.0
        )
        # Time has advanced by 12h — still within the 24h TTL.
        monkeypatch.setattr(_provider.time, "monotonic", lambda: 1000.0 + 12 * 3600)
        # Pass a db mock that would return different values; expect we
        # never see them because cache is hit.
        db = _make_db_mock(ratios_per_root={"OPT_SP_500": 1.0})
        out = await get_stored_greeks_ratios(db)
        assert out["OPT_SP_500"] == 0.5  # original cached value, not new mock value

    async def test_expired_cache_triggers_remeasure(self, monkeypatch):
        _provider._ratio_cache = _provider._RatioCacheEntry(
            ratios={"OPT_SP_500": 0.5}, measured_at=1000.0
        )
        # Time has advanced 25h — past 24h TTL.
        monkeypatch.setattr(_provider.time, "monotonic", lambda: 1000.0 + 25 * 3600)
        db = _make_db_mock(ratios_per_root={"OPT_SP_500": 0.997})
        out = await get_stored_greeks_ratios(db)
        assert out["OPT_SP_500"] == pytest.approx(0.997, abs=0.01)

    async def test_failure_with_prior_cache_serves_stale(self, monkeypatch):
        _provider._ratio_cache = _provider._RatioCacheEntry(
            ratios={"OPT_SP_500": 0.42}, measured_at=1000.0
        )
        # Force TTL expiry.
        monkeypatch.setattr(_provider.time, "monotonic", lambda: 1000.0 + 25 * 3600)

        # Mock the inner measurement to raise — the outer should swallow
        # and serve stale cache.
        async def _boom(_db):
            raise RuntimeError("mongo down")

        monkeypatch.setattr(_provider, "_measure_stored_greeks_ratios", _boom)
        out = await get_stored_greeks_ratios(_make_db_mock())
        assert out["OPT_SP_500"] == 0.42  # stale cache returned

    async def test_failure_without_prior_cache_serves_seed(self, monkeypatch):
        # Cold cache.
        assert _provider._ratio_cache is None

        async def _boom(_db):
            raise RuntimeError("mongo down")

        monkeypatch.setattr(_provider, "_measure_stored_greeks_ratios", _boom)
        out = await get_stored_greeks_ratios(_make_db_mock())
        # _SEED_RATIOS values returned exactly.
        for root, expected in _SEED_RATIOS.items():
            assert out[root] == pytest.approx(expected, abs=1e-9)

    async def test_per_root_failure_is_dropped_not_fatal(self, monkeypatch):
        """A single collection erroring out should not poison the snapshot —
        the surviving roots still populate the cache.
        """
        from tcg.data.options._provider import _PRIORITY_BY_ROOT

        bad_root = "OPT_SP_500"

        class _BadCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("collection scan failed")

        class _MixedCollection:
            def __init__(self, root: str) -> None:
                self._root = root

            def aggregate(self, _pipeline):
                if self._root == bad_root:
                    return _BadCursor()

                class _OK:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        if getattr(self, "_done", False):
                            raise StopAsyncIteration
                        self._done = True
                        return {"_id": None, "total": 500, "with_greeks": 250}

                return _OK()

        class _DB:
            def __getitem__(self, root: str) -> _MixedCollection:
                return _MixedCollection(root)

        out = await get_stored_greeks_ratios(_DB())
        # Surviving roots present with ratio 0.5; bad root absent.
        assert bad_root not in out
        for root in _PRIORITY_BY_ROOT:
            if root != bad_root:
                assert out[root] == pytest.approx(0.5, abs=0.01), root
