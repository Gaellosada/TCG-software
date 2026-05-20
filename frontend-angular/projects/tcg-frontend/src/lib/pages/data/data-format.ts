/**
 * Numeric / date formatters used by Data-page subcomponents. Mirrors
 * React's `utils/format.js` (the subset Data consumes) and `utils/ohlcHelpers.js`.
 *
 * Library-internal: NOT re-exported from `public-api.ts`. Components
 * import via relative path.
 */

/** Format a YYYYMMDD integer as `YYYY-MM-DD`. */
export function tcgFormatDateInt(dateInt: number | string | null | undefined): string {
  if (dateInt == null) return '--';
  const s = String(dateInt);
  if (s.length !== 8) return s;
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
}

/** Today's ISO date (`YYYY-MM-DD`) in local time. */
export function tcgTodayIso(): string {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/** Adds N calendar days to an ISO date string. */
export function tcgAddDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00`);
  d.setDate(d.getDate() + days);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/** Day-difference between two ISO date strings (`a - b` in days). */
export function tcgDaysBetween(isoA: string | null, isoB: string | null): number | null {
  if (!isoA || !isoB) return null;
  const a = new Date(`${isoA}T00:00:00`);
  const b = new Date(`${isoB}T00:00:00`);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return null;
  return Math.round((a.getTime() - b.getTime()) / 86400000);
}

/** Fixed-decimal formatter; em-dash for null/NaN. */
export function tcgFmt(value: number | string | null | undefined, decimals: number): string {
  if (value === null || value === undefined) return '—';
  const num = Number(value);
  if (Number.isNaN(num)) return '—';
  return num.toFixed(decimals);
}

/** Integer formatter with thousands separator; em-dash for null/NaN. */
export function tcgFmtInt(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const num = Number(value);
  if (Number.isNaN(num)) return '—';
  return Math.round(num).toLocaleString();
}

// ─────────────────────────────────────────────────────────────────────
// OHLC helpers (subset of React's ohlcHelpers.js used by Data page)
// ─────────────────────────────────────────────────────────────────────

/**
 * Per-bar OHLC validity. Mirrors the React heuristic — rejects zero-valued
 * fields, OHLC inversions, and exact-marubozu bars (which typically arise
 * from offset-shifted zero-fields).
 */
export function tcgIsValidOhlc(
  o: number | null | undefined,
  h: number | null | undefined,
  l: number | null | undefined,
  c: number | null | undefined,
): boolean {
  if (o == null || h == null || l == null || c == null) return false;
  if (o === 0 || h === 0 || l === 0 || c === 0) return false;
  if (h < Math.max(o, c)) return false;
  if (l > Math.min(o, c)) return false;
  if (h < l) return false;
  const range = h - l;
  if (range > 0) {
    const body = Math.abs(o - c);
    if (body === range && (o === l || o === h)) return false;
  }
  return true;
}

export interface TcgPreparedChart {
  hasOHLC: boolean;
  hasVolume: boolean;
  open: Array<number | null> | null;
  high: Array<number | null> | null;
  low: Array<number | null> | null;
  close: Array<number | null> | null;
}

export interface TcgRawPriceData {
  dates: Array<number | string>;
  open?: number[];
  high?: number[];
  low?: number[];
  close?: number[];
  volume?: number[];
}

/**
 * Prepare chart data from raw OHLC arrays. Bars failing per-bar validity
 * are nulled (Plotly skips them in candlestick traces). Returns:
 *   hasOHLC:   ≥50% of bars valid
 *   hasVolume: ≥5% of volume bars non-zero
 */
export function tcgPrepareChartData(data: TcgRawPriceData): TcgPreparedChart {
  const hasArrays = !!(data.open && data.high && data.low && data.close);
  const volume = data.volume;
  const nonZeroVols = volume ? volume.filter((v) => v > 0).length : 0;
  const hasVolume = !!volume && nonZeroVols > volume.length * 0.05;

  if (!hasArrays || !data.open || !data.high || !data.low || !data.close) {
    return { hasOHLC: false, hasVolume, open: null, high: null, low: null, close: null };
  }

  const len = data.open.length;
  let validCount = 0;
  const open: Array<number | null> = new Array(len);
  const high: Array<number | null> = new Array(len);
  const low: Array<number | null> = new Array(len);
  const close: Array<number | null> = new Array(len);

  for (let i = 0; i < len; i++) {
    if (tcgIsValidOhlc(data.open[i], data.high[i], data.low[i], data.close[i])) {
      open[i] = data.open[i];
      high[i] = data.high[i];
      low[i] = data.low[i];
      close[i] = data.close[i];
      validCount++;
    } else {
      open[i] = null;
      high[i] = null;
      low[i] = null;
      close[i] = null;
    }
  }

  return { hasOHLC: validCount > len * 0.5, hasVolume, open, high, low, close };
}
