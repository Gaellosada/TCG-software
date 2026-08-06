import { describe, it, expect } from 'vitest';
import {
  cashRateApiSpec,
  cashRateSummary,
  DEFAULT_CASH_RATE_SOURCE,
  RATE_COLLECTION,
  RATE_1M_SYMBOL,
} from './cashRateLeg';

describe('cashRateLeg helpers (series-only, F4)', () => {
  it('DEFAULT_CASH_RATE_SOURCE is the v2 1M CMT series (no flat kind/rate_pct)', () => {
    expect(DEFAULT_CASH_RATE_SOURCE).toEqual({
      collection: RATE_COLLECTION,
      symbol: RATE_1M_SYMBOL,
      unit: 'percent',
      compound: true,
    });
    expect('kind' in DEFAULT_CASH_RATE_SOURCE).toBe(false);
    expect('rate_pct' in DEFAULT_CASH_RATE_SOURCE).toBe(false);
  });

  it('cashRateApiSpec emits the series wire spec (collection/symbol/unit/compound)', () => {
    const leg = {
      type: 'cash_rate',
      cash_rate: {
        collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
      },
    };
    expect(cashRateApiSpec(leg)).toEqual({
      collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
    });
    // No flat leftovers on the wire.
    expect('kind' in cashRateApiSpec(leg)).toBe(false);
    expect('rate_pct' in cashRateApiSpec(leg)).toBe(false);
  });

  it('cashRateApiSpec falls back to the default RATE ref for a leg with no source', () => {
    expect(cashRateApiSpec({ type: 'cash_rate' })).toEqual({
      collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
    });
  });

  it('cashRateApiSpec honours compound:false and unit:fraction', () => {
    const leg = {
      type: 'cash_rate',
      cash_rate: {
        collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'fraction', compound: false,
      },
    };
    expect(cashRateApiSpec(leg)).toEqual({
      collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'fraction', compound: false,
    });
  });

  it('cashRateSummary describes the rate series reference', () => {
    expect(
      cashRateSummary({ cash_rate: { collection: 'RATE', symbol: 'RATE_US_CMT_1M' } }),
    ).toBe('rate RATE/RATE_US_CMT_1M');
  });
});
