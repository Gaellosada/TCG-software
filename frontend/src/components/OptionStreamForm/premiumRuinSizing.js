// Guaranteed-ruin sizing detector for a HELD option leg.
//
// A long option held to expiry, sized to spend ~the full NAV on premium at
// every roll (premium-notional sizing with nav_times ~1), decays toward zero:
// an OTM option usually expires worthless, so each cycle multiplies NAV by a
// small factor → geometric decay. This is CORRECT P&L, not a bug (see the PR#90
// investigation FINAL_REPORT, item 3) — but users may not intend it, so the UI
// flags it. Advisory ONLY: it never blocks compute and adds NO wire field.
//
// Thresholds are deliberately loose: the point is to catch the "full NAV on
// premium" shape, not to fire on merely aggressive-but-intentional sizing.
const FULL_NAV_NAV_TIMES = 0.9; // nav_times at/above this ~ the full NAV (or more) on premium
const FULL_NAV_WEIGHT = 90; // |weight| at/above this ~ full-NAV allocation

/**
 * True iff the leg's OWN config is the guaranteed-premium-decay shape:
 *   - an ``option_stream`` leg,
 *   - HELD between rolls (the fixed-contract $-P&L path; the daily-reselect
 *     %-return path this warning describes does not apply otherwise),
 *   - premium-notional sizing (the default; ``futures_notional`` sizes off the
 *     future's dollar notional and does NOT spend the full NAV on premium),
 *   - ``nav_times`` ~1 or more (spends ~the full NAV — or more, if levered — on
 *     premium each roll),
 *   - LONG, full-NAV weight. When the leg carries no ``weight`` yet (the
 *     add/edit form, whose default is long 100%), weight is treated as long
 *     full-weight — the warning is about the SIZING choice at the sizing control.
 *
 * @param {object|null|undefined} leg
 * @returns {boolean}
 */
export function isPremiumRuinSizing(leg) {
  if (!leg || leg.type !== 'option_stream') return false;
  if (leg.hold_between_rolls !== true) return false;
  if (leg.sizing_mode === 'futures_notional') return false;
  const navTimes = typeof leg.nav_times === 'number' ? leg.nav_times : 1;
  if (!(navTimes >= FULL_NAV_NAV_TIMES)) return false;
  // Weight is optional (absent in the add/edit form). When present it must be
  // LONG (positive) and ~full-NAV; a short or small-weight leg is not a
  // premium-bleed-to-zero risk.
  if (leg.weight !== undefined && leg.weight !== null && leg.weight !== '') {
    const w = Number(leg.weight);
    if (!Number.isFinite(w) || w <= 0) return false;
    if (Math.abs(w) < FULL_NAV_WEIGHT) return false;
  }
  return true;
}

export { FULL_NAV_NAV_TIMES, FULL_NAV_WEIGHT };
