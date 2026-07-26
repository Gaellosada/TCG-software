"""v1 ⇄ v2 identity mapping — the only place that knows both dialects.

Decision D1 (spec §2.1): the v2 adapter emits **v1-style text symbols** and
v1-style collection names. v2 identity (``contract_code``, and the integer
``object_id`` / ``contract_id`` / ``serie_id``) never crosses the adapter
boundary, so one saved portfolio runs unchanged on both warehouses.

The forward direction (v1 symbol → v2 keys) is a pure total function on the
S&P 500 family — no lookup table. The reverse is a single indexed query keyed
on ``expiration`` (futures) or ``(expiration, strike, option_type)`` (options).

Do NOT parse the CME month/year code. ``ESZ0`` is ambiguous — the single year
digit covers 2010, 2020 AND 2030, and ``ESZ0.20101217`` and ``ESZ0.20301220``
both exist live. The ``.YYYYMMDD`` expiration suffix is the unambiguous key.

This module's public API is FROZEN: worker 2b's options reader is written
against these exact signatures on a parallel branch.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Mapping

from tcg.data._utils import date_to_int, int_to_date
from tcg.data._v2_compat.errors import V2SymbolError, V2UnsupportedCycle

# --- v2 object identity (spec §1.1, live-verified) ------------------------- #

V2_INDEX_OBJECT_ID: int = 5
V2_INDEX_SERIE_ID: int = 5
V2_FUTURES_OBJECT_ID: int = 6

# --- v1 names the adapter emits (spec §1.3) -------------------------------- #

V2_INDEX_SYMBOL: str = "IND_SP_500"
V2_INDEX_COLLECTION: str = "INDEX"
V2_FUTURES_COLLECTION: str = "FUT_SP_500"
V2_OPTIONS_COLLECTION: str = "OPT_SP_500"

# --- Weekly option roots (spec §3.2) --------------------------------------- #
#
# One logical v1 collection (OPT_SP_500) fans out to four EW root objects,
# routed per contract by ``expiration_cycle``. Note EW3 is object 7, not 13 —
# the ids are not in week order.
EW_OBJECT_BY_CYCLE: Mapping[str, int] = {
    "W1 Friday": 11,
    "W2 Friday": 12,
    "W3 Friday": 7,
    "W4 Friday": 13,
}
EW_OBJECT_IDS: tuple[int, ...] = (11, 12, 7, 13)

# v1 ``underlying_symbol`` per routed object — mirrored verbatim onto
# ``OptionContractDoc.underlying_symbol`` (spec §6.7).
EW_UNDERLYING_BY_OBJECT: Mapping[int, str] = {
    11: "EW1",
    12: "EW2",
    7: "EW3",
    13: "EW4",
}
EW_CYCLE_BY_OBJECT: Mapping[int, str] = {
    obj: cycle for cycle, obj in EW_OBJECT_BY_CYCLE.items()
}

# Collections the v2 warehouse can serve. Everything else raises (spec §1.4).
V2_SUPPORTED_COLLECTIONS: frozenset[str] = frozenset(
    {V2_INDEX_COLLECTION, V2_FUTURES_COLLECTION, V2_OPTIONS_COLLECTION}
)

# --- Symbol grammar (spec §2.3, §2.4) -------------------------------------- #

_FUT_PREFIX = "FUT_SP_500_EMINI_"
_OPT_PREFIX = "OPT_FUT_SP_500_EMINI_"
_FUT_GRAMMAR = "FUT_SP_500_EMINI_<YYYYMMDD>"
_OPT_GRAMMAR = "OPT_FUT_SP_500_EMINI_<YYYYMMDD>_<strike>_<C|P>"

# v2 stores the option type as a lowercase word; v1 as a single char.
_TYPE_TO_V2 = {"C": "call", "P": "put", "call": "call", "put": "put"}
_TYPE_FROM_V2 = {"call": "C", "put": "P", "C": "C", "P": "P"}

# Sentinel ts bounds when the caller leaves start/end open. Generous but
# constant, so the planner still gets a bounded range to BRIN-scan (Sign 6).
_MIN_DATE = date(1900, 1, 1)
_MAX_DATE = date(2100, 12, 31)


def v2_supports_collection(collection: str) -> bool:
    """Return True when *collection* has any v2 representation (spec §1.3)."""
    return collection in V2_SUPPORTED_COLLECTIONS


def ew_object_for_cycle(cycle: str) -> int:
    """Map a v1 ``expiration_cycle`` tag to its v2 EW object id.

    Raises :class:`V2UnsupportedCycle` for ``'M'``, ``''`` and anything else —
    v2 has no monthly S&P 500 options (spec §3.3b / §11 E3).
    """
    obj = EW_OBJECT_BY_CYCLE.get(cycle)
    if obj is None:
        raise V2UnsupportedCycle(cycle)
    return obj


def futures_symbol_from_expiration(expiration_int: int) -> str:
    """``20260619`` → ``'FUT_SP_500_EMINI_20260619'`` (spec §2.3)."""
    _validate_date_int(expiration_int, _FUT_GRAMMAR)
    return f"{_FUT_PREFIX}{expiration_int:08d}"


def expiration_int_from_futures_symbol(symbol: str) -> int:
    """Inverse of :func:`futures_symbol_from_expiration`.

    Raises :class:`V2SymbolError` on anything that is not a v1 ES futures
    symbol. Note an OPTION symbol contains the futures prefix as a substring
    but starts with ``OPT_``, so the strict ``startswith`` below rejects it.
    """
    if not symbol.startswith(_FUT_PREFIX):
        raise V2SymbolError(symbol, _FUT_GRAMMAR)
    tail = symbol[len(_FUT_PREFIX) :]
    if not tail.isdigit() or len(tail) != 8:
        raise V2SymbolError(symbol, _FUT_GRAMMAR)
    return _validate_date_int(int(tail), _FUT_GRAMMAR, symbol)


def option_symbol_from_parts(
    expiration_int: int,
    strike: float,
    option_type: str,
) -> str:
    """``(20260605, 7455.0, 'P')`` → ``'OPT_FUT_SP_500_EMINI_20260605_7455_P'``.

    Strikes are integral on BOTH warehouses (live-verified, zero fractional
    strikes on either side), so the integer rendering is total and lossless. A
    fractional strike is a contract shape neither side has ever stored — it
    raises rather than silently truncating.
    """
    _validate_date_int(expiration_int, _OPT_GRAMMAR)
    if strike != int(strike):
        raise V2SymbolError(
            f"strike={strike}",
            f"{_OPT_GRAMMAR} with an INTEGER strike",
        )
    return (
        f"{_OPT_PREFIX}{expiration_int:08d}"
        f"_{int(strike)}_{option_type_from_v2(option_type)}"
    )


def option_parts_from_symbol(symbol: str) -> tuple[int, int, str]:
    """Inverse of :func:`option_symbol_from_parts`.

    Returns ``(expiration_int, strike_int, 'C'|'P')``. Raises
    :class:`V2SymbolError` on a malformed symbol.
    """
    if not symbol.startswith(_OPT_PREFIX):
        raise V2SymbolError(symbol, _OPT_GRAMMAR)
    tail = symbol[len(_OPT_PREFIX) :]
    parts = tail.split("_")
    if len(parts) != 3:
        raise V2SymbolError(symbol, _OPT_GRAMMAR)
    exp_s, strike_s, type_s = parts
    if not exp_s.isdigit() or len(exp_s) != 8 or not strike_s.isdigit():
        raise V2SymbolError(symbol, _OPT_GRAMMAR)
    if type_s not in ("C", "P"):
        raise V2SymbolError(symbol, _OPT_GRAMMAR)
    return _validate_date_int(int(exp_s), _OPT_GRAMMAR, symbol), int(strike_s), type_s


def option_type_to_v2(t: str) -> str:
    """``'C'``/``'call'`` → ``'call'``; ``'P'``/``'put'`` → ``'put'``."""
    v = _TYPE_TO_V2.get(t)
    if v is None:
        raise V2SymbolError(t, "one of 'C', 'P', 'call', 'put'")
    return v


def option_type_from_v2(t: str) -> str:
    """``'call'`` → ``'C'``; ``'put'`` → ``'P'`` (idempotent on ``C``/``P``)."""
    v = _TYPE_FROM_V2.get(t)
    if v is None:
        raise V2SymbolError(t, "one of 'C', 'P', 'call', 'put'")
    return v


def ts_to_date_int(ts: datetime) -> int:
    """v2 ``timestamptz`` → engine YYYYMMDD int, in UTC (spec §7).

    Always converts explicitly rather than trusting the session timezone, so a
    server-side timezone change cannot shift a bar by a day.
    """
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return date_to_int(ts.date())


def date_int_bounds(
    start: date | None,
    end: date | None,
) -> tuple[datetime, datetime]:
    """Engine date range → half-open ``[lo, hi)`` UTC ``ts`` bounds (spec §7).

    Half-open beats ``BETWEEN``: *end* stays inclusive of its whole day even if
    a non-midnight fact is ever loaded. Both bounds are always returned (never
    ``None``) so every fact query carries a constant range for BRIN.
    """
    lo_d = start if start is not None else _MIN_DATE
    hi_d = (end if end is not None else _MAX_DATE) + timedelta(days=1)
    lo = datetime(lo_d.year, lo_d.month, lo_d.day, tzinfo=timezone.utc)
    hi = datetime(hi_d.year, hi_d.month, hi_d.day, tzinfo=timezone.utc)
    return lo, hi


def _validate_date_int(value: int, grammar: str, original: str | None = None) -> int:
    """Reject a YYYYMMDD int that is not a real calendar date."""
    try:
        int_to_date(value)
    except ValueError as exc:
        raise V2SymbolError(original or str(value), grammar) from exc
    return value
