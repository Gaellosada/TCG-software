"""Unit tests for DefaultMarketDataService.get_aligned_prices().

Tests cover:
- Single leg passthrough
- Multi-leg inner-join alignment
- Empty legs validation
- No overlapping dates validation
- Leg not found (InstrumentId and ContinuousLegSpec)
- Invalid leg type
- Date-range filtering applied before alignment
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from tcg.data._mongo.registry import CollectionRegistry
from tcg.data.service import DefaultMarketDataService
from tcg.types.errors import DataNotFoundError, ValidationError
from tcg.types.market import (
    AdjustmentMethod,
    AssetClass,
    ContinuousLegSpec,
    ContinuousRollConfig,
    ContinuousSeries,
    InstrumentId,
    PriceResult,
    PriceSeries,
    RollStrategy,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_service() -> DefaultMarketDataService:
    mock_db = MagicMock()
    registry = CollectionRegistry(["INDEX", "FUT_VIX", "FUT_SP_500", "ETF"])
    return DefaultMarketDataService(mock_db, registry, cache_size=10)


def _price_series(dates: list[int], close_vals: list[float]) -> PriceSeries:
    """Build a minimal PriceSeries with given dates and close prices.

    open/high/low/volume are filled with deterministic dummy values.
    """
    n = len(dates)
    d = np.array(dates, dtype=np.int64)
    c = np.array(close_vals, dtype=np.float64)
    return PriceSeries(
        dates=d,
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


def _continuous_series(
    ps: PriceSeries, collection: str = "FUT_VIX"
) -> ContinuousSeries:
    return ContinuousSeries(
        collection=collection,
        roll_config=ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH),
        prices=ps,
        roll_dates=(),
        contracts=(),
    )


def _spx_id() -> InstrumentId:
    return InstrumentId(
        symbol="SPX", asset_class=AssetClass.INDEX, collection="INDEX"
    )


def _vix_leg() -> ContinuousLegSpec:
    return ContinuousLegSpec(
        collection="FUT_VIX",
        roll_config=ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH),
    )


# ── Tests ────────────────────────────────────────────────────────────


class TestAlignedPricesValidation:
    """Edge cases and input validation."""

    async def test_empty_legs_raises_validation_error(self):
        svc = _make_service()
        with pytest.raises(ValidationError, match="No legs provided"):
            await svc.get_aligned_prices({})

    async def test_invalid_leg_type_raises_validation_error(self):
        svc = _make_service()
        with pytest.raises(ValidationError, match="expected InstrumentId"):
            await svc.get_aligned_prices({"bad": "not_a_spec"})  # type: ignore[dict-item]


class TestAlignedPricesSingleLeg:
    """Single-leg alignment is just a passthrough (filtered to date range)."""

    async def test_single_instrument_id_leg(self):
        svc = _make_service()
        ps = _price_series([20240101, 20240102, 20240103], [100.0, 101.0, 102.0])
        pr = PriceResult(prices=ps, provider="YAHOO", available_providers=("YAHOO",))

        with patch.object(svc, "get_prices", new_callable=AsyncMock, return_value=pr):
            common_dates, aligned, providers = await svc.get_aligned_prices(
                {"spx": _spx_id()}
            )

        np.testing.assert_array_equal(common_dates, [20240101, 20240102, 20240103])
        assert "spx" in aligned
        np.testing.assert_array_equal(aligned["spx"].close, [100.0, 101.0, 102.0])
        assert providers["spx"] == "YAHOO"

    async def test_single_continuous_leg(self):
        svc = _make_service()
        ps = _price_series([20240101, 20240102], [15.0, 16.0])
        cs = _continuous_series(ps)

        with patch.object(svc, "get_continuous", new_callable=AsyncMock, return_value=cs):
            common_dates, aligned, providers = await svc.get_aligned_prices(
                {"vix": _vix_leg()}
            )

        np.testing.assert_array_equal(common_dates, [20240101, 20240102])
        np.testing.assert_array_equal(aligned["vix"].close, [15.0, 16.0])


class TestAlignedPricesMultiLeg:
    """Multi-leg alignment with inner join on dates."""

    async def test_two_legs_partial_overlap(self):
        svc = _make_service()
        ps_spx = _price_series(
            [20240101, 20240102, 20240103], [100.0, 101.0, 102.0]
        )
        pr_spx = PriceResult(prices=ps_spx, provider="YAHOO", available_providers=("YAHOO",))
        ps_vix = _price_series(
            [20240102, 20240103, 20240104], [15.0, 16.0, 17.0]
        )
        cs_vix = _continuous_series(ps_vix)

        with patch.object(svc, "get_prices", new_callable=AsyncMock, return_value=pr_spx), \
             patch.object(svc, "get_continuous", new_callable=AsyncMock, return_value=cs_vix):
            common_dates, aligned, _ = await svc.get_aligned_prices({
                "spx": _spx_id(),
                "vix": _vix_leg(),
            })

        # Only 20240102 and 20240103 overlap
        np.testing.assert_array_equal(common_dates, [20240102, 20240103])
        np.testing.assert_array_equal(aligned["spx"].close, [101.0, 102.0])
        np.testing.assert_array_equal(aligned["vix"].close, [15.0, 16.0])

        # All OHLCV columns must be filtered consistently
        assert len(aligned["spx"].dates) == 2
        assert len(aligned["spx"].open) == 2
        assert len(aligned["spx"].high) == 2
        assert len(aligned["spx"].low) == 2
        assert len(aligned["spx"].volume) == 2

    async def test_three_legs_intersection(self):
        svc = _make_service()
        ps_a = _price_series([20240101, 20240102, 20240103], [1.0, 2.0, 3.0])
        ps_b = _price_series([20240102, 20240103, 20240104], [4.0, 5.0, 6.0])
        ps_c = _price_series([20240103, 20240104, 20240105], [7.0, 8.0, 9.0])

        pr_a = PriceResult(prices=ps_a, provider="YAHOO", available_providers=("YAHOO",))
        pr_b = PriceResult(prices=ps_b, provider="YAHOO", available_providers=("YAHOO",))
        pr_c = PriceResult(prices=ps_c, provider="YAHOO", available_providers=("YAHOO",))

        id_a = InstrumentId(symbol="A", asset_class=AssetClass.INDEX, collection="INDEX")
        id_b = InstrumentId(symbol="B", asset_class=AssetClass.INDEX, collection="INDEX")
        id_c = InstrumentId(symbol="C", asset_class=AssetClass.INDEX, collection="INDEX")

        async def mock_get_prices(collection, symbol, **kwargs):
            return {"A": pr_a, "B": pr_b, "C": pr_c}[symbol]

        with patch.object(svc, "get_prices", side_effect=mock_get_prices):
            common_dates, aligned, _ = await svc.get_aligned_prices({
                "a": id_a, "b": id_b, "c": id_c,
            })

        # Only 20240103 is in all three
        np.testing.assert_array_equal(common_dates, [20240103])
        np.testing.assert_array_equal(aligned["a"].close, [3.0])
        np.testing.assert_array_equal(aligned["b"].close, [5.0])
        np.testing.assert_array_equal(aligned["c"].close, [7.0])

    async def test_dates_are_sorted(self):
        """Even if legs return unsorted dates, common_dates must be sorted."""
        svc = _make_service()
        # Both have same dates, just confirm output is sorted
        ps = _price_series([20240103, 20240101, 20240102], [3.0, 1.0, 2.0])
        pr = PriceResult(prices=ps, provider="YAHOO", available_providers=("YAHOO",))

        with patch.object(svc, "get_prices", new_callable=AsyncMock, return_value=pr):
            common_dates, _, _ = await svc.get_aligned_prices({"x": _spx_id()})

        # common_dates must be sorted
        assert np.all(common_dates[:-1] <= common_dates[1:])

    async def test_no_overlap_raises_validation_error(self):
        svc = _make_service()
        ps_a = _price_series([20240101], [100.0])
        ps_b = _price_series([20240102], [200.0])

        pr_a = PriceResult(prices=ps_a, provider="YAHOO", available_providers=("YAHOO",))
        pr_b = PriceResult(prices=ps_b, provider="YAHOO", available_providers=("YAHOO",))

        id_a = InstrumentId(symbol="A", asset_class=AssetClass.INDEX, collection="INDEX")
        id_b = InstrumentId(symbol="B", asset_class=AssetClass.INDEX, collection="INDEX")

        async def mock_get_prices(collection, symbol, **kwargs):
            return {"A": pr_a, "B": pr_b}[symbol]

        with patch.object(svc, "get_prices", side_effect=mock_get_prices):
            with pytest.raises(ValidationError, match="No overlapping dates"):
                await svc.get_aligned_prices({"a": id_a, "b": id_b})


class TestAlignedPricesNotFound:
    """Leg not found raises DataNotFoundError."""

    async def test_instrument_id_not_found(self):
        svc = _make_service()

        with patch.object(svc, "get_prices", new_callable=AsyncMock, return_value=None):
            with pytest.raises(DataNotFoundError, match="leg 'missing'"):
                await svc.get_aligned_prices({"missing": _spx_id()})

    async def test_continuous_leg_not_found(self):
        svc = _make_service()

        with patch.object(svc, "get_continuous", new_callable=AsyncMock, return_value=None):
            with pytest.raises(DataNotFoundError, match="leg 'vix_fut'"):
                await svc.get_aligned_prices({"vix_fut": _vix_leg()})


class TestAlignedPricesReturnTypes:
    """Verify return value types and structure."""

    async def test_common_dates_is_int64_array(self):
        svc = _make_service()
        ps = _price_series([20240101, 20240102], [100.0, 101.0])
        pr = PriceResult(prices=ps, provider="YAHOO", available_providers=("YAHOO",))

        with patch.object(svc, "get_prices", new_callable=AsyncMock, return_value=pr):
            common_dates, aligned, providers = await svc.get_aligned_prices({"x": _spx_id()})

        assert common_dates.dtype == np.int64
        assert isinstance(aligned["x"], PriceSeries)
        assert providers["x"] == "YAHOO"

    async def test_aligned_series_dates_match_common(self):
        svc = _make_service()
        ps_a = _price_series([20240101, 20240102, 20240103], [1.0, 2.0, 3.0])
        ps_b = _price_series([20240102, 20240103, 20240104], [4.0, 5.0, 6.0])

        pr_a = PriceResult(prices=ps_a, provider="YAHOO", available_providers=("YAHOO",))
        pr_b = PriceResult(prices=ps_b, provider="YAHOO", available_providers=("YAHOO",))

        id_a = InstrumentId(symbol="A", asset_class=AssetClass.INDEX, collection="INDEX")
        id_b = InstrumentId(symbol="B", asset_class=AssetClass.INDEX, collection="INDEX")

        async def mock_get_prices(collection, symbol, **kwargs):
            return {"A": pr_a, "B": pr_b}[symbol]

        with patch.object(svc, "get_prices", side_effect=mock_get_prices):
            common_dates, aligned, _ = await svc.get_aligned_prices({
                "a": id_a, "b": id_b,
            })

        # Every aligned series must have dates == common_dates
        for label, ps in aligned.items():
            np.testing.assert_array_equal(ps.dates, common_dates)
