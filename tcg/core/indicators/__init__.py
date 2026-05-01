"""Indicator-domain shared vocabulary and helpers.

Currently exposes the asset-type enum and ``infer_asset_type`` helper that
mirrors the FE module ``frontend/src/pages/Indicators/assetTypes.js``. The
two are kept in sync by ``tests/api/test_asset_type_parity.py``.
"""

from tcg.core.indicators.asset_types import (
    ASSET_TYPES,
    EQUITY,
    INDEX,
    OPTION,
    infer_asset_type,
)

__all__ = ["ASSET_TYPES", "EQUITY", "INDEX", "OPTION", "infer_asset_type"]
