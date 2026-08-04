import { describe, it, expect } from 'vitest';
import { buildPortfolioComputeBody } from './computeBodyBuilder';

describe('buildPortfolioComputeBody — cash_rate legs (F4)', () => {
  it('serializes a flat cash leg with its rate source + weight', () => {
    const { body } = buildPortfolioComputeBody({
      legs: [
        { label: 'anchor', type: 'instrument', collection: 'INDEX', symbol: 'IND_SP_500', weight: 50 },
        {
          label: 'cash',
          type: 'cash_rate',
          weight: 100,
          cash_rate: { kind: 'flat', rate_pct: 1.0, compound: true },
        },
      ],
      rebalance: 'none',
    });
    expect(body.legs.cash).toEqual({
      type: 'cash_rate',
      cash_rate: { kind: 'flat', rate_pct: 1.0, compound: true },
    });
    expect(body.weights.cash).toBe(100);
    // The instrument leg is untouched (byte-identical to before).
    expect(body.legs.anchor).toEqual({
      type: 'instrument',
      collection: 'INDEX',
      symbol: 'IND_SP_500',
    });
  });

  it('serializes a series cash leg with its instrument reference', () => {
    const { body } = buildPortfolioComputeBody({
      legs: [
        { label: 'anchor', type: 'instrument', collection: 'INDEX', symbol: 'IND_VIX', weight: 1 },
        {
          label: 'cash',
          type: 'cash_rate',
          weight: 100,
          cash_rate: {
            kind: 'series',
            collection: 'FUT_RATE',
            symbol: 'RATE_USD',
            unit: 'percent',
            rate_pct: 2.0,
            compound: true,
          },
        },
      ],
      rebalance: 'none',
    });
    expect(body.legs.cash.type).toBe('cash_rate');
    expect(body.legs.cash.cash_rate).toEqual({
      kind: 'series',
      collection: 'FUT_RATE',
      symbol: 'RATE_USD',
      unit: 'percent',
      rate_pct: 2.0,
      compound: true,
    });
  });

  it('a cash leg without an explicit source falls back to flat 1%/yr', () => {
    const { body } = buildPortfolioComputeBody({
      legs: [
        { label: 'anchor', type: 'instrument', collection: 'INDEX', symbol: 'X', weight: 1 },
        { label: 'cash', type: 'cash_rate', weight: 100 },
      ],
      rebalance: 'none',
    });
    expect(body.legs.cash.cash_rate).toEqual({ kind: 'flat', rate_pct: 1.0, compound: true });
  });
});
