/**
 * Pure utility functions for portfolio calculations.
 * Extracted for testability — no React or charting dependencies.
 */

/**
 * Normalize an array so the first valid value becomes 100.
 */
export function normalizeTo100(values) {
  if (!values || values.length === 0) return values;
  const base = values[0];
  if (!base || base === 0) return values;
  return values.map((v) => (v / base) * 100);
}

/**
 * Un-invert a short leg's equity back to its long equivalent.
 * Backend computes short legs as: equity = 2 * initial - equity_long
 * So to recover the long curve: equity_long = 2 * initial - equity_short
 */
export function toLongEquivalent(values) {
  if (!values || values.length === 0) return values;
  const initial = values[0];
  return values.map((v) => 2 * initial - v);
}

/**
 * Format a decimal return as percentage string (e.g. 0.05 -> "+5.0%").
 */
export function formatReturn(value) {
  if (value == null || isNaN(value)) return '\u2013';
  const pct = (value * 100).toFixed(1);
  const sign = value > 0 ? '+' : '';
  return `${sign}${pct}%`;
}

/**
 * Get background style for heatmap cell.
 * Green for positive, red for negative, intensity proportional to magnitude.
 */
export function cellBgStyle(value, maxAbs) {
  if (value == null || isNaN(value) || maxAbs === 0) return undefined;
  const ratio = Math.min(Math.abs(value) / maxAbs, 1);
  const opacity = ratio * 0.3;
  if (value > 0) return { backgroundColor: `rgba(34, 197, 94, ${opacity})` };
  if (value < 0) return { backgroundColor: `rgba(239, 68, 68, ${opacity})` };
  return undefined;
}

/**
 * Convert a normal compounded return to log return: ln(1 + R).
 */
export function toLogReturn(value) {
  if (value == null || isNaN(value)) return value;
  // R <= -1 means total loss — ln(1+R) is undefined, show as dash
  if (value <= -1) return NaN;
  return Math.log(1 + value);
}
