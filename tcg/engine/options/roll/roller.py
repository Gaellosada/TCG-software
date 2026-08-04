"""Module 5 implementation — DefaultOptionsRoller.

Spec reference: §3.5 (tcg.engine.options.roll).

Scope
-----
``AtExpiry`` and ``NDaysBeforeExpiry`` are fully implemented.  ``DeltaCross``
raises ``NotImplementedError("phase_2_only: ...")`` — reserved for Phase 2
without prejudice to the Protocol shape.

``NDaysBeforeExpiry`` — trading days, not calendar days
--------------------------------------------------------
Per the strategy-repro SPEC §5.5/§5.6 (screenshots are authoritative over
legacy code/docstrings — SPEC §0): roll rules are stated in TRADING days
("roll the 30-day VIX call 2 trading days before expiry"). The dataclass
previously documented "n calendar days"; that was wrong and has been fixed
(``tcg/types/options.py``) — this implementation always counts TRADING days.

Trading-calendar source: neither ``should_roll`` nor ``next_contract``
receive a calendar or date-sequence argument (see ``protocol.py`` — that is
a Protocol-shape constraint out of this module's file scope), so there is no
injected trading-date source to consult. Module 5 instead reuses the SAME
``CME_TradeDate`` market calendar that Module 4
(``tcg.engine.options.maturity.resolver``) already uses to holiday-adjust
every option expiration in this codebase (SPX and VIX legs alike) — i.e. the
calendar this codebase already treats as canonical for options, not a naive
weekday count. The alias/cache plumbing below is an INTENTIONAL duplication
of that resolver's pattern (mirrors the existing precedent in
``tcg/data/_rolling/calendar.py``, which duplicates the same helper for the
same reason: keeping Module 5's only real external dependency the
``OptionsSelector`` Protocol, and avoiding an import-linter-hostile coupling
to a sibling Module 4). See ``_n_trading_days_before`` below.

Dependency pattern
------------------
The constructor accepts an ``OptionsSelector`` (Protocol from Module 3).
Module 3 already abstracts the ``tcg.data`` boundary through its own ports,
so Module 5 does NOT need its own ``_ports.py``.  The coupling is to the
*shape* of Module 3, not to any concrete implementation — do NOT import
``DefaultOptionsSelector``.

Independence contract verified by::

    grep -rE "^from tcg\\.data" tcg/engine/options/roll/    # must be empty
    grep -r "from tcg.engine.options.selection.selector"    # must be empty
"""

from __future__ import annotations

import functools
from datetime import date, timedelta

import pandas_market_calendars as mcal

from tcg.engine.options.selection.protocol import OptionsSelector
from tcg.types.options import (
    AtExpiry,
    DeltaCross,
    MaturityRule,
    NDaysBeforeExpiry,
    OptionContractDoc,
    OptionDailyRow,
    RollResult,
    RollRule,
    SelectionCriterion,
)

# ---------------------------------------------------------------------------
# Trading-calendar helper for NDaysBeforeExpiry
# ---------------------------------------------------------------------------

#: Spec-level calendar name -> registered pandas_market_calendars name.
#: Mirrors tcg.engine.options.maturity.resolver's alias exactly (same reason:
#: pandas_market_calendars 5.x has no bare "CME" alias).
_CALENDAR_ALIASES: dict[str, str] = {"CME": "CME_TradeDate"}

#: Hard cap (calendar days) on the lookback window used to resolve "N trading
#: days before expiry". Generous for any realistic N (the SPEC only ever uses
#: N=2) while bounding the query for a pathologically large N — see
#: ``_n_trading_days_before``'s clamp behaviour below.
_MAX_LOOKBACK_CALENDAR_DAYS = 1000


@functools.lru_cache(maxsize=16)
def _get_calendar(canonical_name: str):  # type: ignore[return]
    """Return a cached pandas_market_calendars calendar instance."""
    return mcal.get_calendar(canonical_name)


def _trading_calendar(name: str = "CME"):  # type: ignore[return]
    return _get_calendar(_CALENDAR_ALIASES.get(name, name))


def _n_trading_days_before(expiration: date, n: int, calendar: str = "CME") -> date:
    """Return the date that is *n* TRADING days before *expiration*.

    Anchor = the last trading day on/before ``expiration`` (== ``expiration``
    itself whenever expiration is a trading day, which every real option
    expiration is). ``n=0`` returns the anchor unchanged, so
    ``NDaysBeforeExpiry(n=0)`` is behaviourally identical to ``AtExpiry``'s
    boundary (roll on/after that date) — by design, per the F3 brief.

    Walks back *n* entries in the calendar's ``valid_days`` sequence (NOT
    calendar-day subtraction), so a boundary that crosses a weekend or a
    market holiday (e.g. Good Friday) is handled correctly.

    If *n* exceeds the number of trading days available within the bounded
    lookback window (``_MAX_LOOKBACK_CALENDAR_DAYS``) — only reachable with a
    pathologically large ``n``, since the SPEC only ever uses N=2 — the
    result clamps to the earliest trading day found in that window. This is
    documented "roll (effectively) immediately" behaviour: any ``as_of`` from
    that clamped date onward is due, rather than raising or scanning an
    unbounded calendar.
    """
    if n < 0:
        raise ValueError(f"NDaysBeforeExpiry.n must be >= 0, got {n}")

    buffer_days = min(max(n * 2, 30), _MAX_LOOKBACK_CALENDAR_DAYS)
    cal = _trading_calendar(calendar)
    valid_days = cal.valid_days(
        start_date=expiration - timedelta(days=buffer_days),
        end_date=expiration,
    )
    if len(valid_days) == 0:
        raise ValueError(
            f"No valid trading days found up to {expiration} on calendar {calendar!r}"
        )

    valid_dates = [ts.date() for ts in valid_days]
    anchor_idx = len(valid_dates) - 1  # last trading day <= expiration
    target_idx = max(anchor_idx - n, 0)
    return valid_dates[target_idx]


