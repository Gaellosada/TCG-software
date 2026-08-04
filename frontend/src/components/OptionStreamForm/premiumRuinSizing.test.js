import { describe, it, expect } from 'vitest';
import { isPremiumRuinSizing } from './premiumRuinSizing';

// The canonical guaranteed-ruin leg: a long, held, premium-notional option
// sized to spend the full NAV on premium every roll (nav_times ~1, weight
// ~100%). See PR#90 investigation FINAL_REPORT item 3.
function ruinLeg(overrides = {}) {
  return {
    type: 'option_stream',
    option_type: 'P',
    hold_between_rolls: true,
    sizing_mode: 'premium_notional',
    nav_times: 1.0,
    weight: 100,
    ...overrides,
  };
}

describe('isPremiumRuinSizing', () => {
  it('is true for the canonical full-NAV premium long option leg', () => {
    expect(isPremiumRuinSizing(ruinLeg())).toBe(true);
  });

  it('is true when sizing_mode is absent (premium_notional is the default)', () => {
    const leg = ruinLeg();
    delete leg.sizing_mode;
    expect(isPremiumRuinSizing(leg)).toBe(true);
  });

  it('is true when nav_times is absent (defaults to 1)', () => {
    const leg = ruinLeg();
    delete leg.nav_times;
    expect(isPremiumRuinSizing(leg)).toBe(true);
  });

  it('is true with no weight (add/edit form default = long full-NAV)', () => {
    const leg = ruinLeg();
    delete leg.weight;
    expect(isPremiumRuinSizing(leg)).toBe(true);
  });

  it('is true when levered above full NAV (nav_times > 1)', () => {
    expect(isPremiumRuinSizing(ruinLeg({ nav_times: 3 }))).toBe(true);
  });

  it('is false under futures-notional sizing', () => {
    expect(isPremiumRuinSizing(ruinLeg({ sizing_mode: 'futures_notional' }))).toBe(false);
  });

  it('is false when nav_times is small (not spending the full NAV)', () => {
    expect(isPremiumRuinSizing(ruinLeg({ nav_times: 0.1 }))).toBe(false);
  });

  it('is false for a short leg (negative weight)', () => {
    expect(isPremiumRuinSizing(ruinLeg({ weight: -100 }))).toBe(false);
  });

  it('is false for a small-weight leg (not full-NAV allocation)', () => {
    expect(isPremiumRuinSizing(ruinLeg({ weight: 10 }))).toBe(false);
  });

  it('is false when the leg is not held between rolls', () => {
    expect(isPremiumRuinSizing(ruinLeg({ hold_between_rolls: false }))).toBe(false);
  });

  it('is false for a non-option leg', () => {
    expect(isPremiumRuinSizing({ type: 'continuous', hold_between_rolls: true, nav_times: 1, weight: 100 })).toBe(false);
  });

  it('is false for null / undefined', () => {
    expect(isPremiumRuinSizing(null)).toBe(false);
    expect(isPremiumRuinSizing(undefined)).toBe(false);
  });
});
