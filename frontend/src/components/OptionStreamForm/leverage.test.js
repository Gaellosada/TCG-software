import { describe, it, expect } from 'vitest';
import {
  LEVERAGE_BANDS,
  leverageBand,
  computeImpliedLeverage,
  premiumPctOfStrike,
  wipeoutFactor,
  selectionLabel,
  formatLeverage,
} from './leverage';

describe('computeImpliedLeverage', () => {
  it('is navFraction × strike / premiumMid', () => {
    // 10Δ put on SPX ~5100 strike, premium ~23 → strike/premium ≈ 221.7
    const lev = computeImpliedLeverage({ navFraction: 1.0, strike: 5100, premiumMid: 23 });
    expect(lev).toBeCloseTo(221.74, 1);
  });

  it('scales linearly with navFraction (Size% recompute)', () => {
    const full = computeImpliedLeverage({ navFraction: 1.0, strike: 5100, premiumMid: 23 });
    const small = computeImpliedLeverage({ navFraction: 0.01, strike: 5100, premiumMid: 23 });
    expect(small).toBeCloseTo(full * 0.01, 6);
  });

  it('returns null on missing / non-finite / non-positive inputs', () => {
    expect(computeImpliedLeverage({ navFraction: 1, strike: 5100, premiumMid: null })).toBeNull();
    expect(computeImpliedLeverage({ navFraction: 1, strike: 5100, premiumMid: 0 })).toBeNull();
    expect(computeImpliedLeverage({ navFraction: 1, strike: 5100, premiumMid: NaN })).toBeNull();
    expect(computeImpliedLeverage({ navFraction: 0, strike: 5100, premiumMid: 23 })).toBeNull();
    expect(computeImpliedLeverage({ navFraction: 1, strike: -5, premiumMid: 23 })).toBeNull();
  });
});

describe('leverageBand', () => {
  it('green below amber threshold, amber up to red, red beyond', () => {
    expect(leverageBand(1.5)).toBe('green');
    expect(leverageBand(LEVERAGE_BANDS.amber)).toBe('amber');
    expect(leverageBand(5)).toBe('amber');
    expect(leverageBand(LEVERAGE_BANDS.red)).toBe('amber');
    expect(leverageBand(11)).toBe('red');
    expect(leverageBand(220)).toBe('red');
  });

  it('returns null on unusable values', () => {
    expect(leverageBand(null)).toBeNull();
    expect(leverageBand(0)).toBeNull();
    expect(leverageBand(NaN)).toBeNull();
  });
});

describe('premiumPctOfStrike', () => {
  it('is premium/strike × 100', () => {
    expect(premiumPctOfStrike(5100, 23)).toBeCloseTo(0.451, 3);
  });
  it('null on bad inputs', () => {
    expect(premiumPctOfStrike(0, 23)).toBeNull();
    expect(premiumPctOfStrike(5100, null)).toBeNull();
  });
});

describe('wipeoutFactor', () => {
  it('is 1 + 1/navFraction — 2× at 100%, ~4× at 33%', () => {
    expect(wipeoutFactor(1.0)).toBeCloseTo(2.0, 6);
    expect(wipeoutFactor(1 / 3)).toBeCloseTo(4.0, 6);
    expect(wipeoutFactor(0.5)).toBeCloseTo(3.0, 6);
  });
  it('null on bad inputs', () => {
    expect(wipeoutFactor(0)).toBeNull();
    expect(wipeoutFactor(null)).toBeNull();
  });
});

describe('selectionLabel', () => {
  it('formats delta / moneyness / strike', () => {
    expect(selectionLabel({ kind: 'by_delta', target: -0.1 })).toBe('10Δ');
    expect(selectionLabel({ kind: 'by_delta', target: 0.25 })).toBe('25Δ');
    expect(selectionLabel({ kind: 'by_moneyness', target: 1.0 })).toBe('1.00 K/S');
    expect(selectionLabel({ kind: 'by_strike', strike: 5100 })).toBe('5100-strike');
    expect(selectionLabel(null)).toBe('selected');
  });
});

describe('formatLeverage', () => {
  it('rounds by magnitude', () => {
    expect(formatLeverage(221.7)).toBe('222×');
    expect(formatLeverage(4.37)).toBe('4.4×');
    expect(formatLeverage(0.45)).toBe('0.45×');
    expect(formatLeverage(0)).toBeNull();
  });
});
