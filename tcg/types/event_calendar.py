"""Curated US macro event-day calendar (FOMC / NFP / CPI) — STATIC config.

A version-controlled, hand-curated list of US macro event dates used by the
intraday backtest's date-allowlist entry mode (F3.2) and the event-day
attribution view (A3). This is a STATIC curated list (Gael's decision) — NOT an
automated event-data pipeline and NOT a general calendar framework. To correct
or extend it, edit the per-type ISO-date tuples below (and add a date to
``_TENTATIVE`` if it is scheduled-but-unconfirmed).

This module lives in ``tcg.types`` (no dependencies) so both ``tcg.core`` (the
allowlist resolver + the read endpoint) can consume it without crossing an
import-linter boundary — mirroring ``tcg.types.multipliers``. ``tcg.engine`` does
NOT import it (the allowlist is a pure date filter applied in core).

Event-day semantics — what date each entry marks (the day the market reacts):

* ``FOMC`` — the FOMC rate-decision / statement day (the SECOND day of each
  two-day meeting). Source: federalreserve.gov FOMC calendar
  (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm).
  The 2025-08-22 notation vote (Statement on Longer-Run Goals) is NOT a
  rate-decision meeting and is deliberately excluded.
* ``NFP`` — the BLS Employment Situation ("nonfarm payrolls") news-release day.
  Source: BLS archived-release URLs (each URL encodes the actual release date,
  e.g. ``.../archives/empsit_02072025.htm`` == 2025-02-07). Schedule:
  https://www.bls.gov/schedule/news_release/empsit.htm.
* ``CPI`` — the BLS Consumer Price Index news-release day. Source: BLS
  archived-release URLs (e.g. ``.../archives/cpi_01152025.htm`` == 2025-01-15).
  Schedule: https://www.bls.gov/schedule/news_release/cpi.htm.

ACCURACY / verification (curated 2026-08-19):
Every date below is confirmed against a PRIMARY source — the federalreserve.gov
FOMC calendar, or a BLS archived-release URL whose filename encodes the ACTUAL
published release date (the day markets moved), not merely a planned schedule.
The 2025 US government shutdown (began 2025-10-01) disrupted several BLS releases;
the ACTUAL (revised) release dates are used and annotated below. See the BLS
revised-dates notice: https://www.bls.gov/bls/2025-lapse-revised-release-dates.htm.

Shutdown-affected entries (actual dates used):
* 2025 CPI: the September-reference release was rescheduled to 2025-10-24; the
  October-reference CPI was NEVER published (data not collected); the
  November-reference CPI released 2025-12-18. So 2025 has NO mid-November CPI.
* 2025 NFP: the September-reference Employment Situation released 2025-11-20
  (delayed; the originally-scheduled early-October release did not occur during
  the shutdown); the November-reference release was 2025-12-16. October-reference
  household data was not collected.

Coverage: 2025-01 → 2026-08 is fully primary-source verified (spanning the whole
intraday option-data window, ~2025-01 → 2026-07, with margin). Full-year 2026
FOMC dates are from the Fed's published calendar. NFP/CPI releases for 2026-09
onward are intentionally OMITTED rather than guessed — they are beyond the data
window and the post-shutdown BLS schedule shifted; add them here (flag
``_TENTATIVE`` until archive-confirmed) when needed.

``_TENTATIVE`` is the set of ISO dates whose accuracy is NOT fully verified
(scheduled / subject to change). It is currently EMPTY — every date below is
primary-source confirmed. The mechanism exists so a future scheduled-but-
unconfirmed date can be added without silently implying certainty.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

# --------------------------------------------------------------------------- #
# Event-type identifiers (the ONLY valid ``event_type`` values).
# --------------------------------------------------------------------------- #
FOMC = "FOMC"
NFP = "NFP"
CPI = "CPI"
EVENT_TYPES: tuple[str, ...] = (FOMC, NFP, CPI)


@dataclass(frozen=True)
class EventDay:
    """One curated macro event day.

    ``date`` is the calendar day the event lands (decision/release day);
    ``event_type`` is one of :data:`EVENT_TYPES`; ``tentative`` is True when the
    date is scheduled-but-unconfirmed (a downstream consumer should surface it as
    provisional). Frozen + primitive fields => hashable and JSON-friendly.
    """

    date: date
    event_type: str
    tentative: bool = False


# --------------------------------------------------------------------------- #
# CURATED DATA — edit these tuples to correct/extend the calendar.
# Each date is primary-source verified (see module docstring). Grouped by year
# for readability; order within a tuple is not significant (the loader sorts).
# --------------------------------------------------------------------------- #
_FOMC_DATES: tuple[str, ...] = (
    # 2025 — statement/decision day (2nd meeting day). federalreserve.gov.
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026 — federalreserve.gov published calendar.
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)

_NFP_DATES: tuple[str, ...] = (
    # 2025 — BLS Employment Situation release days (archive-URL confirmed).
    "2025-01-10",  # Dec-2024 data (empsit_01102025)
    "2025-02-07",  # Jan (empsit_02072025)
    "2025-03-07",  # Feb
    "2025-04-04",  # Mar (empsit_04042025)
    "2025-05-02",  # Apr (empsit_05022025)
    "2025-06-06",  # May (empsit_06062025)
    "2025-07-03",  # Jun
    "2025-08-01",  # Jul (empsit_08012025 / widely reported)
    "2025-09-05",  # Aug (empsit_09052025)
    "2025-11-20",  # Sep — SHUTDOWN-DELAYED (empsit_11202025)
    "2025-12-16",  # Nov — (empsit_12162025); Oct est. data folded in
    # 2026 — BLS release days (archive-URL confirmed through the data window).
    "2026-01-09",  # Dec-2025 data (empsit_01092026)
    "2026-02-11",  # Jan (empsit_02112026)
    "2026-03-06",  # Feb (empsit_03062026)
    "2026-04-03",  # Mar (empsit_04032026)
    "2026-05-08",  # Apr (empsit_05082026)
    "2026-06-05",  # May (empsit_06052026)
    "2026-07-02",  # Jun (empsit_07022026)
    "2026-08-07",  # Jul (empsit_08072026)
)

_CPI_DATES: tuple[str, ...] = (
    # 2025 — BLS CPI release days (archive-URL confirmed).
    "2025-01-15",  # Dec-2024 data (cpi_01152025)
    "2025-02-12",  # Jan (cpi_02122025)
    "2025-03-12",  # Feb
    "2025-04-10",  # Mar (cpi_04102025)
    "2025-05-13",  # Apr (cpi_05132025)
    "2025-06-11",  # May (cpi_06112025)
    "2025-07-15",  # Jun (cpi_07152025)
    "2025-08-12",  # Jul (cpi_08122025)
    "2025-09-11",  # Aug (cpi_09112025)
    "2025-10-24",  # Sep — SHUTDOWN-RESCHEDULED (cpi_10242025)
    "2025-12-18",  # Nov — SHUTDOWN-SHIFTED; Oct CPI never published
    # 2026 — BLS CPI release days (archive-URL confirmed through the data window).
    "2026-01-13",  # Dec-2025 data (cpi_01132026)
    "2026-02-13",  # Jan (cpi_02132026)
    "2026-03-11",  # Feb (cpi_03112026)
    "2026-04-10",  # Mar (cpi_04102026)
    "2026-05-12",  # Apr (cpi_05122026)
    "2026-06-10",  # May (cpi_06102026)
    "2026-07-14",  # Jun (cpi_07142026)
    "2026-08-12",  # Jul (cpi_08122026)
)

# Scheduled-but-unconfirmed ISO dates (surfaced as provisional). Currently empty
# — every curated date above is primary-source verified.
_TENTATIVE: frozenset[str] = frozenset()

_RAW: dict[str, tuple[str, ...]] = {
    FOMC: _FOMC_DATES,
    NFP: _NFP_DATES,
    CPI: _CPI_DATES,
}


# --------------------------------------------------------------------------- #
# Pure builder (unit-testable on synthetic input) + the assembled calendar.
# --------------------------------------------------------------------------- #
def _build_calendar(
    raw: dict[str, tuple[str, ...]], tentative_iso: frozenset[str]
) -> tuple[EventDay, ...]:
    """Expand the raw ISO tuples into a sorted tuple of :class:`EventDay`.

    A ``ValueError`` from ``date.fromisoformat`` on a malformed literal fails
    LOUDLY at import (a curation typo must never be silently swallowed). Sorted
    by ``(date, event_type)`` for a stable, deterministic ordering.
    """
    days: list[EventDay] = []
    for etype, isos in raw.items():
        for iso in isos:
            days.append(
                EventDay(
                    date=date.fromisoformat(iso),
                    event_type=etype,
                    tentative=iso in tentative_iso,
                )
            )
    return tuple(sorted(days, key=lambda e: (e.date, e.event_type)))


_CALENDAR: tuple[EventDay, ...] = _build_calendar(_RAW, _TENTATIVE)


# --------------------------------------------------------------------------- #
# Loader API — the read surface both the endpoint (A3) and the allowlist
# resolver (F3.2) consume.
# --------------------------------------------------------------------------- #
def _validate_type(event_type: str) -> str:
    """Reject an unknown event type LOUDLY (never silently return nothing)."""
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"unknown event_type {event_type!r} (valid: {', '.join(EVENT_TYPES)})"
        )
    return event_type


def all_event_days() -> tuple[EventDay, ...]:
    """Every curated :class:`EventDay`, sorted by ``(date, event_type)``."""
    return _CALENDAR


def event_days(event_type: str) -> tuple[EventDay, ...]:
    """The curated :class:`EventDay` entries for one event type (date-sorted)."""
    _validate_type(event_type)
    return tuple(e for e in _CALENDAR if e.event_type == event_type)


def event_dates(event_type: str) -> tuple[date, ...]:
    """The curated dates for one event type, ascending."""
    return tuple(e.date for e in event_days(event_type))


def all_event_dates() -> tuple[date, ...]:
    """The UNION of all event dates across types, ascending and de-duplicated.

    (A day can host more than one event — e.g. a CPI print on an FOMC day; the
    union collapses it to a single calendar date.)
    """
    return tuple(sorted({e.date for e in _CALENDAR}))


def event_dates_for_types(event_types: Iterable[str]) -> frozenset[date]:
    """The union of dates for the given set of event types.

    The set the F3.2 allowlist resolver joins with its explicit dates. An unknown
    type raises ``ValueError``. An empty iterable yields an empty set (no dates),
    NOT all dates — the caller decides emptiness semantics.
    """
    wanted = {_validate_type(t) for t in event_types}
    return frozenset(e.date for e in _CALENDAR if e.event_type in wanted)


def tentative_days() -> tuple[EventDay, ...]:
    """The curated event days flagged tentative (scheduled / unconfirmed)."""
    return tuple(e for e in _CALENDAR if e.tentative)
