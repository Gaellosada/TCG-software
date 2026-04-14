"""Unit tests for the tcg.data module.

Tests CollectionRegistry, LRUCache, helpers (parsing, NaN sanitization),
and DefaultMarketDataService with mocked MongoDB.
"""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from tcg.data._cache import LRUCache
from tcg.data._mongo.helpers import (
    deserialize_doc_id,
    extract_price_data,
    parse_instrument_id,
    serialize_doc_id,
)
from tcg.data._mongo.registry import CollectionRegistry
from tcg.data.service import DefaultMarketDataService
from tcg.types.errors import DataNotFoundError
from tcg.types.market import AssetClass, InstrumentId, PriceResult, PriceSeries


# ===================================================================
# CollectionRegistry
# ===================================================================


class TestCollectionRegistry:
    def test_classifies_futures(self):
        reg = CollectionRegistry(["FUT_VIX", "FUT_SP_500", "FUT_T_BOND"])
        assert reg.futures == ["FUT_SP_500", "FUT_T_BOND", "FUT_VIX"]  # sorted
        assert reg.indexes == []
        assert reg.assets == []
        assert reg.options == []

    def test_classifies_index(self):
        reg = CollectionRegistry(["INDEX"])
        assert reg.indexes == ["INDEX"]

    def test_classifies_assets(self):
        reg = CollectionRegistry(["ETF", "FUND", "FOREX"])
        assert reg.assets == ["ETF", "FOREX", "FUND"]  # sorted

    def test_classifies_options_separately(self):
        reg = CollectionRegistry(["OPT_VIX", "OPT_SP_500"])
        assert reg.options == ["OPT_SP_500", "OPT_VIX"]
        # Options are NOT in all_active
        assert reg.all_active == []

    def test_ignores_unknown_collections(self):
        reg = CollectionRegistry(
            ["FUT_VIX", "INDEX", "system.profile", "_internal", "random_stuff"]
        )
        assert reg.futures == ["FUT_VIX"]
        assert reg.indexes == ["INDEX"]
        assert reg.assets == []

    def test_all_active_order(self):
        reg = CollectionRegistry(["FUT_VIX", "ETF", "INDEX", "FUT_SP_500"])
        # all_active = indexes + assets + futures
        assert reg.all_active == ["INDEX", "ETF", "FUT_SP_500", "FUT_VIX"]

    def test_asset_class_for_future(self):
        reg = CollectionRegistry(["FUT_VIX"])
        assert reg.asset_class_for("FUT_VIX") == AssetClass.FUTURE
        # Works even for uncategorized FUT_ prefix
        assert reg.asset_class_for("FUT_UNKNOWN") == AssetClass.FUTURE

    def test_asset_class_for_index(self):
        reg = CollectionRegistry(["INDEX"])
        assert reg.asset_class_for("INDEX") == AssetClass.INDEX

    def test_asset_class_for_equity(self):
        reg = CollectionRegistry(["ETF"])
        assert reg.asset_class_for("ETF") == AssetClass.EQUITY

    def test_asset_class_for_unknown(self):
        reg = CollectionRegistry([])
        assert reg.asset_class_for("NOPE") is None

    def test_contains(self):
        reg = CollectionRegistry(["FUT_VIX", "INDEX"])
        assert "FUT_VIX" in reg
        assert "INDEX" in reg
        assert "NOPE" not in reg

    def test_empty_input(self):
        reg = CollectionRegistry([])
        assert reg.all_active == []
        assert reg.futures == []
        assert reg.indexes == []


# ===================================================================
# LRUCache
# ===================================================================


