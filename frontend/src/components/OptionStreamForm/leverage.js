// Pure, side-effect-free maths for the option-leg implied-leverage readout.
//
// The hold form sizes an option leg by ``nav_times`` (shown as "Size", a raw
// multiplier): premium notional deployed = nav_times x NAV.  The corresponding
// UNDERLYING notional the position controls is a much larger multiple of NAV —
// that multiple is the implied leverage, and it is what makes a naked/short
// option leg able to wipe out equity.  Surfacing it as a concrete number turns
// the qualitative wipeout warning into an actionable figure.
//
//   implied_leverage = nav_times x strike / premium_mid
//
// Derivation (the contract multiplier cancels): with premium notional
// nav_times x NAV, the held quantity is q = nav_times x NAV / premium; the
// underlying notional it controls is q x strike = nav_times x NAV x
// strike/premium = leverage x NAV.  So only ONE representative (strike,
// premium) pair is needed from the backend; the nav_times (Size) scaling is
// entirely client-side (recompute on Size change WITHOUT refetching).

// Colour bands by implied leverage.  Single named constant so the thresholds
// are trivial to tune.  green < amber-threshold ; amber up to red-threshold ;
// red beyond.  (A 10-delta put premium is ~0.3-0.6% of strike, so a full-
// notional short at Size=1 lands deep in the red at ~150-300x.)
// NOTE: the Help page ("Implied leverage on the Size field" in
// pages/Help/HelpPage.jsx) mirrors these thresholds (2×, 10×) in prose — keep
// both in sync if you tune them (HelpPage.test.jsx derives its assertion here).
export const LEVERAGE_BANDS = { amber: 2, red: 10 };

/**
 * Classify an implied-leverage value into a colour band.
 * @returns {'green'|'amber'|'red'|null} null when the value is unusable.
 */
export function leverageBand(leverage) {
  if (typeof leverage !== 'number' || !Number.isFinite(leverage) || leverage <= 0) {
    return null;
  }
  if (leverage < LEVERAGE_BANDS.amber) return 'green';
  if (leverage <= LEVERAGE_BANDS.red) return 'amber';
  return 'red';
}

/**
 * implied_leverage = navFraction x strike / premiumMid.
 * @returns {number|null} null when any input is missing / non-finite /
 *   non-positive (premiumMid<=0 would divide-by-zero) — the caller then falls
 *   back to the qualitative hint.
 */
export function computeImpliedLeverage({ navFraction, strike, premiumMid }) {
  const nums = [navFraction, strike, premiumMid];
  if (!nums.every((x) => typeof x === 'number' && Number.isFinite(x))) return null;
  if (navFraction <= 0 || strike <= 0 || premiumMid <= 0) return null;
  return (navFraction * strike) / premiumMid;
}

/**
 * Premium as a percent of strike — the readout sub-line (e.g. 0.45%).
 * @returns {number|null}
 */
export function premiumPctOfStrike(strike, premiumMid) {
  if (
    typeof strike !== 'number'
    || typeof premiumMid !== 'number'
    || !Number.isFinite(strike)
    || !Number.isFinite(premiumMid)
    || strike <= 0
    || premiumMid < 0
  ) {
    return null;
  }
  return (premiumMid / strike) * 100;
}

/**
 * The premium multiple that wipes equity at this Size.  For a short leg sized
 * at navFraction of NAV, loss = navFraction x NAV x (f - 1), so equity is gone
 * when f = 1 + 1/navFraction (independent of strike/premium — it is a function
 * of the SIZE only).  At Size=1 (navFraction=1) a mere 2x premium spike
 * wipes out; at Size=0.33 it takes ~4x.
 * @returns {number|null}
 */
export function wipeoutFactor(navFraction) {
  if (typeof navFraction !== 'number' || !Number.isFinite(navFraction) || navFraction <= 0) {
    return null;
  }
  return 1 + 1 / navFraction;
}

/**
 * A short human label for the current selection criterion, for the sub-line
 * "(premium ≈ P% of strike for this <selection> <put/call>)".
 *   by_delta   → "10Δ"      (|target|x100, rounded)
 *   by_moneyness → "1.00 K/S"
 *   by_strike  → "5100-strike"
 */
export function selectionLabel(selection) {
  if (!selection || typeof selection !== 'object') return 'selected';
  if (selection.kind === 'by_delta' && typeof selection.target === 'number') {
    const mag = Math.round(Math.abs(selection.target) * 100);
    return `${mag}Δ`;
  }
  if (selection.kind === 'by_moneyness' && typeof selection.target === 'number') {
    return `${selection.target.toFixed(2)} K/S`;
  }
  if (selection.kind === 'by_strike' && typeof selection.strike === 'number') {
    return `${selection.strike}-strike`;
  }
  return 'selected';
}

/**
 * Format the leverage figure for display: "≈ 220× underlying notional".
 * Rounds sensibly by magnitude (no decimals >=10, one decimal below).
 */
export function formatLeverage(leverage) {
  if (typeof leverage !== 'number' || !Number.isFinite(leverage) || leverage <= 0) {
    return null;
  }
  if (leverage >= 10) return `${Math.round(leverage)}×`;
  if (leverage >= 1) return `${leverage.toFixed(1)}×`;
  return `${leverage.toFixed(2)}×`;
}
