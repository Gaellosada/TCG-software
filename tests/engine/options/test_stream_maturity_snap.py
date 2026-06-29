"""Tests for arithmetic-maturity expiration SNAPPING in the bulk stream resolver.

Issue #2 finding (C): non-NearestToTarget maturity rules (EndOfMonth, PlusNDays,
FixedDate, NextThirdFriday) resolve the target expiration by pure date arithmetic
with NO chain-existence check.  When the computed expiration is not one of the
root's LISTED expirations (daily-expiry roots, sparse listings), every date
queried an expiration that has no contracts -> ``no_chain_for_date`` on every
date -> silent all-NaN -> opaque "all option stream values are NaN" 400.

Fix (decision D2, locked): when ``available_expirations`` is supplied and the
arithmetic target is not among them, snap UNCONDITIONALLY to the nearest listed
expiration (same behaviour as NearestToTarget, which has no distance cap) and
record a per-date ``snapped_to:<iso>`` diagnostic so the substitution is
traceable.

Harness: the shared ``_stream_fakes`` bulk reader filters each date's chain by
``expiration_min <= c.expiration <= expiration_max``; an expiration with no
contracts therefore returns an empty chain — exactly the production failure.
"""

from __future__ import annotations

from datetime import date

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import (
    _snap_to_listed,
    resolve_option_stream,
)
from tcg.types.options import ByStrike, EndOfMonth, FixedDate, PlusNDays

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row

# The single LISTED expiration for this root (3rd Friday of Jan-2024). NOT a
# month-end business day, so EndOfMonth's arithmetic target (2024-01-31) misses
# it; NOT 14 days out either, so PlusNDays(14) misses it too.
_LISTED_EXP = date(2024, 1, 19)
_K = _contract(strike=4500, expiration=_LISTED_EXP)


def _chains(dates):
    return {d: [(_K, _row(row_date=d, mid=12.5))] for d in dates}


async def _resolve(dates, *, maturity, available_expirations):
    chains = _chains(dates)
    return await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=maturity,
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=available_expirations,
    )


def test_arithmetic_target_computation():
    """Pin the premise the snap tests rely on: EndOfMonth(0) for 2024-01-10
    computes 2024-01-31 (the last business day), which is NOT the root's listed
    expiration — so a snap is required for it to resolve. (The fail-without-snap
    behaviour is covered by test_no_available_expirations_falls_back_to_arithmetic.)"""
    arith = DefaultMaturityResolver().resolve(
        date(2024, 1, 10), EndOfMonth(offset_months=0)
    )
    assert arith == date(2024, 1, 31)
    assert arith != _LISTED_EXP


async def test_end_of_month_snaps_to_nearest_listed_expiration():
    """EndOfMonth target 2024-01-31 is not listed; with the listed expiration
    2024-01-19 supplied, the resolver snaps to it -> real mid, not all-NaN."""
    dates = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)]
    values, errors, contracts = await _resolve(
        dates, maturity=EndOfMonth(offset_months=0), available_expirations=[_LISTED_EXP]
    )
    # Snapped -> the listed contract is selected on every date.
    assert all(c is not None and c.expiration == _LISTED_EXP for c in contracts)
    # Real values (not NaN).
    assert list(values) == [12.5, 12.5, 12.5]
    # And a per-date snap diagnostic records the substitution.
    assert all(e == f"snapped_to:{_LISTED_EXP.isoformat()}" for e in errors)


async def test_plus_n_days_also_snaps():
    """PlusNDays is a pure-arithmetic rule too; its target (date+14) is not
    listed, so it snaps to the nearest listed expiration as well."""
    dates = [date(2024, 1, 10)]
    # PlusNDays(14) from 2024-01-10 = 2024-01-24, not listed.
    values, errors, contracts = await _resolve(
        dates, maturity=PlusNDays(n=14), available_expirations=[_LISTED_EXP]
    )
    assert contracts[0] is not None and contracts[0].expiration == _LISTED_EXP
    assert values[0] == 12.5
    assert errors[0] == f"snapped_to:{_LISTED_EXP.isoformat()}"