class TestLRUCache:
    def test_put_and_get(self):
        cache = LRUCache(max_size=10)
        cache.put("k1", "v1")
        assert cache.get("k1") == "v1"

    def test_get_missing_returns_none(self):
        cache = LRUCache(max_size=10)
        assert cache.get("missing") is None

    def test_eviction(self):
        cache = LRUCache(max_size=3)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        cache.put("d", 4)  # evicts "a"
        assert cache.get("a") is None
        assert cache.get("b") == 2

    def test_move_to_end_on_get(self):
        cache = LRUCache(max_size=3)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        # Access "a" so it becomes most recent
        cache.get("a")
        cache.put("d", 4)  # should evict "b", not "a"
        assert cache.get("a") == 1
        assert cache.get("b") is None

    def test_update_existing_key(self):
        cache = LRUCache(max_size=3)
        cache.put("a", 1)
        cache.put("a", 2)  # update, not insert
        assert cache.get("a") == 2
        assert len(cache) == 1

    def test_len(self):
        cache = LRUCache(max_size=10)
        cache.put("a", 1)
        cache.put("b", 2)
        assert len(cache) == 2

    def test_contains(self):
        cache = LRUCache(max_size=10)
        cache.put("a", 1)
        assert "a" in cache
        assert "b" not in cache

    def test_clear(self):
        cache = LRUCache(max_size=10)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.clear()
        assert len(cache) == 0
        assert cache.get("a") is None

    def test_invalid_max_size(self):
        with pytest.raises(ValueError, match="max_size must be >= 1"):
            LRUCache(max_size=0)


# ===================================================================
# Helpers -- serialize_doc_id / deserialize_doc_id
# ===================================================================


class TestDocIdSerialization:
    def test_string_id(self):
        assert serialize_doc_id("SPX") == "SPX"

    def test_objectid(self):
        from bson import ObjectId

        oid = ObjectId("507f1f77bcf86cd799439011")
        assert serialize_doc_id(oid) == "507f1f77bcf86cd799439011"

    def test_dict_id(self):
        result = serialize_doc_id({"symbol": "SPX", "exchange": "CBOE"})
        # Sorted keys
        assert result == "exchange=CBOE|symbol=SPX"

    def test_int_id(self):
        assert serialize_doc_id(42) == "42"

    def test_deserialize_valid_objectid(self):
        candidates = deserialize_doc_id("507f1f77bcf86cd799439011")
        from bson import ObjectId

        assert len(candidates) == 2
        assert isinstance(candidates[0], ObjectId)
        assert candidates[1] == "507f1f77bcf86cd799439011"

    def test_deserialize_plain_string(self):
        candidates = deserialize_doc_id("SPX")
        # "SPX" is not a valid ObjectId, so only the string candidate
        assert len(candidates) == 1
        assert candidates[0] == "SPX"


# ===================================================================
# Helpers -- extract_price_data
# ===================================================================


