"""v1 ⇄ v2 identifier mapping — the bidirectional symbol/cycle/date grammar.

Pure functions and constants only: no I/O, no SQL, no DB handle. Everything
here is a live-validated restatement of mapping spec §2 (symbol grammar), §3
(expiration-cycle routing) and §7 (dates/timezone).

The v2 star schema (``tcg_instruments_v2``) keys everything by integer
``object_id`` / ``contract_id``, while every layer above ``tcg.data`` speaks v1
symbols (``IND_SP_500``, ``FUT_SP_500_EMINI_20260619``,
``OPT_FUT_SP_500_EMINI_20260605_7455_P``). This module is the only place that
translation happens, so the integer ids never cross the adapter boundary
(spec §2.5).

Frozen public API — Wave 2a and Wave 2b implement it independently, so no name,
signature or semantic here may drift.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Mapping

from tcg.data._v2_compat.errors import V2SymbolError, V2UnsupportedCycle

# --------------------------------------------------------------------------- #
# Object ids (spec §1.1, live-probed)
# --------------------------------------------------------------------------- #

#: ``tcg_instruments_v2.object`` row for the S&P 500 cash index.
V2_INDEX_OBJECT_ID: int = 5
#: The index's single non-contract ``bar`` serie.
V2_INDEX_SERIE_ID: int = 5
#: ``tcg_instruments_v2.object`` row for the E-mini S&P 500 future.
V2_FUTURES_OBJECT_ID: int = 6

V2_INDEX_SYMBOL: str = "IND_SP_500"
V2_INDEX_COLLECTION: str = "INDEX"
V2_FUTURES_COLLECTION: str = "FUT_SP_500"
V2_OPTIONS_COLLECTION: str = "OPT_SP_500"

#: v1 ``expiration_cycle`` tag → v2 EW option ``object_id`` (spec §3.2).
#: Note EW3 is object **7**, not 9/10 — the ids are not contiguous.
EW_OBJECT_BY_CYCLE: Mapping[str, int] = {
    "W1 Friday": 11,
    "W2 Friday": 12,
    "W3 Friday": 7,
    "W4 Friday": 13,
}

#: The four EW option objects, in cycle order W1..W4 (NOT numeric order).
EW_OBJECT_IDS: tuple[int, ...] = (11, 12, 7, 13)

#: v1 collections that the v2 warehouse can serve at all (spec §1.3/§1.4).
_V2_COLLECTIONS: frozenset[str] = frozenset(
    {V2_INDEX_COLLECTION, V2_FUTURES_COLLECTION, V2_OPTIONS_COLLECTION}
)

_FUTURES_SYMBOL_PREFIX = "FUT_SP_500_EMINI_"
_OPTION_SYMBOL_PREFIX = "OPT_FUT_SP_500_EMINI_"

_CALL_TOKENS = frozenset({"C", "CALL"})
_PUT_TOKENS = frozenset({"P", "PUT"})


# --------------------------------------------------------------------------- #
# Collections and cycles
# --------------------------------------------------------------------------- #


def v2_supports_collection(collection: str) -> bool:
    """True when *collection* has a v2 counterpart (spec §1.3)."""
    return collection in _V2_COLLECTIONS


def ew_object_for_cycle(cycle: str) -> int:
    """v1 ``expiration_cycle`` tag → v2 EW ``object_id``.

    Raises
    ------
    V2UnsupportedCycle
        For ``"M"`` (the monthly 3rd-Friday series, 74,930 v1 contracts with no
        v2 counterpart at all), for the empty tag, and for anything else v2
        cannot route. The message names the offending tag and the fix.
    """
    obj = EW_OBJECT_BY_CYCLE.get(cycle)
    if obj is None:
        shown = repr(cycle) if cycle else "'' (empty)"
        raise V2UnsupportedCycle(
            f'Data source "v2" has no S&P 500 options on expiration cycle '
            f"{shown} — it covers only the weekly EW1-EW4 series. Choose a "
            f"weekly cycle ({', '.join(EW_OBJECT_BY_CYCLE)}), or switch this "
            f'run to data source "v1".'
        )
    return obj


# --------------------------------------------------------------------------- #
# Futures symbols
# --------------------------------------------------------------------------- #


def futures_symbol_from_expiration(expiration_int: int) -> str:
    """``20260619`` → ``"FUT_SP_500_EMINI_20260619"`` (spec §2.3)."""
    return f"{_FUTURES_SYMBOL_PREFIX}{int(expiration_int):08d}"


def expiration_int_from_futures_symbol(symbol: str) -> int:
    """Inverse of :func:`futures_symbol_from_expiration`.

    Raises
    ------
    V2SymbolError
        When *symbol* is not an E-mini futures symbol of the expected grammar.
    """
    if not symbol or not symbol.startswith(_FUTURES_SYMBOL_PREFIX):
        raise V2SymbolError(
            f"Symbol {symbol!r} is not an E-mini futures symbol: expected "
            f"'{_FUTURES_SYMBOL_PREFIX}<YYYYMMDD>'."
        )
    tail = symbol[len(_FUTURES_SYMBOL_PREFIX) :]
    if len(tail) != 8 or not tail.isdigit():
        raise V2SymbolError(
            f"Symbol {symbol!r} has a malformed expiration segment {tail!r}: "
            f"expected 8 digits (YYYYMMDD)."
        )
    return int(tail)


# --------------------------------------------------------------------------- #
# Option symbols
# --------------------------------------------------------------------------- #


def option_type_to_v2(t: str) -> str:
    """``'C'``/``'call'`` → ``'call'``; ``'P'``/``'put'`` → ``'put'``.

    v1 stores a ``char`` (``'C'``/``'P'``); v2 stores the lowercase word.
    """
    up = (t or "").strip().upper()
    if up in _CALL_TOKENS:
        return "call"
    if up in _PUT_TOKENS:
        return "put"
    raise V2SymbolError(
        f"Option type {t!r} is not recognised: expected 'C'/'P' (v1) or "
        f"'call'/'put' (v2)."
    )


def option_type_from_v2(t: str) -> str:
    """``'call'`` → ``'C'``; ``'put'`` → ``'P'`` (the DTO's ``type`` field)."""
    up = (t or "").strip().upper()
    if up in _CALL_TOKENS:
        return "C"
    if up in _PUT_TOKENS:
        return "P"
    raise V2SymbolError(
        f"Option type {t!r} is not recognised: expected 'call'/'put' (v2) or "
        f"'C'/'P' (v1)."
    )


def option_symbol_from_parts(
    expiration_int: int, strike: float, option_type: str
) -> str:
    """Build the v1 option symbol (spec §2.4).

    ``(20260605, 7455.0, 'P')`` → ``"OPT_FUT_SP_500_EMINI_20260605_7455_P"``.

    Strikes are integral on BOTH sides (spec §2.4 VERIFIED: zero fractional
    strikes in either warehouse), so the integer rendering is lossless.
    """
    kind = option_type_from_v2(option_type)
    return f"{_OPTION_SYMBOL_PREFIX}{int(expiration_int):08d}_{int(strike)}_{kind}"


def option_parts_from_symbol(symbol: str) -> tuple[int, int, str]:
    """Inverse of :func:`option_symbol_from_parts`.

    Returns ``(expiration_int, strike_int, 'C' | 'P')``.

    Raises
    ------
    V2SymbolError
        When *symbol* does not match the v1 E-mini option grammar.
    """
    if not symbol or not symbol.startswith(_OPTION_SYMBOL_PREFIX):
        raise V2SymbolError(
            f"Symbol {symbol!r} is not an E-mini option symbol: expected "
            f"'{_OPTION_SYMBOL_PREFIX}<YYYYMMDD>_<strike>_<C|P>'."
        )
    parts = symbol[len(_OPTION_SYMBOL_PREFIX) :].split("_")
    if len(parts) != 3:
        raise V2SymbolError(
            f"Symbol {symbol!r} has {len(parts)} trailing segments, expected 3 "
            f"(<YYYYMMDD>_<strike>_<C|P>)."
        )
    exp_s, strike_s, type_s = parts
    if len(exp_s) != 8 or not exp_s.isdigit():
        raise V2SymbolError(
            f"Symbol {symbol!r} has a malformed expiration segment {exp_s!r}: "
            f"expected 8 digits (YYYYMMDD)."
        )
    if not strike_s.isdigit():
        raise V2SymbolError(
            f"Symbol {symbol!r} has a malformed strike segment {strike_s!r}: "
            f"expected an integer (v1 renders strikes without decimals)."
        )
    return int(exp_s), int(strike_s), option_type_from_v2(type_s)


# --------------------------------------------------------------------------- #
# Dates (spec §7) — v2 stamps every daily fact at exactly 00:00 UTC
# --------------------------------------------------------------------------- #

#: Bounds used when a caller leaves an endpoint open. Wide enough to cover the
#: whole warehouse (v2's earliest fact is 2005, its latest 2026) while keeping
#: the constant ``ts`` predicate that helps the BRIN index (guardrail Sign 6).
_TS_FLOOR = datetime(1900, 1, 1, tzinfo=timezone.utc)
_TS_CEIL = datetime(2100, 1, 1, tzinfo=timezone.utc)


def ts_to_date_int(ts: datetime) -> int:
    """v2 ``timestamptz`` → engine ``YYYYMMDD`` int, in UTC (spec §7).

    Always converts explicitly, so a server/session timezone change cannot
    shift a bar by a day. A naive datetime is read as UTC (every v2 daily fact
    is stored at 00:00 UTC).
    """
    if ts.tzinfo is None:
        d = ts.date()
    else:
        d = ts.astimezone(timezone.utc).date()
    return d.year * 10000 + d.month * 100 + d.day


def date_int_bounds(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    """Engine date range → half-open ``[lo, hi)`` UTC ``ts`` bounds (spec §7).

    Use as ``WHERE ts >= lo AND ts < hi``. The half-open form is preferred over
    ``BETWEEN``: with a ``timestamptz`` column a bare-date upper bound is
    fragile the moment a non-midnight row appears.
    """
    lo = (
        _TS_FLOOR
        if start is None
        else datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    )
    hi = (
        _TS_CEIL
        if end is None
        else datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
        + timedelta(days=1)
    )
    return lo, hi
