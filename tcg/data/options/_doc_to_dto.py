"""Pure translation: raw Mongo OPT_* doc → frozen-dataclass DTOs.

These functions do NOT touch Mongo. They consume Python dicts and
produce ``OptionContractDoc`` / ``OptionDailyRow`` from
``tcg.types.options``. Keeping them dependency-free makes the unit
tests synthetic and fast (guardrail #10).

Cardinal rules baked in here:

- ``mid = (bid + ask) / 2`` only when both are present and positive;
  otherwise ``None``. ``close`` is never used as the primary mark
  (guardrail #4).
- ``type`` normalized to upper-case ``"C"`` / ``"P"``.
- ``atTheMoney``, ``moneyness``, ``daysToExpiry`` are read but
  intentionally discarded (guardrail #3).
- Missing fields surface as ``None``; never silently coerced to ``0``.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Literal, Mapping

from tcg.data._mongo.helpers import serialize_doc_id
from tcg.data.options._strike_factor import STRIKE_FACTOR_VERIFIED
from tcg.types.options import OptionContractDoc, OptionDailyRow


# ---------------------------------------------------------------------------
# Tiny coercion helpers (mirroring tcg.data._mongo.helpers conventions)
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    """Best-effort float; returns None on failure or NaN."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out):
        return None
    return out


