// A4 — Ladder entry-time / rung PnL attribution (W5/P4, unlocked by F4.1).
//
// PURE POST-PROCESSING over the run response `days[]` — NO engine/schema/
// serializer change. F4.1's serializer adds `entries[]` (per-rung rows) ONLY
// on laddered days (see w5_f4_1_laddered_multientry_report.md §5); this
// module aggregates those rows ACROSS DAYS to answer "which rung / entry
// time performs best?"
//
// Bucketing key: RUNG INDEX (0-based position within a day's own `entries`,
// sorted by `entry_ts`) — NOT the raw UTC time-of-day extracted from
// `entry_ts`. ASSUMPTION (named per project convention): `_ladder_entry_times`
// resolves each rung's ET wall-clock time to UTC PER DAY
// (`resolve_et_to_utc(day, ...)`, tcg/core/api/intraday_backtest.py), so the
// same logical rung (e.g. "first entry, 10:00 ET") lands at a UTC `entry_ts`
// that shifts by exactly one hour across a DST boundary. Bucketing on the raw
// UTC time-of-day would silently split one rung into two buckets around every
// DST change (~mid-March / early-November); bucketing on rung index is
// DST-safe because the ladder's ORDER is invariant even though the absolute
// UTC offset is not. Each bucket carries the most common UTC time-of-day
// label seen (for a human-readable axis/table) plus a `timeVaries` flag when
// the label isn't unanimous, so a genuine DST split (or a day whose ladder
// config differs, e.g. a `custom_days` override) stays visible instead of
// being hidden by the index-based grouping.
//
// Only TRADED rungs (`status === 'ok'` with a finite `weighted_pnl_usd`)
// contribute to count/sum/mean/win-rate — mirrors weekdayAttribution.js /
// eventAttribution.js excluding non-traded days. Skipped/gapped rungs
// (`status !== 'ok'`, e.g. `skip_reason: 'max_concurrent'` or a data gap)
// still occupy their rung's bucket slot (surfaced via `nSkipped`) so a rung
// that mostly fails to fill is visible rather than silently absent.
//
// "Not laddered": a run with no per-entry rows at all (single-entry runs
// never carry `entries[]` — F4.1 report §5) or where every laddered day has
// at most ONE rung (nothing to compare rung-to-rung) yields
// `available: false` with a `reason`, so the view can render a hint instead
// of a degenerate one-bar chart.

const TIME_RE = /T(\d{2}:\d{2})/;

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function timeOfDayLabel(entryTs) {
  if (typeof entryTs !== 'string') return null;
  const m = TIME_RE.exec(entryTs);
  return m ? `${m[1]}Z` : null;
}

function median(sortedValues) {
  const n = sortedValues.length;
  if (n === 0) return null;
  const mid = Math.floor(n / 2);
  return n % 2 === 0 ? (sortedValues[mid - 1] + sortedValues[mid]) / 2 : sortedValues[mid];
}

// One laddered day's rows, ordered by `entry_ts` ascending. Rows with a
// missing/unparseable `entry_ts` are dropped — their rung position can't be
// trusted, so they are silently excluded rather than mis-bucketed.
function orderedRungs(day) {
  const rows = (Array.isArray(day.entries) ? day.entries : []).filter(
    (e) => e && typeof e === 'object' && typeof e.entry_ts === 'string'
      && !Number.isNaN(Date.parse(e.entry_ts)),
  );
  rows.sort((a, b) => Date.parse(a.entry_ts) - Date.parse(b.entry_ts));
  return rows;
}

function buildBucket(idx, rows) {
  const labelCounts = new Map();
  let n = 0;
  let nSkipped = 0;
  let sumUsd = 0;
  let wins = 0;
  const usdValues = [];

  for (const row of rows) {
    const label = timeOfDayLabel(row.entry_ts);
    if (label) labelCounts.set(label, (labelCounts.get(label) || 0) + 1);

    const traded = row.status === 'ok' && isFiniteNumber(row.weighted_pnl_usd);
    if (!traded) { nSkipped += 1; continue; }
    const usd = row.weighted_pnl_usd;
    n += 1;
    sumUsd += usd;
    usdValues.push(usd);
    if (usd > 0) wins += 1;
  }

  let label = `Rung ${idx + 1}`;
  let timeVaries = false;
  if (labelCounts.size > 0) {
    const sorted = [...labelCounts.entries()].sort((a, b) => b[1] - a[1]);
    [[label]] = sorted;
    timeVaries = sorted.length > 1;
  }

  const sortedUsd = [...usdValues].sort((a, b) => a - b);
  return {
    rung: idx,
    label,
    timeVaries,
    n,
    nSkipped,
    sumUsd,
    meanUsd: n > 0 ? sumUsd / n : null,
    medianUsd: median(sortedUsd),
    winRate: n > 0 ? wins / n : null,
  };
}

function emptyResult(reason) {
  return { available: false, reason, buckets: [], overall: null };
}

/**
 * Aggregate a laddered run's per-entry rows by RUNG INDEX across all days.
 *
 * @param {Array<object|null|undefined>} days - the run response's `days[]`.
 *   A laddered day carries `entries: [{ entry_ts, status, weighted_pnl_usd,
 *   skip_reason?, pnl? }, ...]` (F4.1 serializer, ascending by `entry_ts`); a
 *   non-laddered day has no `entries` key.
 * @returns {{
 *   available: boolean,
 *   reason: ('no_entries'|'single_rung')|undefined,
 *   buckets: Array<{ rung: number, label: string, timeVaries: boolean,
 *     n: number, nSkipped: number, sumUsd: number, meanUsd: number|null,
 *     medianUsd: number|null, winRate: number|null }>,
 *   overall: { nDays: number, nEntries: number, nTraded: number,
 *     sumUsd: number, meanUsd: number|null, winRate: number|null } | null,
 * }} `available` is false — with `reason` set — when no day carries any
 *   per-entry rows (`'no_entries'`, e.g. a single-entry run) or every
 *   laddered day has at most one rung (`'single_rung'`, nothing to compare).
 *   Callers should render a concise hint instead of a chart in both cases.
 *   When available, `buckets` has one entry per rung index found (0 = the
 *   day's first entry), ordered ascending, and `overall` summarizes across
 *   ALL traded rungs.
 */
export function computeLadderAnalysis(days) {
  const perDay = [];
  for (const day of Array.isArray(days) ? days : []) {
    if (!day || typeof day !== 'object') continue;
    const rungs = orderedRungs(day);
    if (rungs.length > 0) perDay.push(rungs);
  }

  if (perDay.length === 0) return emptyResult('no_entries');

  const maxRungCount = Math.max(...perDay.map((r) => r.length));
  if (maxRungCount <= 1) return emptyResult('single_rung');

  const bucketRows = Array.from({ length: maxRungCount }, () => []);
  for (const rungs of perDay) {
    rungs.forEach((row, idx) => bucketRows[idx].push(row));
  }

  const buckets = bucketRows.map((rows, idx) => buildBucket(idx, rows));

  const nTraded = buckets.reduce((acc, b) => acc + b.n, 0);
  const sumUsd = buckets.reduce((acc, b) => acc + b.sumUsd, 0);
  const wins = buckets.reduce((acc, b) => acc + Math.round((b.winRate || 0) * b.n), 0);

  return {
    available: true,
    buckets,
    overall: {
      nDays: perDay.length,
      nEntries: perDay.reduce((acc, r) => acc + r.length, 0),
      nTraded,
      sumUsd,
      meanUsd: nTraded > 0 ? sumUsd / nTraded : null,
      winRate: nTraded > 0 ? wins / nTraded : null,
    },
  };
}
