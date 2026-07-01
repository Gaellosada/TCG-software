/**
 * Format a Date object or ISO string as YYYY-MM-DD.
 * @param {Date | string} date
 * @returns {string}
 */
export function formatDate(date) {
  const d = typeof date === 'string' ? new Date(date) : date;
  // Format from local components so a Date built from local midnight
  // doesn't slip to the previous day in positive-offset timezones.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/**
 * Format a Date object or ISO timestamp string as "YYYY-MM-DD HH:MM" in local
 * time. Used where a bare date is ambiguous — e.g. tickets created the same
 * day need a time to order/disambiguate them. Returns '--' for an unparseable
 * input so the UI never shows "Invalid Date".
 * @param {Date | string} value
 * @returns {string}
 */
export function formatDateTime(value) {
  const d = typeof value === 'string' ? new Date(value) : value;
  if (!(d instanceof Date) || Number.isNaN(d.getTime())) return '--';
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${y}-${mo}-${day} ${hh}:${mm}`;
}

/**
 * Default exploration window for views whose data carries no inherent date
 * range — notably option_stream legs (the backend REQUIRES an explicit window
 * to enumerate their trade dates). Returns ``{ start, end }`` as YYYY-MM-DD,
 * with ``end`` = today and ``start`` = ~5 years back — the platform's standard
 * long-history default. Shared by the basket explorer (Data/BasketChart) and
 * the portfolio editor (Portfolio/usePortfolio) so both prefill the same
 * window. The user can widen/narrow afterwards.
 *
 * Note: formats via toISOString (UTC) for parity with the original call sites;
 * unlike formatDate() this can land on the prior day near midnight in
 * positive-offset timezones, which is immaterial for a multi-year lookback.
 * @returns {{ start: string, end: string }}
 */
export function defaultDateRange() {
  const end = new Date();
  const start = new Date();
  start.setFullYear(start.getFullYear() - 5);
  const iso = (d) => d.toISOString().slice(0, 10);
  return { start: iso(start), end: iso(end) };
}

/**
 * Format a YYYYMMDD integer as YYYY-MM-DD string.
 * @param {number} dateInt
 * @returns {string}
 */
export function formatDateInt(dateInt) {
  if (dateInt == null) return '--';
  const s = String(dateInt);
  if (s.length !== 8) return s;
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
}

/**
 * Format a number with fixed decimal places and optional thousands separator.
 * @param {number} value
 * @param {number} decimals - Number of decimal places (default 2)
 * @returns {string}
 */
export function formatNumber(value, decimals = 2) {
  if (value == null || !Number.isFinite(value)) return '--';
  return value.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/**
 * Format a number as a percentage string.
 * @param {number} value - Value in decimal form (e.g. 0.05 for 5%)
 * @param {number} decimals - Number of decimal places (default 2)
 * @returns {string}
 */
export function formatPercent(value, decimals = 2) {
  if (value == null || !Number.isFinite(value)) return '--';
  return `${(value * 100).toFixed(decimals)}%`;
}

/**
 * Format a currency value.
 * @param {number} value
 * @param {string} currency - Currency code (default 'USD')
 * @returns {string}
 */
export function formatCurrency(value, currency = 'USD') {
  if (value == null || !Number.isFinite(value)) return '--';
  return value.toLocaleString('en-US', {
    style: 'currency',
    currency,
  });
}
