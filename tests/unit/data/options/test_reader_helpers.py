"""Pure-function tests for ``tcg.data.options.reader`` helpers.

These exercise the per-doc materialization logic without any Mongo
client. We import the private helpers because they encapsulate the
client-side filter, type-case, and provider-selection plumbing that
``query_chain`` relies on.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data._mongo.registry import CollectionRegistry
from tcg.data.options.reader import (
    _build_rows,
    _materialize_chain_row,
    _fallback_provider,
    _find_bar_for_date,
)


# ---------------------------------------------------------------------------
# _materialize_chain_row — combines provider pick + filters + bar lookup
# ---------------------------------------------------------------------------


class TestMaterializeChainRow:
    def test_sp500_call_passes_filter(self, sp500_doc):
        pair = _materialize_chain_row(
            doc=sp500_doc,
            collection="OPT_SP_500",
            target_yyyymmdd=20240301,
            type_filter="C",
            strike_min=None,
            strike_max=None,
        )
        assert pair is not None
        contract, row = pair
        assert contract.type == "C"
        assert contract.provider == "IVOLATILITY"
        assert row.date == date(2024, 3, 1)
        assert row.delta_stored == 0.50

    def test_type_filter_excludes_puts(self, sp500_doc):
        pair = _materialize_chain_row(
            doc=sp500_doc,
            collection="OPT_SP_500",
            target_yyyymmdd=20240301,
            type_filter="P",
            strike_min=None,
            strike_max=None,
        )
        assert pair is None

    def test_type_both_includes(self, sp500_doc):
        pair = _materialize_chain_row(
            doc=sp500_doc,
            collection="OPT_SP_500",
            target_yyyymmdd=20240301,
            type_filter="BOTH",
            strike_min=None,
            strike_max=None,
        )
        assert pair is not None

    def test_strike_filter(self, sp500_doc):
        # strike=5000; min=4500 ok, max=4900 excludes
        assert _materialize_chain_row(
            doc=sp500_doc,
            collection="OPT_SP_500",
            target_yyyymmdd=20240301,
            type_filter="BOTH",
            strike_min=4500.0,
            strike_max=4900.0,
        ) is None

        assert _materialize_chain_row(
            doc=sp500_doc,
            collection="OPT_SP_500",
            target_yyyymmdd=20240301,
            type_filter="BOTH",
            strike_min=4500.0,
            strike_max=5500.0,
        ) is not None

    def test_missing_date_returns_none(self, sp500_doc):
        pair = _materialize_chain_row(
            doc=sp500_doc,
            collection="OPT_SP_500",
            target_yyyymmdd=20240310,  # not present in eodDatas
            type_filter="BOTH",
            strike_min=None,
            strike_max=None,
        )
        assert pair is None

    def test_vix_no_greeks_surfaced(self, vix_doc):
        pair = _materialize_chain_row(
            doc=vix_doc,
            collection="OPT_VIX",
            target_yyyymmdd=20240315,
            type_filter="BOTH",
            strike_min=None,
            strike_max=None,
        )
        assert pair is not None
        contract, row = pair
        assert contract.provider == "CBOE"
        assert contract.type == "P"  # normalized
        assert row.delta_stored is None
        assert row.iv_stored is None

    def test_btc_uses_internal(self, btc_doc):
        pair = _materialize_chain_row(
            doc=btc_doc,
            collection="OPT_BTC",
            target_yyyymmdd=20240320,
            type_filter="BOTH",
            strike_min=None,
            strike_max=None,
        )
        assert pair is not None
        contract, row = pair
        assert contract.provider == "INTERNAL"
        assert row.delta_stored == 0.50
        assert row.underlying_price_stored == 58000.0

    def test_eth_first_non_empty_provider(self, eth_doc_with_deribit):
        pair = _materialize_chain_row(
            doc=eth_doc_with_deribit,
            collection="OPT_ETH",
            target_yyyymmdd=20240320,
            type_filter="BOTH",
            strike_min=None,
            strike_max=None,
        )
        assert pair is not None
        contract, row = pair
        # DERIBIT picked over INTERNAL by priority:
        assert contract.provider == "DERIBIT"
        assert row.bid == 80.0
        # No greeks should ever appear on OPT_ETH:
        assert row.delta_stored is None
        assert row.iv_stored is None

    def test_eth_internal_fallback(self, eth_doc_with_internal):
        pair = _materialize_chain_row(
            doc=eth_doc_with_internal,
            collection="OPT_ETH",
            target_yyyymmdd=20240320,
            type_filter="BOTH",
            strike_min=None,
            strike_max=None,
        )
        assert pair is not None
        contract, _ = pair
        assert contract.provider == "INTERNAL"

    def test_eth_empty_returns_none(self, eth_doc_empty):
        pair = _materialize_chain_row(
            doc=eth_doc_empty,
            collection="OPT_ETH",
            target_yyyymmdd=20240320,
            type_filter="BOTH",
            strike_min=None,
            strike_max=None,
        )
        assert pair is None


# ---------------------------------------------------------------------------
# _build_rows — full series with greeks merged by date
# ---------------------------------------------------------------------------


class TestBuildRows:
    def test_sp500_full_series_sorted(self, sp500_doc):
        rows = _build_rows(sp500_doc, "IVOLATILITY", allow_greeks=True)
        # 3 bars in fixture → 3 rows; sorted chronologically.
        assert [r.date for r in rows] == [
            date(2024, 3, 1),
            date(2024, 3, 2),
            date(2024, 3, 3),
        ]
        # Day 1 has greeks; day 2 / 3 do not.
        assert rows[0].delta_stored == 0.50
        assert rows[1].delta_stored is None
        assert rows[2].delta_stored is None
        # Mid rule: day 2 has no bid, day 3 has zero quotes.
        assert rows[0].mid == pytest.approx(2.05)
        assert rows[1].mid is None
        assert rows[2].mid is None

    def test_vix_blocked_greeks(self, vix_doc):
        rows = _build_rows(vix_doc, "CBOE", allow_greeks=False)
        assert len(rows) == 1
        # Even if the doc had eodGreeks (it doesn't here), allow_greeks=False
        # would suppress them.
        assert rows[0].delta_stored is None

    def test_no_eod_datas(self):
        rows = _build_rows({"_id": "x"}, "IVOLATILITY", allow_greeks=True)
        assert rows == []


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


class TestFindBarForDate:
    def test_hit(self):
        bars = [{"date": 20240101}, {"date": 20240102}]
        assert _find_bar_for_date(bars, 20240102) == {"date": 20240102}

    def test_miss(self):
        bars = [{"date": 20240101}]
        assert _find_bar_for_date(bars, 20240102) is None

    def test_skips_garbage(self):
        bars = [{"date": "bad"}, {"date": 20240102}]
        assert _find_bar_for_date(bars, 20240102) == {"date": 20240102}


class TestFallbackProvider:
    def test_btc(self):
        assert _fallback_provider("OPT_BTC") == "INTERNAL"

    def test_vix(self):
        assert _fallback_provider("OPT_VIX") == "CBOE"

    def test_eth(self):
        assert _fallback_provider("OPT_ETH") == "DERIBIT"

    def test_default(self):
        assert _fallback_provider("OPT_SP_500") == "IVOLATILITY"


# ---------------------------------------------------------------------------
# CollectionRegistry.all_options
# ---------------------------------------------------------------------------


class TestCollectionRegistryOptions:
    def test_all_options_extracted_and_sorted(self):
        names = [
            "INDEX",
            "FUT_SP_500",
            "OPT_SP_500",
            "OPT_BTC",
            "OPT_VIX",
            "ETF",
        ]
        registry = CollectionRegistry(names)
        assert registry.all_options == ["OPT_BTC", "OPT_SP_500", "OPT_VIX"]

    def test_options_not_in_all_active(self):
        names = ["INDEX", "FUT_SP_500", "OPT_SP_500"]
        registry = CollectionRegistry(names)
        # all_active must keep its existing contract (no OPT_*).
        assert "OPT_SP_500" not in registry.all_active
        assert "OPT_SP_500" not in registry  # __contains__ delegates to all_active

    def test_empty(self):
        registry = CollectionRegistry([])
        assert registry.all_options == []


# ---------------------------------------------------------------------------
# _peek_last_trade_date — surfaces ingestion cutoff for default-date UX
# ---------------------------------------------------------------------------


class _StubCollection:
    """Minimal Motor-collection stub for ``_peek_last_trade_date`` tests.

    ``find_one`` accepts a query + projection + sort; the stub picks the
    first doc matching the ``expiration`` predicate, sorted as requested.
    """

    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    async def find_one(self, query, projection=None, sort=None):
        cands = [d for d in self._docs if _matches(d, query)]
        if sort:
            field, direction = sort[0]
            cands.sort(key=lambda d: _get_path(d, field) or 0, reverse=(direction == -1))
        return cands[0] if cands else None


def _matches(doc, query):
    for k, v in query.items():
        if k == "expiration" and isinstance(v, dict):
            ev = doc.get("expiration")
            if "$gte" in v and (ev is None or ev < v["$gte"]):
                return False
            if "$ne" in v and ev == v["$ne"]:
                return False
        elif isinstance(v, dict) and "$exists" in v:
            if v["$exists"] and k not in doc:
                return False
            if not v["$exists"] and k in doc:
                return False
        else:
            if doc.get(k) != v:
                return False
    return True


def _get_path(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class TestPeekLastTradeDate:
    """Regression (2026-04-28): defaulting the chain-query date to "today"
    returns zero rows because Mongo bar dates end at the ingestion cutoff
    (typically weeks behind real time). ``OptionRootInfo.last_trade_date``
    surfaces the cutoff so the frontend can default to a date that has
    data.

    Single-path strategy: pick the live contract with smallest expiration
    >= today, scan its ``eodDatas`` bars across all providers, return max
    bar date. None when no live contract has bars — caller surfaces that
    loudly rather than guessing.
    """

    @pytest.mark.asyncio
    async def test_picks_max_bar_date_from_live_contract(self):
        from tcg.data.options.reader import _peek_last_trade_date

        docs = [
            {
                "expiration": 20260428,  # live contract
                "eodDatas": {"IVOLATILITY": [
                    {"date": 20260101, "bid": 1, "ask": 2},
                    {"date": 20260427, "bid": 1, "ask": 2},  # latest
                    {"date": 20260315, "bid": 1, "ask": 2},
                ]},
            },
            {
                "expiration": 20231215,  # already-expired — must be ignored
                "eodDatas": {"IVOLATILITY": [{"date": 20231215, "bid": 1, "ask": 1}]},
            },
        ]
        coll = _StubCollection(docs)
        assert await _peek_last_trade_date(coll) == date(2026, 4, 27)

    @pytest.mark.asyncio
    async def test_scans_all_providers_on_live_doc(self):
        """Regression — OPT_BTC is heterogeneous: docs vary in which
        provider key they use. The scan must traverse all provider keys
        on the live doc, not require a specific provider.
        """
        from tcg.data.options.reader import _peek_last_trade_date

        docs = [
            {
                "expiration": 20260428,
                "eodDatas": {"DERIBIT": [
                    {"date": 20260425, "bid": 1, "ask": 2},
                    {"date": 20260427, "bid": 1, "ask": 2},
                ]},
            },
        ]
        coll = _StubCollection(docs)
        assert await _peek_last_trade_date(coll) == date(2026, 4, 27)

    @pytest.mark.asyncio
    async def test_no_live_contract_returns_none(self):
        """When there is no live contract, return None loudly. The
        frontend will surface "no data available" rather than guess a
        default and silently mislead the user.
        """
        from tcg.data.options.reader import _peek_last_trade_date

        docs = [
            {"expiration": 20231215, "eodDatas": {"IVOLATILITY": [{"date": 20231215}]}},
        ]
        coll = _StubCollection(docs)
        assert await _peek_last_trade_date(coll) is None

    @pytest.mark.asyncio
    async def test_live_contract_without_eod_datas_returns_none(self):
        from tcg.data.options.reader import _peek_last_trade_date

        coll = _StubCollection([{"expiration": 20260428}])  # no eodDatas
        assert await _peek_last_trade_date(coll) is None

    @pytest.mark.asyncio
    async def test_empty_collection_returns_none(self):
        from tcg.data.options.reader import _peek_last_trade_date

        assert await _peek_last_trade_date(_StubCollection([])) is None
