"""Tests for the END-OF-MONTH roll SCHEDULE on option streams (Issue #3).

``RollSchedule = EndOfMonthRoll | None`` is a NEW, orthogonal dimension to the
``maturity`` rule.  With ``roll_schedule=EndOfMonthRoll()`` the resolver:

  * resolves the maturity rule ONCE on the last TRADING day of each month
    (plus unconditionally on the first queryable date), and
  * HOLDS that resolved expiration for every date until the next month-end
    roll — instead of re-resolving the maturity per trade date.

The held expiration is selected exactly as before (maturity + ``roll_offset`` +
the Issue-#2 snap-to-listed), Phases B/C are unchanged, and ``derive_rolls``
fires only on the resulting monthly expiration transitions.

Harness reuses the shared bulk fakes (``_stream_fakes``); the same harness the
roll-offset and maturity-snap suites use.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tcg.core.api._options_materialise import derive_rolls
from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByStrike,
    EndOfMonth,
    EndOfMonthRoll,
    FixedDate,
    NextThirdFriday,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row

# Listed monthly expirations = each month's last business day (Issue #3 snaps
# the EndOfMonth(offset_months=1) arithmetic target to these).  Distinct mids
# per expiration so the series value reveals which contract is held.
_FEB = date(2024, 2, 29)
_MAR = date(2024, 3, 28)  # 29th is Good Friday → 28th (trading-calendar snap)
_APR = date(2024, 4, 30)
_MAY = date(2024, 5, 31)
_LISTED = [_FEB, _MAR, _APR, _MAY]
_MID = {_FEB: 2.0, _MAR: 3.0, _APR: 4.0, _MAY: 5.0}


def _chains(dates):
    """Every listed expiration is quoted on every date (strike 4500)."""
    return {
        d: [
            (_contract(strike=4500, expiration=e), _row(row_date=d, mid=_MID[e]))
            for e in _LISTED
        ]
        for d in dates
    }


# Business days spanning late-Jan → early-Apr 2024 (a few per month, INCLUDING
# each month-end last trading day so the roll dates are present in the axis).
_DATES = [
    date(2024, 1, 16),
    date(2024, 1, 31),  # Jan last trading day (roll)
    date(2024, 2, 15),
    date(2024, 2, 29),  # Feb last trading day (roll)
    date(2024, 3, 15),
    date(2024, 3, 28),  # Mar last trading day (roll)
    date(2024, 4, 1),
]


async def _resolve(dates, *, maturity, roll_schedule, roll_offset=0, available=None):
    chains = _chains(dates)
    return await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=maturity,
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=roll_offset,
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=available if available is not None else _LISTED,
        roll_schedule=roll_schedule,
    )


# ── Core behaviour: hold one contract per month, roll at month-end ─────────


async def test_eom_holds_one_contract_per_month_and_rolls_monthly():
    """EndOfMonth(offset_months=1) under EOM-roll re-resolves ONLY on each
    month's last trading day and holds in between.

    The roll fires ON the month-end date (``d >= cur_eom``), so the new
    contract is established on that day and held until the next month-end:
      * 01-16 (init roll) … 02-15 hold FEB,
      * 02-29 (Feb roll) … 03-15 hold MAR,
      * 03-28 (Mar roll) … 04-01 hold APR.
    Exactly one contract per holding-window, rolling at month-end — NOT
    re-selected per trade date.
    """
    maturity = EndOfMonth(offset_months=1)
    values, errors, contracts = await _resolve(
        _DATES, maturity=maturity, roll_schedule=EndOfMonthRoll()
    )

    # No failures (snap notes are success-side).
    assert all(e is None or e.startswith("snapped_to:") for e in errors), errors

    held = {d: c.expiration for d, c in zip(_DATES, contracts)}
    # Init roll (01-16) resolves FEB and holds it through 01-31 AND the pre-roll
    # part of February (02-15) — the Feb roll has NOT fired yet on 02-15.
    assert held[date(2024, 1, 16)] == _FEB
    assert held[date(2024, 1, 31)] == _FEB
    assert held[date(2024, 2, 15)] == _FEB
    # The Feb month-end (02-29) is the roll date → MAR, held through 03-15.
    assert held[date(2024, 2, 29)] == _MAR
    assert held[date(2024, 3, 15)] == _MAR
    # The Mar month-end (03-28) rolls → APR, held into April (04-01).
    assert held[date(2024, 3, 28)] == _APR
    assert held[date(2024, 4, 1)] == _APR

    # Series value follows the held contract's mid.
    by_date = dict(zip(_DATES, values))
    assert by_date[date(2024, 1, 16)] == _MID[_FEB]
    assert by_date[date(2024, 2, 15)] == _MID[_FEB]
    assert by_date[date(2024, 2, 29)] == _MID[_MAR]
    assert by_date[date(2024, 3, 28)] == _MID[_APR]

    # Exactly THREE distinct contracts over the span (FEB, MAR, APR) — proving a
    # monthly cadence, not daily churn.
    assert {c.expiration for c in contracts} == {_FEB, _MAR, _APR}


async def test_eom_holds_constant_within_month_not_per_date():
    """Within a single holding-month the contract does NOT change date-to-date —
    the whole point of EOM-roll (vs the stateless per-date resolve)."""
    # Two February dates that bracket mid-month; both must hold the SAME (MAR)
    # contract even though a per-date resolve of EndOfMonth(1) on different Feb
    # days would still give MAR here — so use a maturity that WOULD drift daily
    # without holding: NextThirdFriday(0) resolves to a different 3rd-Friday as
    # the ref date advances across its own 3rd Friday.  Under holding, the
    # contract is pinned to whatever the month-end roll resolved.
    dates = [date(2024, 2, 1), date(2024, 2, 15), date(2024, 2, 29)]
    # List the relevant 3rd Fridays as available so the snap lands on a real one.
    third_fris = [date(2024, 2, 16), date(2024, 3, 15), date(2024, 4, 19)]
    chains = {
        d: [
            (_contract(strike=4500, expiration=e), _row(row_date=d, mid=float(e.month)))
            for e in third_fris
        ]
        for d in dates
    }
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=third_fris,
        roll_schedule=EndOfMonthRoll(),
    )
    assert all(c is not None for c in contracts)
    # The first date (2024-02-01) is the init roll; it resolves NextThirdFriday(0)
    # as of 2024-02-01 → 2024-02-16.  All three Feb dates HOLD that same expiry —
    # no per-date drift to the March 3rd Friday even though 2024-02-29 alone
    # would resolve forward.
    assert {c.expiration for c in contracts} == {date(2024, 2, 16)}


async def test_eom_roll_markers_fire_monthly():
    """derive_rolls over the held-contract array emits exactly the monthly
    expiration transitions (FEB→MAR at the Feb roll, MAR→APR at the Mar roll)."""
    maturity = EndOfMonth(offset_months=1)
    values, errors, contracts = await _resolve(
        _DATES, maturity=maturity, roll_schedule=EndOfMonthRoll()
    )
    iso = [d.isoformat() for d in _DATES]
    vals = [None if np.isnan(v) else float(v) for v in values]
    rolls = derive_rolls(iso, vals, contracts)
    # The held expiration changes ON each month-end roll date: FEB→MAR on
    # 2024-02-29 (the Feb roll), MAR→APR on 2024-03-28 (the Mar roll).
    roll_dates = [r["date"] for r in rolls]
    assert roll_dates == ["2024-02-29", "2024-03-28"]
    feb_to_mar = rolls[0]
    assert feb_to_mar["sold"]["expiration"] == _FEB.isoformat()
    assert feb_to_mar["bought"]["expiration"] == _MAR.isoformat()
    mar_to_apr = rolls[1]
    assert mar_to_apr["sold"]["expiration"] == _MAR.isoformat()
    assert mar_to_apr["bought"]["expiration"] == _APR.isoformat()


# ── #2 snap preserved UNDER monthly holding (load-bearing) ─────────────────


async def test_issue2_snap_preserved_under_monthly_holding():
    """Issue #2's expiration-snap is now LOAD-BEARING under EOM-roll: a contract
    is held ~21 days, not re-selected daily.  The snap must still fire on the
    roll date and the SNAPPED expiration must be the held one all month.

    EndOfMonth(offset_months=0)'s arithmetic target is the calendar month-end
    (e.g. 2024-01-31); the only LISTED expiration is the 3rd Friday 2024-01-19,
    so the resolver snaps to it and HOLDS it across all of January.
    """
    listed = date(2024, 1, 19)  # the single listed expiration (a 3rd Friday)
    k = _contract(strike=4500, expiration=listed)
    # January dates including the 31st (the roll date).
    dates = [date(2024, 1, 8), date(2024, 1, 16), date(2024, 1, 31)]
    chains = {d: [(k, _row(row_date=d, mid=12.5))] for d in dates}
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
        available_expirations=[listed],
        roll_schedule=EndOfMonthRoll(),
    )
    # The snapped listed contract is held on EVERY January date (not just the
    # init date) — the snap survives the hold.
    assert all(c is not None and c.expiration == listed for c in contracts)
    assert list(values) == [12.5, 12.5, 12.5]
    # The snap diagnostic still records the substitution on the held dates.
    assert all(e == f"snapped_to:{listed.isoformat()}" for e in errors)


async def test_issue2_snap_note_travels_to_held_dates():
    """The ``snapped_to:`` annotation is a property of the held contract, so it
    must appear on the held (non-roll) dates too, not only the roll date."""
    listed = date(2024, 1, 19)
    k = _contract(strike=4500, expiration=listed)
    dates = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)]  # no month-end
    chains = {d: [(k, _row(row_date=d, mid=7.0))] for d in dates}
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
        available_expirations=[listed],
        roll_schedule=EndOfMonthRoll(),
    )
    # All three dates (init roll + 2 held) carry the snap note + the held value.
    assert all(e == f"snapped_to:{listed.isoformat()}" for e in errors)
    assert list(values) == [7.0, 7.0, 7.0]


# ── Mid-month-expiry edge → WARN, no crash (gap, not exception) ────────────


async def test_mid_month_expiry_gap_does_not_crash():
    """If the held contract expires mid-month (e.g. a 3rd-Friday maturity under
    EOM-roll), the tail of the month has no chain data → NaN gap with a per-date
    diagnostic, NOT an exception (Gael's locked decision: WARN, don't block)."""
    # Hold the JAN 3rd-Friday (2024-01-19) under EOM-roll: dates after the 19th
    # have NO chain for that contract → gap.
    held_exp = date(2024, 1, 19)
    k = _contract(strike=4500, expiration=held_exp)
    dates = [date(2024, 1, 16), date(2024, 1, 19), date(2024, 1, 25), date(2024, 1, 31)]
    # Chain only quotes the contract on/before its expiry; after the 19th the
    # bulk reader returns nothing for it (mid-month death).
    chains = {
        d: ([(k, _row(row_date=d, mid=1.5))] if d <= held_exp else []) for d in dates
    }
    # No crash:
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=FixedDate(date=held_exp),  # pins the held expiry mid-month
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=[held_exp],
        roll_schedule=EndOfMonthRoll(),
    )
    # On/before expiry: real value.  After expiry: NaN + no_chain_for_date.
    assert values[0] == 1.5 and values[1] == 1.5
    assert np.isnan(values[2]) and np.isnan(values[3])
    assert errors[2] == "no_chain_for_date" and errors[3] == "no_chain_for_date"


# ── roll_schedule=None preserves the existing per-date behaviour ───────────


async def test_roll_schedule_none_is_per_date_baseline():
    """roll_schedule=None (default) keeps the stateless per-date resolve: the
    output must be identical to omitting roll_schedule entirely."""
    maturity = EndOfMonth(offset_months=1)
    v_none, e_none, c_none = await _resolve(
        _DATES, maturity=maturity, roll_schedule=None
    )
    # Default path (roll_schedule not passed).
    chains = _chains(_DATES)
    v_def, e_def, c_def = await resolve_option_stream(
        dates=_DATES,
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
        available_expirations=_LISTED,
    )
    np.testing.assert_array_equal(v_none, v_def)
    assert [c.expiration for c in c_none] == [c.expiration for c in c_def]


# ── Reject roll_schedule on the legacy non-bulk path ───────────────────────


async def test_roll_schedule_without_bulk_reader_raises():
    """The legacy per-date path cannot honour a roll schedule (no pre-resolved
    expiration sweep) → requesting one raises, mirroring the roll_offset guard."""
    dates = [date(2024, 1, 16), date(2024, 1, 31)]
    chains = _chains(dates)
    with pytest.raises(
        ValueError, match="roll_schedule requires the bulk chain reader"
    ):
        await resolve_option_stream(
            dates=dates,
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=EndOfMonth(offset_months=1),
            selection=ByStrike(strike=4500.0),
            stream="mid",
            chain_reader=FakeChainReader(chains),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=None,
            roll_schedule=EndOfMonthRoll(),
        )


# ── roll_offset composes with EOM-roll ─────────────────────────────────────


async def test_roll_offset_shifts_held_resolution():
    """roll_offset shifts the maturity resolution forward on each roll date, so
    the held contract can differ from the no-offset hold.

    On the Jan roll date (2024-01-31), EndOfMonth(offset_months=1) resolves to
    FEB-end.  With roll_offset=2, the resolution is as of 2024-02-02 → MARCH →
    MAR-end, so January would hold MAR instead of FEB once the offset crosses
    into February.  (We assert the held contract reflects the offset.)
    """
    # Single January date that is the month-end roll (init roll fires here too).
    dates = [date(2024, 1, 31)]
    chains = _chains(dates)
    _v0, _e0, c0 = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=EndOfMonth(offset_months=1),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=0,
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=_LISTED,
        roll_schedule=EndOfMonthRoll(),
    )
    _v2, _e2, c2 = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=EndOfMonth(offset_months=1),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=2,  # 2024-01-31 + 2 = 2024-02-02 → EndOfMonth(1) → MAR-end
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=_LISTED,
        roll_schedule=EndOfMonthRoll(),
    )
    assert c0[0].expiration == _FEB
    assert c2[0].expiration == _MAR  # offset pushed the resolution into Feb


# ── Init-guard semantics: retry within the month after a failed first resolve ──


class _FailFirstResolver(DefaultMaturityResolver):
    """Resolver that RAISES on its first resolve() call, then behaves normally.

    Stands in for a maturity rule that is transiently unresolvable on the first
    queryable date (e.g. a calendar edge) but resolves on a later date in the
    SAME month.  The sweep's init guard keys on ``held_exp is None`` so it keeps
    retrying within the month rather than blanking it (vs ``held_roll_month is
    None`` which would only retry at the next month-end)."""

    def __init__(self):
        self._calls = 0

    def resolve(self, *, ref_date, rule, calendar="CME"):
        self._calls += 1
        if self._calls == 1:
            raise ValueError("synthetic first-resolve failure")
        return super().resolve(ref_date=ref_date, rule=rule, calendar=calendar)


async def test_failed_first_resolve_retries_within_month():
    """If the first roll-date resolve fails, the sweep re-tries on the next date
    in the same month (init guard = ``held_exp is None``) and recovers — it does
    NOT blank the whole month until the next month-end."""
    # Two January dates, neither a month-end. First fails, second succeeds.
    dates = [date(2024, 1, 8), date(2024, 1, 9)]
    chains = _chains(dates)
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=EndOfMonth(offset_months=1),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=_FailFirstResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=_LISTED,
        roll_schedule=EndOfMonthRoll(),
    )
    # Date 0 failed to resolve → NaN + maturity_resolution_failed.
    assert contracts[0] is None
    assert errors[0] == "maturity_resolution_failed"
    assert np.isnan(values[0])
    # Date 1 (still January, not a month-end) RE-TRIED because held_exp was still
    # None → resolved FEB and produced a real value. (With a held_roll_month-keyed
    # guard this date would have been skipped and stayed NaN.)
    assert contracts[1] is not None and contracts[1].expiration == _FEB
    assert values[1] == _MID[_FEB]
