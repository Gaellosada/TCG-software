"""Parity test between FE and BE asset-type vocabularies.

The frontend module at ``frontend/src/pages/Indicators/assetTypes.js``
and the backend module at ``tcg/core/indicators/asset_types.py`` MUST
agree on the set of asset-type string values. This test fails loud if
either side gains or loses a value — that is exactly the situation
the parity test is here to catch.

The test compares the SET of literal string values — NOT the constant
names. The naming convention (UPPERCASE constants on both sides) is
documentation, not contract; the wire/storage form (lowercase string
literals) is the contract.

Strategy
--------
Read the JS file as text and regex-extract the right-hand sides of
``export const INDEX = '...'``, ``EQUITY = '...'``, and ``OPTION = '...'``.
Compare the resulting set to the Python ``ASSET_TYPES`` frozenset.

If either side adds a 4th asset-type, the parity test must be updated
in lock-step on both sides — fail loud rather than allow silent drift.
"""

from __future__ import annotations

import re
from pathlib import Path

from tcg.core.indicators.asset_types import ASSET_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
FE_ASSET_TYPES_JS = (
    REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "assetTypes.js"
)

# Match `export const NAME = 'value';` — single OR double quotes.
# Captures (NAME, value).
_EXPORT_RE = re.compile(
    r"export\s+const\s+(INDEX|EQUITY|OPTION)\s*=\s*['\"]([^'\"]+)['\"]\s*;",
    re.MULTILINE,
)


def _extract_fe_asset_types() -> dict[str, str]:
    """Extract the {NAME: value} mapping from the FE asset-types module."""
    assert FE_ASSET_TYPES_JS.exists(), (
        f"FE asset-types module missing at {FE_ASSET_TYPES_JS}"
    )
    text = FE_ASSET_TYPES_JS.read_text(encoding="utf-8")
    matches = _EXPORT_RE.findall(text)
    out: dict[str, str] = {}
    for name, value in matches:
        out[name] = value
    return out


def test_fe_module_exports_three_constants() -> None:
    """The FE module must export INDEX, EQUITY, and OPTION constants."""
    fe = _extract_fe_asset_types()
    assert set(fe.keys()) == {"INDEX", "EQUITY", "OPTION"}, (
        f"FE asset-types module missing constants; got {sorted(fe.keys())}"
    )


def test_fe_be_asset_type_values_match() -> None:
    """The set of asset-type literals must be identical on FE and BE."""
    fe = _extract_fe_asset_types()
    fe_values = set(fe.values())
    be_values = set(ASSET_TYPES)
    assert fe_values == be_values, (
        "FE/BE asset-type drift detected — keep "
        "frontend/src/pages/Indicators/assetTypes.js and "
        "tcg/core/indicators/asset_types.py in sync.\n"
        f"  FE values: {sorted(fe_values)}\n"
        f"  BE values: {sorted(be_values)}\n"
        f"  FE - BE: {sorted(fe_values - be_values)}\n"
        f"  BE - FE: {sorted(be_values - fe_values)}"
    )


def test_canonical_lowercase_literals() -> None:
    """All asset-type values are lowercase, non-empty, and exactly the
    expected literals — guards against accidental rename to e.g. 'Index'.
    """
    expected = {"index", "equity", "option"}
    assert set(ASSET_TYPES) == expected, (
        f"BE ASSET_TYPES drifted from canonical set; got {sorted(ASSET_TYPES)}"
    )
    fe = _extract_fe_asset_types()
    assert set(fe.values()) == expected, (
        f"FE asset-type values drifted from canonical set; got {sorted(fe.values())}"
    )
    # Constant naming is also part of documentation — assert it.
    assert fe.get("INDEX") == "index"
    assert fe.get("EQUITY") == "equity"
    assert fe.get("OPTION") == "option"