async def test_no_snap_when_target_is_listed():
    """Control: when the arithmetic target IS a listed expiration, no snap and
    no snap diagnostic — the date resolves cleanly with error_code None."""
    dates = [date(2024, 1, 10)]
    # Make the listed expiration BE the EndOfMonth target so no snap is needed.
    eom = date(2024, 1, 31)
    k = _contract(strike=4500, expiration=eom)
    chains = {d: [(k, _row(row_date=d, mid=9.9))] for d in dates}
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=EndOfMonth(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=[eom],
    )
    assert contracts[0] is not None and contracts[0].expiration == eom
    assert values[0] == 9.9
    assert errors[0] is None  # no snap -> no diagnostic


async def test_no_available_expirations_falls_back_to_arithmetic():
    """Backward-compat: when available_expirations is None (legacy callers /
    NearestToTarget probe disabled), the else-branch keeps its pure-arithmetic
    behaviour — no snap, and the not-listed target yields no_chain_for_date."""
    dates = [date(2024, 1, 10)]
    values, errors, contracts = await _resolve(
        dates, maturity=EndOfMonth(offset_months=0), available_expirations=None
    )
    # Arithmetic target 2024-01-31 not listed -> empty chain -> failure.
    assert contracts[0] is None
    assert errors[0] == "no_chain_for_date"
    assert values[0] != values[0]  # NaN


# ── N1: equidistant tie-break + order-independence ──────────────────────


def test_snap_tie_break_picks_earlier_expiration():
    """When two listed expirations are EXACTLY equidistant from the arithmetic
    target, the earlier (lower-DTE) one wins — parity with NearestToTarget's
    (delta, dte) tie-break."""
    early = date(2024, 1, 10)
    late = date(2024, 1, 30)
    target = date(2024, 1, 20)  # 10 days from each — a genuine tie.
    assert (target - early).days == (late - target).days == 10
    assert _snap_to_listed(target, [early, late]) == early


def test_snap_is_order_independent():
    """The snap result does not depend on the order of the listed expirations
    (the helper sorts internally via the (distance, date) key)."""
    early = date(2024, 1, 10)
    late = date(2024, 1, 30)
    target = date(2024, 1, 20)
    assert _snap_to_listed(target, [early, late]) == _snap_to_listed(
        target, [late, early]
    )


async def test_end_to_end_tie_break_selects_earlier_listed_contract():
    """Through the full bulk resolver: a FixedDate target equidistant between two
    listed expirations snaps to the EARLIER one, and its contract is selected."""
    early = date(2024, 2, 16)
    late = date(2024, 3, 15)
    # FixedDate exactly midway (14 days each side of 2024-02-16 / 2024-03-15
    # would be 2024-03-01; verify the tie precisely).
    target = date(2024, 3, 1)
    assert (target - early).days == (late - target).days == 14
    k_early = _contract(strike=4500, expiration=early)
    k_late = _contract(strike=4500, expiration=late)
    dates = [date(2024, 1, 15)]
    chains = {
        d: [
            (k_early, _row(row_date=d, mid=7.0)),
            (k_late, _row(row_date=d, mid=8.0)),
        ]
        for d in dates
    }
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=FixedDate(date=target),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=[late, early],  # deliberately unsorted
    )
    assert contracts[0] is not None and contracts[0].expiration == early
    assert values[0] == 7.0  # the earlier contract's mid
    assert errors[0] == f"snapped_to:{early.isoformat()}"


# ── N2: resolver exception -> dedicated maturity_resolution_failed code ──


class _RaisingMaturityResolver:
    """Maturity resolver whose resolve() always raises a non-TCGError, standing
    in for a pathological rule (e.g. a calendar month with zero business days)."""

    def resolve(self, *, ref_date, rule, calendar="CME"):
        raise ValueError("synthetic resolver failure")

    def resolve_with_chain(
        self, *, ref_date, rule, available_expirations
    ):  # pragma: no cover
        raise ValueError("synthetic resolver failure")


async def test_resolver_exception_yields_maturity_resolution_failed():
    """Hardening (finding D): a non-TCGError from the maturity resolver is
    caught per-date and surfaced as ``maturity_resolution_failed`` (a dedicated
    code, distinct from ``no_chain_for_date``) — never a 500."""
    dates = [date(2024, 1, 10)]
    chains = _chains(dates)
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=EndOfMonth(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=_RaisingMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=[_LISTED_EXP],
    )
    assert contracts[0] is None
    assert errors[0] == "maturity_resolution_failed"
    assert values[0] != values[0]  # NaN
