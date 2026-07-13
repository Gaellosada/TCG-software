// Unit boundary (Sign 7): localStorage stores percent (e.g. "4.5" means 4.5%).
// The wire contract and the metrics engine expect a fraction (0.045).
// This module is the single conversion site — all other code reads fractions.

export const DEFAULT_RISK_FREE_RATE_PCT = 4.0;
export const DEFAULT_RISK_FREE_RATE_FRACTION = 0.04;

/**
 * Read the user-configured default risk-free rate from localStorage and return
 * it as an annualized fraction (e.g. 0.04 for 4%).
 *
 * localStorage key: 'tcg-risk-free-rate' (string, percent, e.g. "4.00").
 * Falls back to DEFAULT_RISK_FREE_RATE_FRACTION (0.04) when the key is absent,
 * empty, non-numeric, negative, or localStorage is unavailable.
 *
 * Change applied on next Statistics mount — no cross-tab listener (out of scope).
 */
export function getRiskFreeRateFraction() {
  try {
    const raw = localStorage.getItem('tcg-risk-free-rate');
    if (raw == null || raw === '') return DEFAULT_RISK_FREE_RATE_FRACTION;
    const pct = parseFloat(raw);
    if (!Number.isFinite(pct) || pct < 0) return DEFAULT_RISK_FREE_RATE_FRACTION;
    return pct / 100;
  } catch {
    return DEFAULT_RISK_FREE_RATE_FRACTION;
  }
}

// localStorage key for the portfolio-result cache toggle. The toggle now drives
// a REQUEST FLAG (``use_cache``) sent to the backend's on-disk cache — there is
// no frontend result cache. DEFAULT ON: caching is on unless explicitly off.
export const PORTFOLIO_CACHE_KEY = 'tcg-portfolio-cache-enabled';

/**
 * Whether compute requests should ask the backend to cache. DEFAULT ON — true
 * unless the stored value is exactly the string 'false' (mirrors the App.jsx
 * boolean idiom). Absent / any other value / unavailable localStorage → true.
 * Read at mount by usePortfolio (a toggle change applies on the next mount).
 */
export function isPortfolioCacheEnabled() {
  try {
    return localStorage.getItem(PORTFOLIO_CACHE_KEY) !== 'false';
  } catch {
    return true;
  }
}
