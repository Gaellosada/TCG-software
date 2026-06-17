"""Unit tests for the SQL data layer's pure helpers and gotcha handling.

No live DB: these exercise the Decimal→float boundary, the collection→
AssetClass mapping, and the option-row derivations (mid / iv / UNION-ALL
collapse) that encode the documented warehouse gotchas. Live-DB parity is
covered separately by ``output/parity_harness.py``.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from tcg.data._sql.connection import to_float, to_float_or
from tcg.data._sql.instruments import _asset_class_for
from tcg.data._sql.options import (
    _canonical_mid_inputs_ok,
    _coalesce_first,
    _mid,
    _normalize_type,
    _sanitize_iv,
    _scale,
)
from tcg.data.service import DefaultMarketDataService
from tcg.types.market import AssetClass


# --------------------------------------------------------------------------- #
# Decimal / NULL coercion [boundary gotcha]
# --------------------------------------------------------------------------- #
class TestToFloat:
    def test_decimal_to_float(self):
        assert to_float(Decimal("1234.5600")) == pytest.approx(1234.56)

    def test_int_to_float(self):
        assert to_float(7) == 7.0
        assert isinstance(to_float(7), float)

    def test_none_stays_none(self):
        assert to_float(None) is None

    def test_nan_collapses_to_none(self):
        assert to_float(float("nan")) is None
        assert to_float(Decimal("NaN")) is None

    def test_plain_float_passthrough(self):
        assert to_float(3.14) == 3.14

    def test_unparseable_to_none(self):
        assert to_float("not-a-number") is None

    def test_to_float_or_substitutes_default(self):
        assert to_float_or(None, 0.0) == 0.0
        assert to_float_or(float("nan"), 0.0) == 0.0
        assert to_float_or(Decimal("5"), 0.0) == 5.0


# --------------------------------------------------------------------------- #
# Collection → AssetClass mapping [collection-mapping gotcha]
# --------------------------------------------------------------------------- #
class TestAssetClassMapping:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("future", AssetClass.FUTURE),
            ("index", AssetClass.INDEX),
            ("etf", AssetClass.EQUITY),
            ("fund", AssetClass.EQUITY),
            ("forex", AssetClass.EQUITY),
            ("anything_else", AssetClass.EQUITY),  # coarse default, as Mongo did
        ],
    )
    def test_reader_maps_dwh_class_to_coarse_enum(self, raw, expected):
        assert _asset_class_for(raw) == expected

    @pytest.mark.parametrize(
        "collection,expected",
        [
            ("FUT_SP_500", AssetClass.FUTURE),
            ("FUT_VIX", AssetClass.FUTURE),
            ("INDEX", AssetClass.INDEX),
            ("ETF", AssetClass.EQUITY),
            ("FUND", AssetClass.EQUITY),
            ("FOREX", AssetClass.EQUITY),
            ("OPT_SP_500", None),  # options excluded from the portfolio classifier
            ("UNKNOWN", None),
        ],
    )
    def test_service_classifies_collection_names(self, collection, expected):
        assert DefaultMarketDataService.asset_class_for(collection) == expected


# --------------------------------------------------------------------------- #
# Option mid / iv / type derivations [gotchas 8, vendor IV sentinel, type case]
# --------------------------------------------------------------------------- #
class TestOptionDerivations:
    def test_mid_requires_both_quotes_positive(self):
        # Both present and positive → average.
        assert _mid(1.0, 1.5) == pytest.approx(1.25)
        # Mirrors the production Mongo _compute_mid: a zero or missing side → None.
        assert _mid(0.0, 1.5) is None
        assert _mid(1.0, 0.0) is None
        assert _mid(None, 1.5) is None
        assert _mid(1.0, None) is None
        assert _mid(0.0, 0.0) is None
        assert _mid(-1.0, 2.0) is None

    def test_canonical_mid_inputs_ok_matches_mid(self):
        # The two must agree so the parity harness and reader never disagree.
        for b, a in [(0.0, 0.3), (1.0, 2.0), (None, 1.0), (0.0, 0.0)]:
            ok = _canonical_mid_inputs_ok(b, a)
            assert ok == (_mid(b, a) is not None)

    def test_sanitize_iv_drops_nonpositive(self):
        assert _sanitize_iv(0.25) == 0.25
        assert _sanitize_iv(0.0) is None
        assert _sanitize_iv(-1.0) is None  # IVolatility no-IV sentinel
        assert _sanitize_iv(None) is None

    def test_normalize_type_uppercases(self):
        assert _normalize_type("c") == "C"
        assert _normalize_type("P") == "P"
        assert _normalize_type(" p ") == "P"

    def test_normalize_type_defaults_to_C_on_garbage(self):
        # dwh option_type is NOT NULL so this is unreachable in practice;
        # the default keeps the Literal["C","P"] total.
        assert _normalize_type(None) == "C"
        assert _normalize_type("X") == "C"

    def test_coalesce_first_keeps_first_non_null(self):
        # UNION-ALL collapse rule: first non-NULL wins per field.
        assert _coalesce_first(1.0, 2.0) == 1.0
        assert _coalesce_first(None, 2.0) == 2.0
        assert _coalesce_first(None, None) is None
        assert _coalesce_first(0.0, 9.0) == 0.0  # 0.0 is a real value, not "null"

    def test_scale_preserves_none(self):
        assert _scale(None, 100.0) is None
        assert _scale(2.0, 50.0) == 100.0
