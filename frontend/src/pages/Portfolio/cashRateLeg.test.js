import { describe, it, expect } from 'vitest';
import {
  makeCashRateLeg,
  cashRateApiSpec,
  cashRateSummary,
  DEFAULT_CASH_RATE_SOURCE,
} from './cashRateLeg';

describe('cashRateLeg helpers', () => {
  it('makeCashRateLeg returns a flat 1%/yr, +100 leg with a readable label', () => {
    const leg = makeCashRateLeg();
    expect(leg.type).toBe('cash_rate');
    expect(leg.weight).toBe(100);
    expect(leg.label).toMatch(/cash/i);
    expect(leg.cash_rate).toEqual(DEFAULT_CASH_RATE_SOURCE);
    // Not a shared reference (mutating the leg must not mutate the default).
    leg.cash_rate.rate_pct = 5;
    expect(DEFAULT_CASH_RATE_SOURCE.rate_pct).toBe(1.0);
  });

  it('cashRateApiSpec emits a minimal flat wire spec', () => {
    const leg = { type: 'cash_rate', cash_rate: { kind: 'flat', rate_pct: 2.5, compound: true } };
    expect(cashRateApiSpec(leg)).toEqual({ kind: 'flat', rate_pct: 2.5, compound: true });
  });

  it('cashRateApiSpec falls back to flat 1% for a leg with no source', () => {
    expect(cashRateApiSpec({ type: 'cash_rate' })).toEqual({
      kind: 'flat',
      rate_pct: 1.0,
      compound: true,
    });
  });

  it('cashRateApiSpec emits the instrument ref for a series source', () => {
    const leg = {
      type: 'cash_rate',
      cash_rate: {
        kind: 'series',
        collection: 'FUT_RATE',
        symbol: 'RATE_USD',
        unit: 'percent',
        rate_pct: 2.0,
        compound: false,
      },
    };
    expect(cashRateApiSpec(leg)).toEqual({
      kind: 'series',
      collection: 'FUT_RATE',
      symbol: 'RATE_USD',
      unit: 'percent',
      rate_pct: 2.0,
      compound: false,
    });
  });

  it('cashRateApiSpec coerces a non-finite rate to 1.0 and compound defaults true', () => {
    const leg = { type: 'cash_rate', cash_rate: { kind: 'flat', rate_pct: '' } };
    expect(cashRateApiSpec(leg)).toEqual({ kind: 'flat', rate_pct: 1.0, compound: true });
  });

  it('cashRateSummary describes flat and series sources', () => {
    expect(cashRateSummary({ cash_rate: { kind: 'flat', rate_pct: 3 } })).toBe('flat 3.00%/yr');
    expect(
      cashRateSummary({ cash_rate: { kind: 'series', collection: 'FUT_RATE', symbol: 'RATE_USD' } }),
    ).toBe('series FUT_RATE/RATE_USD');
  });
});
