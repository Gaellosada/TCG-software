/**
 * Format a Date object or ISO string as YYYY-MM-DD.
 * @param {Date | string} date
 * @returns {string}
 */
export function formatDate(date) {
  const d = typeof date === 'string' ? new Date(date) : date;
  return d.toISOString().slice(0, 10);
}

/**
 * Format a number with fixed decimal places and optional thousands separator.
 * @param {number} value
 * @param {number} decimals - Number of decimal places (default 2)
 * @returns {string}
 */
export function formatNumber(value, decimals = 2) {
  if (value == null || Number.isNaN(value)) return '--';
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
  if (value == null || Number.isNaN(value)) return '--';
  return `${(value * 100).toFixed(decimals)}%`;
}

/**
 * Format a currency value.
 * @param {number} value
 * @param {string} currency - Currency code (default 'USD')
 * @returns {string}
 */
export function formatCurrency(value, currency = 'USD') {
  if (value == null || Number.isNaN(value)) return '--';
  return value.toLocaleString('en-US', {
    style: 'currency',
    currency,
  });
}
