"""Unit tests for ``tcg.core.indicators.asset_types.infer_asset_type``.

Mirror coverage of the FE ``assetTypes.test.js`` (positive + null/unknown)
so that the two implementations stay observationally identical. Sign 4:
no magic asset-type strings — assert against the imported constants.
"""

from __future__ import annotations

from tcg.core.api._models import ContinuousInstrumentRef, SpotInstrumentRef
from tcg.core.indicators.asset_types import (
    EQUITY,
    INDEX,
    OPTION,
    infer_asset_type,
)


def test_index_collection_is_index() -> None:
    ref = SpotInstrumentRef(type="spot", collection="INDEX", instrument_id="SPX")
    assert infer_asset_type(ref) == INDEX


def test_opt_prefix_is_option() -> None:
    ref = SpotInstrumentRef(type="spot", collection="OPT_SP_500", instrument_id="X")
    assert infer_asset_type(ref) == OPTION


def test_fut_continuous_is_equity() -> None:
    ref = ContinuousInstrumentRef(
        type="continuous",
        collection="FUT_ES",
        adjustment="none",
        cycle="M",
        rollOffset=0,
        strategy="front_month",
    )
    assert infer_asset_type(ref) == EQUITY


def test_etf_forex_fund_are_equity() -> None:
    for collection in ("ETF", "FOREX", "FUND"):
        ref = SpotInstrumentRef(
            type="spot", collection=collection, instrument_id="X"
        )
        assert infer_asset_type(ref) == EQUITY, collection


def test_dict_input_supported() -> None:
    assert infer_asset_type({"type": "spot", "collection": "INDEX"}) == INDEX
    assert infer_asset_type({"collection": "OPT_X"}) == OPTION


def test_none_returns_none() -> None:
    assert infer_asset_type(None) is None


def test_missing_collection_returns_none() -> None:
    assert infer_asset_type({"type": "spot"}) is None
    assert infer_asset_type({"type": "spot", "collection": ""}) is None


def test_unknown_collection_returns_none() -> None:
    ref = {"type": "spot", "collection": "CRYPTO", "instrument_id": "BTC"}
    assert infer_asset_type(ref) is None
    # Near-miss: INDEX_OLD is NOT a prefix match — 'INDEX' is exact-match only.
    assert infer_asset_type({"collection": "INDEX_OLD"}) is None
