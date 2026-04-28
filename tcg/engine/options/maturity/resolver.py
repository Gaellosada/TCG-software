"""Default implementation of MaturityResolver (Module 4).

Spec reference: §3.4 — pure date arithmetic, uses pandas_market_calendars for
holiday handling.

Rule semantics (all verified in tests/unit/engine/options/maturity/):

NextThirdFriday(offset_months)
    Third Friday of (ref_date.month + offset_months), at-or-after ref_date.
    If the computed 3rd Friday is a CME holiday → prior business day (per §3.4).
    "At-or-after" means: if ref_date is already past the 3rd Friday of the
    target month, advance one more month.

EndOfMonth(offset_months)
    Last business day of (ref_date.month + offset_months), via
    pandas_market_calendars valid_days(...).

PlusNDays(n)
    Simple calendar arithmetic: ref_date + timedelta(days=n).
    No business-day adjustment (per §3.4 — caller is responsible).

FixedDate(date)
    Returned as-is.

NearestToTarget(target_dte_days)
    Requires available_expirations (from caller, e.g. Module 3's chain).
    Picks the expiration whose DTE is closest to target_dte_days.
    Tie-break: lower DTE wins (per §3.4).
    resolve() raises ValueError; use resolve_with_chain().

Calendar note (ASSUMPTION logged in PROBLEMS.md):
    The spec says calendar="CME" but pandas_market_calendars 5.x does not
    register a bare "CME" alias.  "CME_TradeDate" is the general CME trade-
    date calendar that covers all CME products (equity, rate, bond, ag).  We
    map "CME" → "CME_TradeDate" internally and accept any registered name
    for the calendar parameter.
"""

from __future__ import annotations

import functools
from calendar import monthrange
from datetime import date, timedelta

import pandas_market_calendars as mcal

from tcg.types.options import (
    EndOfMonth,
    FixedDate,
    MaturityRule,
    NearestToTarget,
    NextThirdFriday,
    PlusNDays,
)

from .protocol import MaturityResolver  # noqa: F401 — re-export for convenience


# ---------------------------------------------------------------------------
# Calendar name normalisation
# ---------------------------------------------------------------------------

#: Map spec-level names to registered pandas_market_calendars names.
#: "CME" is the canonical name in the spec; "CME_TradeDate" is the
#: correct library key (v5+).  Other common aliases handled here.
_CALENDAR_ALIASES: dict[str, str] = {
    "CME": "CME_TradeDate",
}


def _canonical_calendar_name(name: str) -> str:
    return _CALENDAR_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# Calendar cache (one instance per canonical name)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=16)
def _get_calendar(canonical_name: str):  # type: ignore[return]
    """Return a cached pandas_market_calendars calendar instance."""
    return mcal.get_calendar(canonical_name)


def _calendar(name: str):  # type: ignore[return]
    return _get_calendar(_canonical_calendar_name(name))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _third_friday_of_month(year: int, month: int) -> date:
    """Return the third Friday of the given (year, month)."""
    d = date(year, month, 1)
    # weekday(): Mon=0 ... Fri=4
    days_to_first_friday = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=days_to_first_friday)
    return first_friday + timedelta(weeks=2)


def _add_months(d: date, months: int) -> date:
    """Add `months` to a date, clamping the day to month-end if needed."""
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def _is_business_day(d: date, cal) -> bool:
    """Return True if d is a valid business day on cal."""
    vd = cal.valid_days(start_date=d, end_date=d)
    return len(vd) > 0


def _prior_business_day(d: date, cal) -> date:
    """Return the last business day strictly before d (search up to 10 days back)."""
    candidate = d - timedelta(days=1)
    for _ in range(10):
        if _is_business_day(candidate, cal):
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError(f"Could not find prior business day within 10 days of {d}")


def _last_business_day_of_month(year: int, month: int, cal) -> date:
    """Return the last business day of the given (year, month)."""
    last_day = date(year, month, monthrange(year, month)[1])
    first_day = date(year, month, 1)
    vd = cal.valid_days(start_date=first_day, end_date=last_day)
    if len(vd) == 0:
        raise ValueError(f"No valid business days in {year}-{month:02d} for calendar")
    return vd[-1].date()


