"""Unit tests for basket asset-class homogeneity validation helpers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from tcg.core.api.persistence import (
    BasketLegIn,
    _asset_class_from_collection,
    _check_basket_homogeneity,
    _check_basket_no_duplicates,
)


def _leg(instrument_id: str, collection: str, weight: float = 0.5) -> BasketLegIn:
    return BasketLegIn(
        instrument_id=instrument_id, collection=collection, weight=weight
    )


# ---------------------------------------------------------------------------
# _asset_class_from_collection
# ---------------------------------------------------------------------------


def test_futures_collection() -> None:
    assert _asset_class_from_collection("FUT_VIX") == "future"
    assert _asset_class_from_collection("FUT_SP_500") == "future"


def test_index_collection() -> None:
    assert _asset_class_from_collection("INDEX") == "index"


def test_equity_collections() -> None:
    assert _asset_class_from_collection("ETF") == "equity"
    assert _asset_class_from_collection("FUND") == "equity"
    assert _asset_class_from_collection("FOREX") == "equity"


def test_options_collection_returns_none() -> None:
    assert _asset_class_from_collection("OPT_VIX") is None
    assert _asset_class_from_collection("OPT_SP_500") is None


def test_unknown_collection_returns_none() -> None:
    assert _asset_class_from_collection("CRYPTO") is None
    assert _asset_class_from_collection("") is None


# ---------------------------------------------------------------------------
# _check_basket_homogeneity
# ---------------------------------------------------------------------------


def test_homogeneous_equity_legs_passes() -> None:
    legs = [_leg("SPY", "ETF"), _leg("QQQ", "ETF")]
    _check_basket_homogeneity(legs)  # should not raise


def test_homogeneous_future_legs_passes() -> None:
    legs = [_leg("VX1", "FUT_VIX"), _leg("VX2", "FUT_VIX")]
    _check_basket_homogeneity(legs)


def test_mixed_equity_future_raises_400() -> None:
    legs = [_leg("SPY", "ETF"), _leg("VX1", "FUT_VIX")]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_homogeneity(legs)
    assert exc_info.value.status_code == 400
    assert "mixed" in exc_info.value.detail.lower()


def test_options_collection_raises_400() -> None:
    legs = [_leg("VIX_C", "OPT_VIX")]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_homogeneity(legs)
    assert exc_info.value.status_code == 400
    assert "OPT_VIX" in exc_info.value.detail


def test_unknown_collection_raises_400() -> None:
    legs = [_leg("BTC", "CRYPTO")]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_homogeneity(legs)
    assert exc_info.value.status_code == 400


def test_empty_legs_passes() -> None:
    """Empty basket is valid — supports saving partial work."""
    _check_basket_homogeneity([])


def test_single_leg_passes() -> None:
    legs = [_leg("SPY", "ETF", weight=1.0)]
    _check_basket_homogeneity(legs)


def test_mixed_index_equity_raises_400() -> None:
    legs = [_leg("SPX", "INDEX"), _leg("SPY", "ETF")]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_homogeneity(legs)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# _check_basket_no_duplicates
# ---------------------------------------------------------------------------


def test_no_duplicates_passes() -> None:
    legs = [_leg("SPY", "ETF"), _leg("QQQ", "ETF")]
    _check_basket_no_duplicates(legs)


def test_duplicate_instrument_id_raises_400() -> None:
    legs = [_leg("SPY", "ETF"), _leg("SPY", "ETF", weight=0.3)]
    with pytest.raises(HTTPException) as exc_info:
        _check_basket_no_duplicates(legs)
    assert exc_info.value.status_code == 400
    assert "SPY" in exc_info.value.detail


def test_empty_no_duplicates_passes() -> None:
    _check_basket_no_duplicates([])


# ---------------------------------------------------------------------------
# BasketLegIn — weight non-zero validator
# ---------------------------------------------------------------------------


def test_basket_leg_zero_weight_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BasketLegIn(instrument_id="SPY", collection="ETF", weight=0.0)


def test_basket_leg_negative_weight_allowed() -> None:
    leg = BasketLegIn(instrument_id="SPY", collection="ETF", weight=-0.5)
    assert leg.weight == -0.5
