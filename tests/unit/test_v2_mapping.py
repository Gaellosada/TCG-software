"""Unit tests for the v1 ⇄ v2 identity mapping and the v2 error taxonomy.

The round-trip properties are the load-bearing ones: the adapter's whole
premise (decision D1) is that a saved portfolio's v1 symbols survive a trip
through v2 unchanged. If symbol → parts → symbol is not the identity, one
strategy silently becomes two.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.core.api.errors import STATUS_MAP
from tcg.data._v2_compat._mapping import (
    EW_OBJECT_BY_CYCLE,
    EW_OBJECT_IDS,
    V2_FUTURES_COLLECTION,
    V2_FUTURES_OBJECT_ID,
    V2_INDEX_COLLECTION,
    V2_INDEX_OBJECT_ID,
    V2_INDEX_SERIE_ID,
    V2_INDEX_SYMBOL,
    V2_OPTIONS_COLLECTION,
    date_int_bounds,
    ew_object_for_cycle,
    expiration_int_from_futures_symbol,
    futures_symbol_from_expiration,
    option_parts_from_symbol,
    option_symbol_from_parts,
    option_type_from_v2,
    option_type_to_v2,
    ts_to_date_int,
    v2_supports_collection,
)
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2DataUnavailable,
    V2InstrumentUnavailable,
    V2SymbolError,
    V2UnsupportedCycle,
    V2UnsupportedField,
)
from tcg.types.errors import DataAccessError


# --- Constants (spec §1.1, live-verified) ---------------------------------- #


def test_object_ids_match_live_v2_schema():
    assert V2_INDEX_OBJECT_ID == 5
    assert V2_INDEX_SERIE_ID == 5
    assert V2_FUTURES_OBJECT_ID == 6
    assert V2_INDEX_SYMBOL == "IND_SP_500"
    assert V2_INDEX_COLLECTION == "INDEX"
    assert V2_FUTURES_COLLECTION == "FUT_SP_500"
    assert V2_OPTIONS_COLLECTION == "OPT_SP_500"


def test_ew_routing_table():
    """EW3 is object 7, NOT 13 — the ids are not in week order."""
    assert EW_OBJECT_BY_CYCLE == {
        "W1 Friday": 11,
        "W2 Friday": 12,
        "W3 Friday": 7,
        "W4 Friday": 13,
    }
    assert EW_OBJECT_IDS == (11, 12, 7, 13)


@pytest.mark.parametrize("cycle,obj", list(EW_OBJECT_BY_CYCLE.items()))
def test_ew_object_for_cycle(cycle, obj):
    assert ew_object_for_cycle(cycle) == obj


def test_objects_for_cycle_adds_the_quarterly_object_to_w3_only():
    """"W3 Friday" serves the standard quarterly ES option (14) IN ADDITION to
    EW3 (7): serial-month 3rd Fridays come from 7, quarterly-month ones from 14.
    Object 14 is quarterly-only, so it must not appear on any other cycle."""
    from tcg.data._v2_compat._mapping import (
        ALL_OPTION_OBJECT_IDS,
        V2_QUARTERLY_OBJECT_ID,
        objects_for_cycle,
    )

    assert V2_QUARTERLY_OBJECT_ID == 14
    assert set(objects_for_cycle("W3 Friday")) == {7, 14}
    assert objects_for_cycle("W1 Friday") == (11,)
    assert objects_for_cycle("W2 Friday") == (12,)
    assert objects_for_cycle("W4 Friday") == (13,)
    for cycle in ("W1 Friday", "W2 Friday", "W4 Friday"):
        assert 14 not in objects_for_cycle(cycle)
    # The union used by existence/coverage paths carries every option object
    # (never the underlying FUTURE object 6).
    assert set(ALL_OPTION_OBJECT_IDS) == {11, 12, 7, 13, 14}
    assert 6 not in ALL_OPTION_OBJECT_IDS


@pytest.mark.parametrize("bad", ["M", "", "W", "Q", "W5 Friday"])
def test_objects_for_cycle_rejects_non_weekly(bad):
    from tcg.data._v2_compat._mapping import objects_for_cycle

    with pytest.raises(V2UnsupportedCycle):
        objects_for_cycle(bad)


@pytest.mark.parametrize("bad", ["M", "", "W", "Q", "W5 Friday", "w1 friday"])
def test_ew_object_for_cycle_rejects_non_weekly(bad):
    """'M' has 74,930 v1 contracts and no v2 counterpart — must fail loudly."""
    with pytest.raises(V2UnsupportedCycle):
        ew_object_for_cycle(bad)


# --- Collection gating ------------------------------------------------------ #


@pytest.mark.parametrize("coll", ["INDEX", "FUT_SP_500", "OPT_SP_500"])
def test_supported_collections(coll):
    assert v2_supports_collection(coll) is True


@pytest.mark.parametrize(
    "coll",
    ["ETF", "FUND", "FOREX", "FUT_VIX", "FUT_NASDAQ_100", "OPT_VIX", "IND_SP_500", ""],
)
def test_unsupported_collections(coll):
    """Note IND_SP_500 is an INSTRUMENT, not a collection (spec §1.2)."""
    assert v2_supports_collection(coll) is False


# --- Futures symbol grammar -------------------------------------------------- #


def test_futures_symbol_worked_examples():
    assert futures_symbol_from_expiration(20100618) == "FUT_SP_500_EMINI_20100618"
    assert futures_symbol_from_expiration(20260619) == "FUT_SP_500_EMINI_20260619"
    assert expiration_int_from_futures_symbol("FUT_SP_500_EMINI_20301220") == 20301220


def test_futures_symbol_ambiguous_cme_code_is_never_parsed():
    """ESZ0 covers 2010, 2020 AND 2030 — the date, not the code, is the key."""
    a = futures_symbol_from_expiration(20101217)
    b = futures_symbol_from_expiration(20301220)
    assert a != b
    assert expiration_int_from_futures_symbol(a) == 20101217
    assert expiration_int_from_futures_symbol(b) == 20301220


@pytest.mark.parametrize(
    "bad",
    [
        "FUT_SP_500_EMINI_2026061",  # 7 digits
        "FUT_SP_500_EMINI_202606190",  # 9 digits
        "FUT_SP_500_EMINI_ESM0",  # CME code, not a date
        "FUT_SP_500_EMINI_",
        "FUT_VIX_20260619",
        "IND_SP_500",
        "",
        "OPT_FUT_SP_500_EMINI_20260605_7455_P",  # option, not a future
        "FUT_SP_500_EMINI_20261345",  # month 13, day 45
    ],
)
def test_futures_symbol_rejects_malformed(bad):
    with pytest.raises(V2SymbolError):
        expiration_int_from_futures_symbol(bad)


# --- Option symbol grammar --------------------------------------------------- #


def test_option_symbol_worked_examples():
    assert (
        option_symbol_from_parts(20260605, 7455.0, "P")
        == "OPT_FUT_SP_500_EMINI_20260605_7455_P"
    )
    assert (
        option_symbol_from_parts(20260605, 100.0, "call")
        == "OPT_FUT_SP_500_EMINI_20260605_100_C"
    )
    assert option_parts_from_symbol("OPT_FUT_SP_500_EMINI_20260605_2000_P") == (
        20260605,
        2000,
        "P",
    )


def test_option_symbol_rejects_fractional_strike():
    """Zero fractional strikes exist on EITHER warehouse — never truncate."""
    with pytest.raises(V2SymbolError):
        option_symbol_from_parts(20260605, 7455.5, "P")


@pytest.mark.parametrize(
    "bad",
    [
        "OPT_FUT_SP_500_EMINI_20260605_7455",  # no type
        "OPT_FUT_SP_500_EMINI_20260605_7455_X",  # bad type
        "OPT_FUT_SP_500_EMINI_20260605_74.5_P",  # fractional
        "OPT_FUT_SP_500_EMINI_2026_7455_P",  # short date
        "FUT_SP_500_EMINI_20260619",  # future, not an option
        "OPT_VIX_20260605_20_P",
        "",
    ],
)
def test_option_symbol_rejects_malformed(bad):
    with pytest.raises(V2SymbolError):
        option_parts_from_symbol(bad)


@pytest.mark.parametrize("t,v2", [("C", "call"), ("P", "put"), ("call", "call")])
def test_option_type_to_v2(t, v2):
    assert option_type_to_v2(t) == v2


@pytest.mark.parametrize("v2,t", [("call", "C"), ("put", "P"), ("C", "C")])
def test_option_type_from_v2(v2, t):
    assert option_type_from_v2(v2) == t


@pytest.mark.parametrize("bad", ["X", "CALL", "", "both"])
def test_option_type_rejects_unknown(bad):
    with pytest.raises(V2SymbolError):
        option_type_to_v2(bad)
    with pytest.raises(V2SymbolError):
        option_type_from_v2(bad)


# --- Round-trip properties (the load-bearing ones) --------------------------- #


@settings(max_examples=200, deadline=None)
@given(st.dates(min_value=date(1980, 1, 1), max_value=date(2050, 12, 31)))
def test_futures_symbol_roundtrip_is_identity(d):
    exp_int = d.year * 10000 + d.month * 100 + d.day
    sym = futures_symbol_from_expiration(exp_int)
    assert expiration_int_from_futures_symbol(sym) == exp_int
    assert (
        futures_symbol_from_expiration(expiration_int_from_futures_symbol(sym)) == sym
    )


@settings(max_examples=200, deadline=None)
@given(
    st.dates(min_value=date(1980, 1, 1), max_value=date(2050, 12, 31)),
    st.integers(min_value=1, max_value=99999),
    st.sampled_from(["C", "P"]),
)
def test_option_symbol_roundtrip_is_identity(d, strike, opt_type):
    exp_int = d.year * 10000 + d.month * 100 + d.day
    sym = option_symbol_from_parts(exp_int, float(strike), opt_type)
    parsed_exp, parsed_strike, parsed_type = option_parts_from_symbol(sym)
    assert (parsed_exp, parsed_strike, parsed_type) == (exp_int, strike, opt_type)
    assert (
        option_symbol_from_parts(parsed_exp, float(parsed_strike), parsed_type) == sym
    )


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(["C", "P"]))
def test_option_type_roundtrip(t):
    assert option_type_from_v2(option_type_to_v2(t)) == t


# --- Dates and timezone (spec §7) -------------------------------------------- #


def test_ts_to_date_int_utc_midnight():
    assert ts_to_date_int(datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)) == 20260605


def test_ts_to_date_int_converts_rather_than_truncating():
    """A non-UTC ts must be CONVERTED, not read in its own zone.

    2026-06-05 21:00 UTC-05:00 is 2026-06-06 02:00Z — a different DAY. Reading
    the local date would shift the bar back one day.
    """
    tz = timezone(timedelta(hours=-5))
    assert ts_to_date_int(datetime(2026, 6, 5, 21, 0, tzinfo=tz)) == 20260606


def test_ts_to_date_int_naive_is_taken_as_utc():
    assert ts_to_date_int(datetime(2026, 6, 5, 0, 0)) == 20260605


def test_date_int_bounds_is_half_open_and_end_inclusive():
    lo, hi = date_int_bounds(date(2026, 1, 2), date(2026, 6, 11))
    assert lo == datetime(2026, 1, 2, tzinfo=timezone.utc)
    # hi is the day AFTER end, so the whole of 2026-06-11 is captured by ts < hi
    assert hi == datetime(2026, 6, 12, tzinfo=timezone.utc)


def test_date_int_bounds_always_bounded_for_brin():
    """Open-ended requests still get constant bounds (guardrail Sign 6)."""
    lo, hi = date_int_bounds(None, None)
    assert lo.tzinfo is timezone.utc and hi.tzinfo is timezone.utc
    assert lo < datetime(1950, 1, 1, tzinfo=timezone.utc)
    assert hi > datetime(2090, 1, 1, tzinfo=timezone.utc)


# --- Error taxonomy (spec §11) ----------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        V2CollectionUnavailable("FUT_VIX"),
        V2InstrumentUnavailable("IND_VIX"),
        V2UnsupportedCycle("M"),
        V2UnsupportedField("mid"),
        V2SymbolError("nope", "FUT_SP_500_EMINI_<YYYYMMDD>"),
    ],
)
def test_every_v2_error_is_a_v2_data_unavailable(exc):
    assert isinstance(exc, V2DataUnavailable)
    # Frozen cross-worker API: the base subclasses the project's error type.
    assert isinstance(exc, DataAccessError)


@pytest.mark.parametrize(
    "exc",
    [
        V2CollectionUnavailable("FUT_VIX"),
        V2InstrumentUnavailable("IND_VIX"),
        V2UnsupportedCycle("M"),
        V2UnsupportedField("mid"),
        V2SymbolError("nope", "grammar"),
    ],
)
def test_v2_errors_surface_as_http_400_not_502(exc):
    """A 'v2 cannot serve this' case is client-correctable, not a backend fault.

    STATUS_MAP is keyed on the error_type STRING, so this is what decides the
    status code — NOT the exception's base class.
    """
    assert STATUS_MAP[exc.error_type] == 400


@pytest.mark.parametrize(
    "exc,must_name",
    [
        (V2CollectionUnavailable("FUT_VIX"), "FUT_VIX"),
        (V2InstrumentUnavailable("IND_VIX"), "IND_VIX"),
        (V2UnsupportedCycle("M"), "'M'"),
        (V2UnsupportedField("mid"), "mid"),
    ],
)
def test_error_messages_name_the_value_and_the_fix(exc, must_name):
    """Spec §11: every message names the offending value AND the remedy."""
    assert must_name in exc.message
    assert 'data source "v1"' in exc.message