def _parse_yyyymmdd(value: Any) -> date | None:
    """Parse YYYYMMDD ints, datetime, ISO strings; tolerate floats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return None
        if not (19000101 <= iv <= 21001231):
            return None
        try:
            return date(iv // 10000, (iv // 100) % 100, iv % 100)
        except ValueError:
            return None
    if isinstance(value, str):
        try:
            iv = int(value)
            if 19000101 <= iv <= 21001231:
                return date(iv // 10000, (iv // 100) % 100, iv % 100)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            return None
    return None


def _normalize_type(raw: Any) -> Literal["C", "P"] | None:
    """Upper-case-and-validate the call/put marker.

    OPT_VIX is observed with mixed case (``"c"/"p"/"C"/"P"`` per
    DB_SCHEMA_FINDINGS §3 row ``type``); everywhere else upper case.
    Anything that is not a single letter c/p/C/P returns None so the
    caller can decide to drop the row.
    """
    if not isinstance(raw, str):
        return None
    upper = raw.strip().upper()
    if upper in ("C", "P"):
        return upper  # type: ignore[return-value]
    return None


def _compute_mid(bid: float | None, ask: float | None) -> float | None:
    """``(bid + ask) / 2`` only if both are present and positive."""
    if bid is None or ask is None:
        return None
    if bid <= 0.0 or ask <= 0.0:
        return None
    return (bid + ask) / 2.0


def _sanitize_iv(value: float | None) -> float | None:
    """IV must be strictly positive. IVolatility uses ``-1.0`` as a
    "no IV" sentinel for deep-OTM rows where the solver does not converge;
    surface those as missing rather than as a stored negative value.
    """
    if value is None:
        return None
    if value <= 0.0:
        return None
    return value


# ---------------------------------------------------------------------------
# Contract metadata (one per option document)
# ---------------------------------------------------------------------------


def doc_to_contract(
    doc: Mapping[str, Any],
    collection: str,
    provider: str,
) -> OptionContractDoc | None:
    """Translate the static portion of a doc to ``OptionContractDoc``.

    Returns ``None`` if any of the contract-defining fields cannot be
    resolved (``expiration``, ``strike``, ``type``). Such docs are
    skipped at the reader level rather than half-populated.
    """
    expiration = _parse_yyyymmdd(doc.get("expiration"))
    strike = _to_float(doc.get("strike"))
    contract_type = _normalize_type(doc.get("type"))
    if expiration is None or strike is None or contract_type is None:
        return None

    contract_id = serialize_doc_id(doc.get("_id", "unknown"))

    # _id may be a dict {internalSymbol, expirationCycle}; pull cycle out.
    raw_id = doc.get("_id")
    cycle = ""
    if isinstance(raw_id, dict):
        cycle_val = raw_id.get("expirationCycle")
        if isinstance(cycle_val, str):
            cycle = cycle_val

    root_underlying_raw = doc.get("rootUnderlying")
    root_underlying: str = (
        root_underlying_raw if isinstance(root_underlying_raw, str) else ""
    )

    underlying_ref_raw = doc.get("underlying")
    underlying_ref = (
        underlying_ref_raw if isinstance(underlying_ref_raw, str) else None
    )

    underlying_symbol_raw = doc.get("underlyingSymbol")
    underlying_symbol = (
        underlying_symbol_raw
        if isinstance(underlying_symbol_raw, str)
        else None
    )

    contract_size = _to_float(doc.get("contractSize"))

    currency_raw = doc.get("currency")
    currency = currency_raw if isinstance(currency_raw, str) else None

    return OptionContractDoc(
        collection=collection,
        contract_id=contract_id,
        root_underlying=root_underlying,
        underlying_ref=underlying_ref,
        underlying_symbol=underlying_symbol,
        expiration=expiration,
        expiration_cycle=cycle,
        strike=strike,
        type=contract_type,
        contract_size=contract_size,
        currency=currency,
        provider=provider,
        strike_factor_verified=STRIKE_FACTOR_VERIFIED.get(collection, False),
    )


# ---------------------------------------------------------------------------
# Per-day row (eodDatas + eodGreeks merged on `date`)
# ---------------------------------------------------------------------------


def bar_and_greek_to_row(
    bar: Mapping[str, Any],
    greek: Mapping[str, Any] | None,
) -> OptionDailyRow | None:
    """Merge one ``eodDatas`` bar with the matching ``eodGreeks`` entry.

    *greek* is None when the row has no Greek snapshot for that day or
    when the root has its Greeks blocked entirely (OPT_VIX / OPT_ETH).
    Returns ``None`` when the bar has no parseable date.

    Stored fields ``atTheMoney`` / ``moneyness`` / ``daysToExpiry`` from
    *greek* are intentionally ignored (guardrail #3).
    """
    row_date = _parse_yyyymmdd(bar.get("date"))
    if row_date is None:
        return None

    bid = _to_float(bar.get("bid"))
    ask = _to_float(bar.get("ask"))

    iv_stored = _sanitize_iv(_to_float(greek.get("impliedVolatility"))) if greek else None
    delta_stored = _to_float(greek.get("delta")) if greek else None
    gamma_stored = _to_float(greek.get("gamma")) if greek else None
    theta_stored = _to_float(greek.get("theta")) if greek else None
    vega_stored = _to_float(greek.get("vega")) if greek else None
    underlying_price_stored = (
        _to_float(greek.get("underlyingPrice")) if greek else None
    )

    return OptionDailyRow(
        date=row_date,
        open=_to_float(bar.get("open")),
        high=_to_float(bar.get("high")),
        low=_to_float(bar.get("low")),
        close=_to_float(bar.get("close")),
        bid=bid,
        ask=ask,
        bid_size=_to_float(bar.get("bidSize")),
        ask_size=_to_float(bar.get("askSize")),
        volume=_to_float(bar.get("volume")),
        open_interest=_to_float(bar.get("openInterest")),
        mid=_compute_mid(bid, ask),
        iv_stored=iv_stored,
        delta_stored=delta_stored,
        gamma_stored=gamma_stored,
        theta_stored=theta_stored,
        vega_stored=vega_stored,
        underlying_price_stored=underlying_price_stored,
    )


def index_greeks_by_date(
    greeks_list: list[Mapping[str, Any]] | None,
) -> dict[date, Mapping[str, Any]]:
    """Build a ``{date → greek-entry}`` index for one provider's array."""
    out: dict[date, Mapping[str, Any]] = {}
    if not greeks_list:
        return out
    for entry in greeks_list:
        d = _parse_yyyymmdd(entry.get("date"))
        if d is None:
            continue
        out[d] = entry
    return out
