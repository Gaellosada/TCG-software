"""Document parsing, ID serialization, eodDatas extraction, NaN sanitization.

All data leaving ``_mongo/`` must be NaN-free. Sanitization happens here
at the adapter boundary.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
from bson import ObjectId
from bson.errors import InvalidId

from tcg.types.market import AssetClass, InstrumentId, PriceSeries

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ID serialization
# ---------------------------------------------------------------------------

def serialize_doc_id(doc_id: Any) -> str:
    """Convert any MongoDB ``_id`` value to a string for external use.

    Handles ObjectId, string, dict (composite keys), and other types.
    """
    if isinstance(doc_id, ObjectId):
        return str(doc_id)
    if isinstance(doc_id, dict):
        # Composite key -- produce a stable string representation.
        # Sort keys for determinism.
        parts = [f"{k}={v}" for k, v in sorted(doc_id.items())]
        return "|".join(parts)
    return str(doc_id)


def deserialize_doc_id(doc_id_str: str) -> list[Any]:
    """Return candidate ``_id`` values to try when querying.

    The legacy Java platform stored ``_id`` as ObjectId, string, dict
    (composite key), or int depending on the collection. We try
    candidates in priority order: composite dict (if the string looks
    like ``key=val|key=val``), ObjectId, then raw string.
    """
    candidates: list[Any] = []

    # Composite key: "key1=val1|key2=val2" → {"key1": "val1", "key2": "val2"}
    if "|" in doc_id_str and "=" in doc_id_str:
        try:
            parts = doc_id_str.split("|")
            reconstructed = {}
            for part in parts:
                k, v = part.split("=", 1)
                reconstructed[k] = v
            candidates.append(reconstructed)
        except ValueError:
            pass  # Malformed — fall through to other candidates

    try:
        candidates.append(ObjectId(doc_id_str))
    except (InvalidId, TypeError):
        pass
    candidates.append(doc_id_str)
    return candidates


# ---------------------------------------------------------------------------
# Price data extraction
# ---------------------------------------------------------------------------

def extract_price_data(
    doc: dict[str, Any],
    provider: str | None = None,
) -> PriceSeries | None:
    """Parse ``eodDatas`` from a MongoDB document into a ``PriceSeries``.

    Parameters
    ----------
    doc:
        Raw MongoDB document containing ``eodDatas``.
    provider:
        If specified, use that provider's data. If ``None``, use the first
        available provider.

    Returns ``None`` if the document has no usable price data.
    """
    eod_datas = doc.get("eodDatas")
    if not eod_datas or not isinstance(eod_datas, dict):
        return None

    # Select provider
    if provider is not None:
        bars = eod_datas.get(provider)
        if bars is None:
            return None
    else:
        # Use first available provider
        first_key = next(iter(eod_datas), None)
        if first_key is None:
            return None
        bars = eod_datas[first_key]

    if not bars:
        return None

    doc_id_str = serialize_doc_id(doc.get("_id", "unknown"))

    # Pre-allocate lists for accepted bars
    dates_out: list[int] = []
    open_out: list[float] = []
    high_out: list[float] = []
    low_out: list[float] = []
    close_out: list[float] = []
    volume_out: list[float] = []

    for bar in bars:
        date_val = bar.get("date")
        if date_val is None:
            continue
        try:
            date_int = int(date_val)
        except (TypeError, ValueError):
            logger.warning(
                "Dropping bar with non-integer date: instrument=%s date=%r",
                doc_id_str,
                date_val,
            )
            continue

        close_val = _to_float(bar.get("close"))
        # Drop entire bar if close is NaN (architecture section 3.10)
        if close_val is None or math.isnan(close_val):
            logger.warning(
                "Dropping bar with NaN close: instrument=%s date=%s",
                doc_id_str,
                date_val,
            )
            continue

        open_val = _sanitize_non_critical(_to_float(bar.get("open")), 0.0)
        high_val = _sanitize_non_critical(_to_float(bar.get("high")), 0.0)
        low_val = _sanitize_non_critical(_to_float(bar.get("low")), 0.0)
        volume_val = _sanitize_non_critical(_to_float(bar.get("volume")), 0.0)

        dates_out.append(date_int)
        open_out.append(open_val)
        high_out.append(high_val)
        low_out.append(low_val)
        close_out.append(close_val)
        volume_out.append(volume_val)

    if not dates_out:
        return None

    # Sort by date (ascending)
    order = np.argsort(np.array(dates_out, dtype=np.int64))

    return PriceSeries(
        dates=np.array(dates_out, dtype=np.int64)[order],
        open=np.array(open_out, dtype=np.float64)[order],
        high=np.array(high_out, dtype=np.float64)[order],
        low=np.array(low_out, dtype=np.float64)[order],
        close=np.array(close_out, dtype=np.float64)[order],
        volume=np.array(volume_out, dtype=np.float64)[order],
    )


def _to_float(value: Any) -> float | None:
    """Safely convert a value to float. Returns None if impossible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize_non_critical(value: float | None, default: float) -> float:
    """Replace NaN or None in non-critical fields with *default*."""
    if value is None or math.isnan(value):
        return default
    return value


# ---------------------------------------------------------------------------
# Instrument ID parsing
# ---------------------------------------------------------------------------

def parse_instrument_id(doc: dict[str, Any], collection: str) -> InstrumentId:
    """Build an ``InstrumentId`` from a MongoDB document and its collection name.

    The asset class is inferred from the collection prefix.
    """
    symbol = serialize_doc_id(doc.get("_id", "unknown"))

    if collection.startswith("FUT_"):
        asset_class = AssetClass.FUTURE
    elif collection == "INDEX":
        asset_class = AssetClass.INDEX
    else:
        asset_class = AssetClass.EQUITY

    return InstrumentId(
        symbol=symbol,
        asset_class=asset_class,
        collection=collection,
    )