class TestExtractPriceData:
    def _make_doc(
        self,
        bars: list[dict],
        provider: str = "yahoo",
        doc_id: str = "SPX",
    ) -> dict:
        return {
            "_id": doc_id,
            "eodDatas": {provider: bars},
        }

    def test_basic_extraction(self):
        bars = [
            {"date": 20240102, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000.0},
            {"date": 20240101, "open": 98.0, "high": 101.0, "low": 97.0, "close": 100.0, "volume": 800.0},
        ]
        doc = self._make_doc(bars)
        result = extract_price_data(doc)

        assert result is not None
        series = result.prices
        assert len(series) == 2
        # Should be sorted by date
        assert series.dates[0] == 20240101
        assert series.dates[1] == 20240102
        assert series.close[0] == 100.0
        assert series.close[1] == 103.0

    def test_specific_provider(self):
        doc = {
            "_id": "SPX",
            "eodDatas": {
                "yahoo": [{"date": 20240101, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}],
                "iex": [{"date": 20240101, "open": 1.1, "high": 2.1, "low": 0.6, "close": 1.6, "volume": 200.0}],
            },
        }
        result = extract_price_data(doc, provider="iex")
        assert result is not None
        assert result.prices.close[0] == 1.6
        assert result.provider == "iex"
        assert set(result.available_providers) == {"yahoo", "iex"}

    def test_missing_provider_raises(self):
        doc = self._make_doc(
            [{"date": 20240101, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}],
            provider="yahoo",
        )
        with pytest.raises(DataNotFoundError, match="not available"):
            extract_price_data(doc, provider="nonexistent")

    def test_nan_close_drops_bar(self):
        bars = [
            {"date": 20240101, "open": 100.0, "high": 105.0, "low": 99.0, "close": float("nan"), "volume": 1000.0},
            {"date": 20240102, "open": 101.0, "high": 106.0, "low": 100.0, "close": 104.0, "volume": 1100.0},
        ]
        doc = self._make_doc(bars)
        result = extract_price_data(doc)

        assert result is not None
        series = result.prices
        assert len(series) == 1
        assert series.dates[0] == 20240102

    def test_nan_volume_replaced_with_zero(self):
        bars = [
            {"date": 20240101, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": float("nan")},
        ]
        doc = self._make_doc(bars)
        result = extract_price_data(doc)

        assert result is not None
        assert result.prices.volume[0] == 0.0

    def test_nan_open_replaced_with_zero(self):
        bars = [
            {"date": 20240101, "open": float("nan"), "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000.0},
        ]
        doc = self._make_doc(bars)
        result = extract_price_data(doc)

        assert result is not None
        assert result.prices.open[0] == 0.0

    def test_missing_eod_datas(self):
        doc = {"_id": "SPX"}
        assert extract_price_data(doc) is None

    def test_empty_eod_datas(self):
        doc = {"_id": "SPX", "eodDatas": {}}
        assert extract_price_data(doc) is None

    def test_empty_bars_list(self):
        doc = self._make_doc([])
        assert extract_price_data(doc) is None

    def test_all_bars_nan_close(self):
        bars = [
            {"date": 20240101, "open": 100.0, "high": 105.0, "low": 99.0, "close": float("nan"), "volume": 1000.0},
            {"date": 20240102, "open": 101.0, "high": 106.0, "low": 100.0, "close": float("nan"), "volume": 1100.0},
        ]
        doc = self._make_doc(bars)
        assert extract_price_data(doc) is None

    def test_missing_close_field_drops_bar(self):
        bars = [
            {"date": 20240101, "open": 100.0, "high": 105.0, "low": 99.0, "volume": 1000.0},
            {"date": 20240102, "open": 101.0, "high": 106.0, "low": 100.0, "close": 104.0, "volume": 1100.0},
        ]
        doc = self._make_doc(bars)
        result = extract_price_data(doc)
        assert result is not None
        assert len(result.prices) == 1
        assert result.prices.dates[0] == 20240102

    def test_uses_first_available_provider(self):
        doc = {
            "_id": "SPX",
            "eodDatas": {
                "alpha": [{"date": 20240101, "open": 1.0, "high": 2.0, "low": 0.5, "close": 999.0, "volume": 100.0}],
            },
        }
        result = extract_price_data(doc)
        assert result is not None
        assert result.prices.close[0] == 999.0
        assert result.provider == "alpha"

    def test_result_is_price_result(self):
        bars = [
            {"date": 20240101, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000.0},
        ]
        doc = self._make_doc(bars)
        result = extract_price_data(doc)
        assert isinstance(result, PriceResult)
        assert isinstance(result.prices, PriceSeries)
        assert result.prices.dates.dtype == np.int64
        assert result.prices.close.dtype == np.float64
        assert result.provider == "yahoo"
        assert result.available_providers == ("yahoo",)


# ===================================================================
# Helpers -- parse_instrument_id
# ===================================================================


class TestParseInstrumentId:
    def test_index_collection(self):
        doc = {"_id": "SPX"}
        iid = parse_instrument_id(doc, "INDEX")
        assert iid.symbol == "SPX"
        assert iid.asset_class == AssetClass.INDEX
        assert iid.collection == "INDEX"

    def test_future_collection(self):
        doc = {"_id": "ESH24"}
        iid = parse_instrument_id(doc, "FUT_SP_500")
        assert iid.symbol == "ESH24"
        assert iid.asset_class == AssetClass.FUTURE
        assert iid.collection == "FUT_SP_500"

    def test_equity_collection(self):
        doc = {"_id": "SPY"}
        iid = parse_instrument_id(doc, "ETF")
        assert iid.symbol == "SPY"
        assert iid.asset_class == AssetClass.EQUITY


# ===================================================================
# DefaultMarketDataService (with mocked MongoDB)
# ===================================================================


def _make_service() -> tuple[DefaultMarketDataService, AsyncMock]:
    """Build a service with a mocked database and a real registry."""
    mock_db = MagicMock()
    registry = CollectionRegistry(
        ["INDEX", "FUT_VIX", "FUT_SP_500", "ETF"]
    )
    service = DefaultMarketDataService(mock_db, registry, cache_size=10)
    return service, mock_db


class TestDefaultMarketDataServiceListCollections:
    async def test_list_all(self):
        service, _ = _make_service()
        result = await service.list_collections()
        assert result == ["INDEX", "ETF", "FUT_SP_500", "FUT_VIX"]

    async def test_filter_by_future(self):
        service, _ = _make_service()
        result = await service.list_collections(AssetClass.FUTURE)
        assert result == ["FUT_SP_500", "FUT_VIX"]

    async def test_filter_by_index(self):
        service, _ = _make_service()
        result = await service.list_collections(AssetClass.INDEX)
        assert result == ["INDEX"]

    async def test_filter_by_equity(self):
        service, _ = _make_service()
        result = await service.list_collections(AssetClass.EQUITY)
        assert result == ["ETF"]


class TestDefaultMarketDataServiceGetPrices:
    async def test_returns_none_for_missing_instrument(self):
        service, mock_db = _make_service()
        # Mock find_one to return None for all candidates
        mock_coll = AsyncMock()
        mock_coll.find_one = AsyncMock(return_value=None)
        mock_db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await service.get_prices("INDEX", "NONEXISTENT")
        assert result is None

    async def test_cache_hit_returns_same_object(self):
        service, mock_db = _make_service()

        # Build a mock that returns a valid document on first call
        sample_doc = {
            "_id": "SPX",
            "eodDatas": {
                "yahoo": [
                    {"date": 20240101, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000.0},
                ],
            },
        }
        mock_coll = AsyncMock()
        mock_coll.find_one = AsyncMock(return_value=sample_doc)
        mock_db.__getitem__ = MagicMock(return_value=mock_coll)

        # First call -- populates cache
        result1 = await service.get_prices("INDEX", "SPX")
        assert result1 is not None

        # Second call -- should hit cache
        result2 = await service.get_prices("INDEX", "SPX")
        assert result2 is result1  # same object, not re-queried

        # find_one should have been called only during the first get_prices.
        # "SPX" is not a valid ObjectId, so deserialize_doc_id returns
        # only ["SPX"] (one candidate), meaning one find_one call total.
        assert mock_coll.find_one.await_count == 1

    async def test_list_instruments_unknown_collection_raises(self):
        service, _ = _make_service()
        with pytest.raises(DataNotFoundError, match="not found"):
            await service.list_instruments("NOPE")

    async def test_list_instruments_delegates_to_reader(self):
        service, mock_db = _make_service()

        # Patch MongoInstrumentReader.list_instruments
        sample_id = InstrumentId(
            symbol="SPX", asset_class=AssetClass.INDEX, collection="INDEX"
        )
        with patch.object(
            service._mongo,
            "list_instruments",
            new_callable=AsyncMock,
            return_value=([sample_id], 1),
        ):
            result = await service.list_instruments("INDEX")
            assert result.total == 1
            assert result.items == (sample_id,)
            assert result.skip == 0
            assert result.limit == 50

    async def test_get_continuous_rejects_non_futures_collection(self):
        from tcg.types.errors import DataNotFoundError
        from tcg.types.market import ContinuousRollConfig, RollStrategy

        service, _ = _make_service()
        with pytest.raises(DataNotFoundError, match="not a futures collection"):
            await service.get_continuous(
                "INDEX",
                ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH),
            )

    async def test_get_continuous_rejects_unknown_collection(self):
        from tcg.types.errors import DataNotFoundError
        from tcg.types.market import ContinuousRollConfig, RollStrategy

        service, _ = _make_service()
        with pytest.raises(DataNotFoundError, match="not found"):
            await service.get_continuous(
                "FUT_NONEXISTENT",
                ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH),
            )

    async def test_get_aligned_prices_empty_legs_raises(self):
        service, _ = _make_service()
        from tcg.types.errors import ValidationError as VE
        with pytest.raises(VE, match="No legs provided"):
            await service.get_aligned_prices({})