class DefaultOptionsRoller:
    """Concrete implementation of the ``OptionsRoller`` Protocol.

    Parameters
    ----------
    selector:
        An object satisfying ``OptionsSelector`` (Module 3 Protocol).
        Injected at construction; never imported as a concrete class here.
    """

    def __init__(self, selector: OptionsSelector) -> None:
        self._selector = selector

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_roll(
        self,
        held: OptionContractDoc,
        held_row: OptionDailyRow,
        as_of: date,
        rule: RollRule,
    ) -> bool:
        """Return True when the roll condition is satisfied.

        AtExpiry semantics: roll on or after ``held.expiration``.
        NDaysBeforeExpiry semantics: roll on or after the date that is
        ``rule.n`` TRADING days before ``held.expiration`` (see
        ``_n_trading_days_before``).
        """
        if isinstance(rule, AtExpiry):
            return as_of >= held.expiration
        if isinstance(rule, NDaysBeforeExpiry):
            trigger_date = _n_trading_days_before(held.expiration, rule.n)
            return as_of >= trigger_date
        if isinstance(rule, DeltaCross):
            raise NotImplementedError(
                "phase_2_only: DeltaCross roll rule reserved for Phase 2"
            )
        raise ValueError(f"Unknown roll rule type: {type(rule).__name__}")

    async def next_contract(
        self,
        held: OptionContractDoc,
        as_of: date,
        rule: RollRule,
        criterion_for_new: SelectionCriterion,
        maturity_for_new: MaturityRule,
    ) -> RollResult:
        """Check roll condition and, if due, select the replacement contract.

        Root extraction: ``held.collection`` is used as the root passed to
        Module 3 (e.g. ``"OPT_SP_500"``).  This matches what Module 1's
        ``query_chain`` expects as the *root* parameter.

        Dispatch on rule type:

        - ``AtExpiry``: check ``as_of >= held.expiration``; if not due return
          ``not_yet_due``; if due call ``selector.select`` and wrap result.
        - ``NDaysBeforeExpiry``: check ``as_of >= _n_trading_days_before(...)``;
          same not-due/selector/wrap flow as ``AtExpiry`` — only the roll
          TIMING differs, so the replacement is selected via the exact same
          ``selector.select(root=held.collection, date=as_of, type=held.type,
          criterion=criterion_for_new, maturity=maturity_for_new)`` call
          AtExpiry makes, i.e. the same nearest-next-expiration contract.
        - ``DeltaCross``: raise ``NotImplementedError`` (Phase 2 only).
        """
        if isinstance(rule, DeltaCross):
            raise NotImplementedError("phase_2_only")

        if isinstance(rule, AtExpiry):
            due = as_of >= held.expiration
            return await self._roll_if_due(
                due=due,
                held=held,
                as_of=as_of,
                criterion_for_new=criterion_for_new,
                maturity_for_new=maturity_for_new,
                reason_when_rolled="rolled_at_expiry",
            )

        if isinstance(rule, NDaysBeforeExpiry):
            trigger_date = _n_trading_days_before(held.expiration, rule.n)
            due = as_of >= trigger_date
            return await self._roll_if_due(
                due=due,
                held=held,
                as_of=as_of,
                criterion_for_new=criterion_for_new,
                maturity_for_new=maturity_for_new,
                reason_when_rolled="rolled_n_days_before_expiry",
            )

        raise ValueError(f"Unknown roll rule type: {type(rule).__name__}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _roll_if_due(
        self,
        *,
        due: bool,
        held: OptionContractDoc,
        as_of: date,
        criterion_for_new: SelectionCriterion,
        maturity_for_new: MaturityRule,
        reason_when_rolled: str,
    ) -> RollResult:
        """Shared AtExpiry / NDaysBeforeExpiry handler — extracted for
        readability and to guarantee both rules select the replacement
        identically (same selector.select call) once due."""
        if not due:
            return RollResult(
                new_contract=None,
                roll_date=None,
                reason="not_yet_due",
                error_code="not_yet_due",
            )

        # Roll is due — invoke Module 3 to select the replacement.
        result = await self._selector.select(
            root=held.collection,
            date=as_of,
            type=held.type,
            criterion=criterion_for_new,
            maturity=maturity_for_new,
        )

        if result.contract is not None:
            return RollResult(
                new_contract=result.contract,
                roll_date=as_of,
                reason=reason_when_rolled,
                error_code=None,
            )

        # Selection failed — propagate error_code from Module 3.
        return RollResult(
            new_contract=None,
            roll_date=None,
            reason=f"roll_selection_failed: {result.error_code}",
            error_code=result.error_code,
        )