# ---------------------------------------------------------------------------
# DefaultMaturityResolver
# ---------------------------------------------------------------------------

class DefaultMaturityResolver:
    """Stateless implementation of MaturityResolver.

    Usage::

        r = DefaultMaturityResolver()
        result = r.resolve(date(2024, 1, 1), NextThirdFriday(offset_months=0))
        # → date(2024, 1, 19)
    """

    # -- Public API ----------------------------------------------------------

    def resolve(
        self,
        ref_date: date,
        rule: MaturityRule,
        calendar: str = "CME",
    ) -> date:
        """Resolve a maturity rule to a concrete expiration date.

        Dispatches to the appropriate private handler based on rule type.
        Raises ValueError for NearestToTarget — use resolve_with_chain.
        """
        if isinstance(rule, NextThirdFriday):
            return self._resolve_next_third_friday(ref_date, rule, calendar)
        if isinstance(rule, EndOfMonth):
            return self._resolve_end_of_month(ref_date, rule, calendar)
        if isinstance(rule, PlusNDays):
            return self._resolve_plus_n_days(ref_date, rule)
        if isinstance(rule, FixedDate):
            return self._resolve_fixed_date(rule)
        if isinstance(rule, NearestToTarget):
            raise ValueError(
                "NearestToTarget requires available_expirations; "
                "call resolve_with_chain instead"
            )
        raise TypeError(f"Unknown MaturityRule type: {type(rule)!r}")

    def resolve_with_chain(
        self,
        ref_date: date,
        rule: NearestToTarget,
        available_expirations: list[date],
    ) -> date | None:
        """Pick the expiration nearest to ref_date + target_dte_days.

        Returns None if available_expirations is empty.
        Tie-break: lower DTE wins (per spec §3.4).
        """
        if not available_expirations:
            return None

        target_date = ref_date + timedelta(days=rule.target_dte_days)

        def sort_key(exp: date) -> tuple[int, int]:
            dte = (exp - ref_date).days
            delta = abs(exp - target_date).days
            return (delta, dte)  # primary: smallest delta; secondary: lower dte

        return min(available_expirations, key=sort_key)

    # -- Private handlers ----------------------------------------------------

    def _resolve_next_third_friday(
        self,
        ref_date: date,
        rule: NextThirdFriday,
        calendar: str,
    ) -> date:
        """Third Friday of (ref_date.month + offset_months), at-or-after ref_date.

        "At-or-after": if offset_months=0 and ref_date is already past the
        3rd Friday of the current month, advance to the next month's 3rd Friday.
        If the result is a holiday, roll to the prior business day.
        """
        target_base = _add_months(ref_date, rule.offset_months)
        tf = _third_friday_of_month(target_base.year, target_base.month)

        # If the 3rd Friday is in the past (or today, still past after ref_date
        # check), advance one month.  This handles offset_months=0 when ref_date
        # is already past the 3rd Friday.
        if tf < ref_date:
            next_month = _add_months(target_base, 1)
            tf = _third_friday_of_month(next_month.year, next_month.month)

        cal = _calendar(calendar)
        if not _is_business_day(tf, cal):
            tf = _prior_business_day(tf, cal)
        return tf

    def _resolve_end_of_month(
        self,
        ref_date: date,
        rule: EndOfMonth,
        calendar: str,
    ) -> date:
        """Last business day of (ref_date.month + offset_months)."""
        target = _add_months(ref_date, rule.offset_months)
        cal = _calendar(calendar)
        return _last_business_day_of_month(target.year, target.month, cal)

    def _resolve_plus_n_days(self, ref_date: date, rule: PlusNDays) -> date:
        """Calendar arithmetic only — no business-day adjustment (per §3.4)."""
        return ref_date + timedelta(days=rule.n)

    def _resolve_fixed_date(self, rule: FixedDate) -> date:
        """Return the fixed date as-is."""
        return rule.date
