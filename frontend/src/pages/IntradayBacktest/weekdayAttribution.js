// Weekday-attribution grouping (A1 / W2-P1).
//
// PURE POST-PROCESSING over the existing intraday backtest run response — no
// engine/schema/serializer change. HANDOFF.md §3: the response already carries
// per-day `date` + full PnL decomposition (`option_pnl_pts`, `hedge_pnl_pts`,
// `total_pnl_pts`, `total_pnl_usd`) in `days[]`. This module groups that by
// weekday (Mon..Fri) so the frontend can quantify Wed/Thu/Fri concentration.
//
// Only TRADED days (a day with a finite `pnl.total_pnl_usd`) contribute to the
// per-weekday stats — mirrors how the aggregate panel already distinguishes
// `n_traded` from `n_days`. Skipped / excluded / data-gap days have no PnL to
// attribute and would otherwise silently dilute the sample. `n` on each bucket
// IS the true sample size behind that bucket's mean/median/win-rate — the
// caller must surface it (data window is only ~1-1.5yr; thin buckets are not
// robust).

export const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

// Mon=0 .. Fri=4 for a "YYYY-MM-DD" date string, or null if unparseable or a
// weekend (defensive — trading-day dates should never land on Sat/Sun).
// Parsed via Date.UTC (not `new Date(str)`/local components) so the weekday
// is derived from the calendar date itself, immune to the host timezone —
// same convention as IntradayBacktestPage's groupDaysByMonth.
function weekdayIndex(dateStr) {
  if (typeof dateStr !== 'string') return null;
  const m = DATE_RE.exec(dateStr);
  if (!m) return null;
  const [, y, mo, d] = m;
  const dow = new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d))).getUTCDay(); // 0 Sun..6 Sat
  if (dow === 0 || dow === 6) return null;
  return dow - 1; // Mon=0 .. Fri=4
}

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function median(sortedValues) {
  const n = sortedValues.length;
  if (n === 0) return null;
  const mid = Math.floor(n / 2);
  return n % 2 === 0 ? (sortedValues[mid - 1] + sortedValues[mid]) / 2 : sortedValues[mid];
}

/**
 * Group a run response's `days[]` by weekday and compute PnL attribution
 * stats per weekday bucket.
 *
 * @param {Array<object|null|undefined>} days - the run response's `days[]`.
 *   Each element is expected to look like `{ date: 'YYYY-MM-DD', pnl: {
 *   total_pnl_usd, total_pnl_pts, ... } | null, ... }`. Malformed / null /
 *   undefined entries, unparseable dates, and non-traded days (no `pnl` or a
 *   non-finite `total_pnl_usd`) are silently skipped — they never crash the
 *   aggregation and never inflate a bucket's `n`.
 * @returns {Array<{
 *   weekday: string, n: number, sumUsd: number, sumPts: number,
 *   meanUsd: number|null, meanPts: number|null,
 *   medianUsd: number|null, medianPts: number|null,
 *   winRate: number|null,
 * }>} exactly 5 buckets, ordered Mon..Fri (WEEKDAY_LABELS). An empty/all-
 *   excluded input yields 5 zeroed buckets (n=0, sums=0, mean/median/winRate
 *   null) rather than throwing.
 */
export function groupPnlByWeekday(days) {
  const raw = WEEKDAY_LABELS.map(() => ({
    n: 0, sumUsd: 0, sumPts: 0, wins: 0, usdValues: [], ptsValues: [],
  }));

  for (const day of days || []) {
    if (!day || typeof day !== 'object') continue;
    const idx = weekdayIndex(day.date);
    if (idx === null) continue;
    const pnl = day.pnl;
    if (!pnl || typeof pnl !== 'object' || !isFiniteNumber(pnl.total_pnl_usd)) continue;

    const usd = pnl.total_pnl_usd;
    const pts = isFiniteNumber(pnl.total_pnl_pts) ? pnl.total_pnl_pts : 0;
    const bucket = raw[idx];
    bucket.n += 1;
    bucket.sumUsd += usd;
    bucket.sumPts += pts;
    bucket.usdValues.push(usd);
    bucket.ptsValues.push(pts);
    if (usd > 0) bucket.wins += 1;
  }

  return raw.map((b, i) => {
    const sortedUsd = [...b.usdValues].sort((a, c) => a - c);
    const sortedPts = [...b.ptsValues].sort((a, c) => a - c);
    return {
      weekday: WEEKDAY_LABELS[i],
      n: b.n,
      sumUsd: b.sumUsd,
      sumPts: b.sumPts,
      meanUsd: b.n > 0 ? b.sumUsd / b.n : null,
      meanPts: b.n > 0 ? b.sumPts / b.n : null,
      medianUsd: median(sortedUsd),
      medianPts: median(sortedPts),
      winRate: b.n > 0 ? b.wins / b.n : null,
    };
  });
}
