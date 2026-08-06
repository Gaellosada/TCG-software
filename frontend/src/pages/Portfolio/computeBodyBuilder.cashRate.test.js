import { describe, it, expect } from 'vitest';
import { buildPortfolioComputeBody } from './computeBodyBuilder';

describe('buildPortfolioComputeBody — cash_rate legs (F4, v2 rate series)', () => {
  it('serializes a rate cash leg: type cash_rate, data_source v2, nested ref, weight', () => {
    const { body } = buildPortfolioComputeBody({
      legs: [
        { label: 'anchor', type: 'instrument', collection: 'INDEX', symbol: 'IND_SP_500', weight: 50 },
        {
          label: 'cash',
          type: 'cash_rate',
          weight: 100,
          dataSource: 'v2',
          cash_rate: {
            collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
          },
        },
      ],
      rebalance: 'none',
    });
    expect(body.legs.cash).toEqual({
      type: 'cash_rate',
      data_source: 'v2',
      cash_rate: {
        collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
      },
    });
    expect(body.weights.cash).toBe(100);
    // The instrument leg is untouched (byte-identical to before).
    expect(body.legs.anchor).toEqual({
      type: 'instrument',
      collection: 'INDEX',
      symbol: 'IND_SP_500',
    });
  });

  it('stamps data_source:v2 even when the leg omits dataSource (rates are v2-only)', () => {
    const { body } = buildPortfolioComputeBody({
      legs: [
        { label: 'anchor', type: 'instrument', collection: 'INDEX', symbol: 'X', weight: 1 },
        { label: 'cash', type: 'cash_rate', weight: 100 },
      ],
      rebalance: 'none',
    });
    expect(body.legs.cash).toEqual({
      type: 'cash_rate',
      data_source: 'v2',
      cash_rate: {
        collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
      },
    });
    expect(body.weights.cash).toBe(100);
  });

  it('does NOT emit a flat kind/rate_pct on the wire', () => {
    const { body } = buildPortfolioComputeBody({
      legs: [
        { label: 'anchor', type: 'instrument', collection: 'INDEX', symbol: 'X', weight: 1 },
        {
          label: 'cash',
          type: 'cash_rate',
          weight: 100,
          dataSource: 'v2',
          cash_rate: { collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true },
        },
      ],
      rebalance: 'none',
    });
    expect('kind' in body.legs.cash.cash_rate).toBe(false);
    expect('rate_pct' in body.legs.cash.cash_rate).toBe(false);
  });
});
