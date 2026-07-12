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

// localStorage key for the opt-in local portfolio-result cache toggle.
export const PORTFOLIO_CACHE_KEY = 'tcg-portfolio-cache-enabled';

/**
 * Whether the local portfolio-result cache is enabled. Opt-in, DEFAULT OFF:
 * only the exact string 'true' enables it (mirrors the App.jsx boolean idiom).
 * Absent / any other value / unavailable localStorage → false (no behavior
 * change until the user turns it on).
 */
export function isPortfolioCacheEnabled() {
  try {
    return localStorage.getItem(PORTFOLIO_CACHE_KEY) === 'true';
  } catch {
    return false;
  }
}
