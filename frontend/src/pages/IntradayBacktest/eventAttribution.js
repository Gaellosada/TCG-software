// Event-day attribution (A3 / W4-P3).
//
// PURE POST-PROCESSING that joins the run response's `days[]` against the F3.1
// curated static event calendar (`GET /api/intraday-backtest/event-calendar`,
// shape `{ event_types, events:{FOMC:[{date,tentative}],NFP,CPI}, all_dates,
// tentative_dates }`) — NO engine/schema/serializer change. Answers "is
// performance concentrated on FOMC/NFP/CPI days, and which structurally hurt?"
//
// Only TRADED days (a day with a finite `pnl.total_pnl_usd`) contribute to any
// bucket — same convention as weekdayAttribution.js / regimeSensitivity.js.
//
// Multi-membership: a day may match more than one event type (e.g. a rare
// FOMC+CPI overlap). It is counted in EACH matching per-type bucket (so FOMC
// N + NFP N + CPI N can sum to MORE than the "any event" N), but in the
// any-vs-non-event split it is counted exactly ONCE as "event" — the split is
// a partition of the traded days, the per-type buckets are not.

export const EVENT_TYPES = ['FOMC', 'NFP', 'CPI'];

export const EVENT_TYPE_LABELS = { FOMC: 'FOMC', NFP: 'NFP', CPI: 'CPI' };

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

// Traded days only, reduced to { date, usd }. Mirrors the other attribution
// modules' non-traded exclusion (skipped/excluded/data-gap days carry no PnL
// to attribute).
function tradedDays(days) {
  const out = [];
  for (const day of Array.isArray(days) ? days : []) {
    if (!day || typeof day !== 'object') continue;
    const pnl = day.pnl;
    if (!pnl || typeof pnl !== 'object' || !isFiniteNumber(pnl.total_pnl_usd)) continue;
    if (typeof day.date !== 'string') continue;
    out.push({ date: day.date, usd: pnl.total_pnl_usd });
  }
  return out;
}

// Build a Set of date strings for one event type from the endpoint's
// `events[type]` array (`[{date, tentative}, ...]`). Returns an empty Set
// (never null) for a missing/malformed type so downstream bucketing never
// has to special-case "no data for this type" separately from "type present
// but empty" — both simply contribute N=0.
function typeDateSet(eventCalendar, type) {
  const set = new Set();
  const entries = eventCalendar && typeof eventCalendar === 'object'
    ? eventCalendar.events && eventCalendar.events[type]
    : null;
  if (!Array.isArray(entries)) return set;
  for (const e of entries) {
    if (e && typeof e === 'object' && typeof e.date === 'string') set.add(e.date);
  }
  return set;
}

function hasAnyCalendarData(eventCalendar) {
  return !!(eventCalendar && typeof eventCalendar === 'object'
    && eventCalendar.events && typeof eventCalendar.events === 'object');
}

function emptyResult(reason) {
  return { available: false, reason, typeBuckets: [], comparison: [] };
}

function statBucket(key, label, rows) {
  const n = rows.length;
  const sumUsd = rows.reduce((acc, r) => acc + r.usd, 0);
  const wins = rows.filter((r) => r.usd > 0).length;
  return {
    key,
    label,
    n,
    sumUsd,
    meanUsd: n > 0 ? sumUsd / n : null,
    winRate: n > 0 ? wins / n : null,
  };
}

/**
 * Join a run response's `days[]` with the curated event calendar and
 * aggregate per-event-type and event-vs-non-event PnL attribution.
 *
 * @param {Array<object|null|undefined>} days - the run response's `days[]`.
 * @param {object|null|undefined} eventCalendar - the F3.1 event-calendar
 *   endpoint payload, or null/undefined if it failed to load / is not yet
 *   available. Shape: `{ events: { FOMC: [{date,tentative}], NFP, CPI } }`.
 * @returns {{
 *   available: boolean,
 *   reason: ('no_calendar'|'no_overlap')|undefined,
 *   typeBuckets: Array<{ key: string, label: string, n: number, sumUsd: number,
 *     meanUsd: number|null, winRate: number|null }>,
 *   comparison: Array<{ key: 'event'|'non_event', label: string, n: number,
 *     sumUsd: number, meanUsd: number|null, winRate: number|null }>,
 * }} `available` is false — with `reason` set — when the calendar failed to
 *   load/is absent (`'no_calendar'`) or loaded but no traded day in this run
 *   falls on ANY curated event date (`'no_overlap'`). Callers should render a
 *   concise hint instead of a chart in both cases. When available,
 *   `typeBuckets` has one entry per `EVENT_TYPES` (FOMC, NFP, CPI order) and
 *   `comparison` has exactly two entries (`event`, `non_event`) that
 *   partition the traded days.
 */
export function computeEventAttribution(days, eventCalendar) {
  if (!hasAnyCalendarData(eventCalendar)) return emptyResult('no_calendar');

  const typeSets = Object.fromEntries(EVENT_TYPES.map((t) => [t, typeDateSet(eventCalendar, t)]));
  const traded = tradedDays(days);

  const eventRows = [];
  const nonEventRows = [];
  for (const row of traded) {
    const isEvent = EVENT_TYPES.some((t) => typeSets[t].has(row.date));
    if (isEvent) eventRows.push(row);
    else nonEventRows.push(row);
  }

  if (eventRows.length === 0) return emptyResult('no_overlap');

  const typeBuckets = EVENT_TYPES.map((t) => {
    const rows = traded.filter((row) => typeSets[t].has(row.date));
    return statBucket(t, EVENT_TYPE_LABELS[t], rows);
  });

  const comparison = [
    statBucket('event', 'Event day', eventRows),
    statBucket('non_event', 'Non-event day', nonEventRows),
  ];

  return { available: true, typeBuckets, comparison };
}
