"""Asset-type vocabulary for indicators (backend side).

Single source of truth on the BE for the three asset types an indicator
can be declared compatible with. The matching JS module lives at
``frontend/src/pages/Indicators/assetTypes.js`` and the parity test
``tests/api/test_asset_type_parity.py`` fails loud if either side gains
or drops a value.

Conventions
-----------
* Lowercase string literals (``"index"``, ``"equity"``, ``"option"``)
  are the canonical wire/storage form. Match the FE.
* UPPERCASE module-level constants (``INDEX``, ``EQUITY``, ``OPTION``)
  must be used in code — never hard-code the literal.
* ``ASSET_TYPES`` is a frozenset of all three values, suitable for
  membership checks.

Collection-classification heuristic (mirrors
``InstrumentPickerModal``'s CATEGORY_CONFIG and the FE
``inferAssetType``):

* ``INDEX`` collection           → "index"
* ``OPT_*`` collection prefix    → "option"
* ``ETF`` / ``FOREX`` / ``FUND`` → "equity"
* ``FUT_*`` collection prefix    → "equity" (continuous-future stream)
* anything else / missing field  → ``None`` (caller routes on the null)

``infer_asset_type`` is exported and unit-tested but is NOT called from
production code paths in this wave (Wave 2a is metadata only). Wave 2b
wires it into request validation; keep the helper pure.
"""

from __future__ import annotations

from typing import Any, Literal

INDEX: Literal["index"] = "index"
EQUITY: Literal["equity"] = "equity"
OPTION: Literal["option"] = "option"

# Frozenset of all known asset-type literals. The FE ``ASSET_TYPES`` array
# must contain exactly the same string values — enforced by the parity test.
ASSET_TYPES: frozenset[str] = frozenset({INDEX, EQUITY, OPTION})

AssetType = Literal["index", "equity", "option"]


def infer_asset_type(series_ref: Any) -> AssetType | None:
    """Infer asset-type from a SeriesRef-shaped value.

    Accepts either a Pydantic ``SpotInstrumentRef`` / ``ContinuousInstrumentRef``
    instance (anything exposing a ``collection`` string attribute) or a plain
    dict with a ``"collection"`` key. Returns one of the asset-type literals
    or ``None`` for unknown / unrecognised inputs.

    The ``None`` is meaningful — callers route on it (do not coerce to an
    empty string or guess "equity"). Sign 10: no silent failure.
    """
    if series_ref is None:
        return None

    # Support both Pydantic models (attribute access) and plain dicts.
    if isinstance(series_ref, dict):
        collection = series_ref.get("collection")
    else:
        collection = getattr(series_ref, "collection", None)

    if not isinstance(collection, str) or not collection:
        return None

    if collection == "INDEX":
        return INDEX
    if collection.startswith("OPT_"):
        return OPTION
    if collection.startswith("FUT_"):
        return EQUITY
    if collection in ("ETF", "FOREX", "FUND"):
        return EQUITY
    return None
