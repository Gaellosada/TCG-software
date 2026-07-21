"""Item D — hold-cadence predicate (duck-typed, fail-loud).

The stream resolver used ``isinstance(maturity, EndOfMonth)`` to gate the
per-month HOLD cadence.  That is now a duck-typed ``is_hold_cadence`` capability
on the maturity dataclass, consulted via ``_is_hold_cadence`` at BOTH gate sites.

These tests pin:
  * the capability itself (EndOfMonth holds; the other four rules do not);
  * the predicate helper reads the capability (default False when unset);
  * BYTE-IDENTITY: EndOfMonth still routes through the hold branch (a real
    resolve over the shared bulk fakes still holds one contract per month);
  * FAIL-LOUD: a maturity that SETS ``is_hold_cadence`` but whose roll-date math
    is not wired raises ``NotImplementedError`` (converting the old D1 F3
    silent-wrong-cadence footgun into a loud error), NOT a silent per-date
    resolve;
  * non-hold cadences (PlusNDays / FixedDate / NextThirdFriday) do NOT hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import (
    _is_hold_cadence,
    resolve_option_stream,
)
from tcg.types.options import (
    ByStrike,
    EndOfMonth,
    FixedDate,
    NearestToTarget,
    NextThirdFriday,
    PlusNDays,
    RollOffset,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row


# ── Capability + predicate ────────────────────────────────────────────────


def test_end_of_month_declares_hold_cadence():
    assert EndOfMonth().is_hold_cadence is True
    assert EndOfMonth(offset_months=1).is_hold_cadence is True


@pytest.mark.parametrize(
    "maturity",
    [
        NextThirdFriday(),
        PlusNDays(30),
        FixedDate(date(2024, 6, 21)),
        NearestToTarget(target_dte_days=30),
    ],
)
def test_non_hold_cadences_do_not_declare_the_flag(maturity):
    # No ``is_hold_cadence`` attribute at all → the getattr default governs.
    assert not hasattr(maturity, "is_hold_cadence")
    assert _is_hold_cadence(maturity) is False


def test_predicate_true_only_for_end_of_month():
    assert _is_hold_cadence(EndOfMonth()) is True
    assert _is_hold_cadence(NextThirdFriday()) is False
    assert _is_hold_cadence(PlusNDays(7)) is False


# ── Byte-identity: EndOfMonth still holds (routes through the hold branch) ──

_FEB = date(2024, 2, 29)
_MAR = date(2024, 3, 28)
_APR = date(2024, 4, 30)
_LISTED = [_FEB, _MAR, _APR]
_MID = {_FEB: 2.0, _MAR: 3.0, _APR: 4.0}
_DATES = [
    date(2024, 1, 31),
    date(2024, 2, 15),
    date(2024, 2, 29),
    date(2024, 3, 15),
    date(2024, 3, 28),
]


def _chains(dates):
    return {
        d: [
            (_contract(strike=4500, expiration=e), _row(row_date=d, mid=_MID[e]))
            for e in _LISTED
        ]
        for d in dates
    }


async def _resolve(maturity):
    chains = _chains(_DATES)
    return await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=maturity,
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=_LISTED,
    )


async def test_end_of_month_still_holds_per_month():
    """EndOfMonth resolved through the duck-typed gate still holds one contract
    per month (the roll fires ON month-end), unchanged from the isinstance gate."""
    _values, errors, contracts = await _resolve(EndOfMonth(offset_months=1))
    assert all(e is None or e.startswith("snapped_to:") for e in errors), errors
    held = {d: c.expiration for d, c in zip(_DATES, contracts)}
    # Jan-31 (init roll) → FEB, held through 02-15; Feb-29 rolls → MAR, held
    # through 03-15; Mar-28 rolls → APR.
    assert held[date(2024, 1, 31)] == _FEB
    assert held[date(2024, 2, 15)] == _FEB
    assert held[date(2024, 2, 29)] == _MAR
    assert held[date(2024, 3, 15)] == _MAR
    assert held[date(2024, 3, 28)] == _APR


# ── Fail-loud: hold-cadence flag set but roll math not wired ────────────────


@dataclass(frozen=True)
class _UnwiredHoldCadence:
    """A synthetic maturity that CLAIMS to be a hold cadence but has no roll
    math in the resolver (models a future cadence that set the flag before its
    per-period math was implemented)."""

    @property
    def is_hold_cadence(self) -> bool:
        return True


async def test_unwired_hold_cadence_raises_not_implemented():
    """A hold-cadence maturity with no roll-date math must FAIL LOUD, not
    silently fall through to a per-date stateless resolve (D1 F3)."""
    with pytest.raises(NotImplementedError, match="hold cadence"):
        await _resolve(_UnwiredHoldCadence())
