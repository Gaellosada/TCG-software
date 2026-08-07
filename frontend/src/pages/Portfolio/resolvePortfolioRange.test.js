// @vitest-environment node
//
// Unit tests for the pure overlap-range logic shared by the active editor and
// the saved-list cache detection.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { overlapRangeOf } from './resolvePortfolioRange';

vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));
vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(),
  getContinuousSeries: vi.fn(),
}));

import { getPortfolio } from '../../api/persistence';
import { getInstrumentPrices } from '../../api/data';
import { childPortfolioIds, childRangeAccessorFor } from './resolvePortfolioRange';

describe('overlapRangeOf()', () => {
  it('returns null when no leg resolved a range', () => {
    expect(overlapRangeOf([])).toBe(null);
    expect(overlapRangeOf([{ start: null, end: null }, { start: null, end: null }])).toBe(null);
  });

  it('single leg → that leg’s range', () => {
    // No cadence info on the leg → recommendedStart defaults to raw start,
    // segments to []. start/end unchanged (additive fields only).
    expect(overlapRangeOf([{ start: '2020-01-01', end: '2020-12-31' }]))
      .toEqual({
        start: '2020-01-01', end: '2020-12-31',
        recommendedStart: '2020-01-01', segments: [],
      });
  });

  it('overlap = latest start → earliest end', () => {
    const out = overlapRangeOf([
      { start: '2010-01-01', end: '2022-12-31' },
      { start: '2015-06-01', end: '2020-03-15' },
      { start: '2012-01-01', end: '2025-01-01' },
    ]);
    expect(out).toEqual({
      start: '2015-06-01', end: '2020-03-15',
      recommendedStart: '2015-06-01', segments: [],
    });
  });

  it('ignores null-range legs but uses the valid ones', () => {
    const out = overlapRangeOf([
      { start: null, end: null },
      { start: '2018-01-01', end: '2019-01-01' },
    ]);
    expect(out).toEqual({
      start: '2018-01-01', end: '2019-01-01',
      recommendedStart: '2018-01-01', segments: [],
    });
  });

  it('disjoint ranges (start > end) → null', () => {
    const out = overlapRangeOf([
      { start: '2020-01-01', end: '2020-06-30' },
      { start: '2021-01-01', end: '2021-06-30' },
    ]);
    expect(out).toBe(null);
  });

  it('touching endpoints (start === end) → single-day overlap (not null)', () => {
    const out = overlapRangeOf([
      { start: '2019-01-01', end: '2020-06-30' },
      { start: '2020-06-30', end: '2021-01-01' },
    ]);
    expect(out).toEqual({
      start: '2020-06-30', end: '2020-06-30',
      recommendedStart: '2020-06-30', segments: [],
    });
  });

  it('folds the LATEST per-leg recommendedStart and carries the cliff band', () => {
    // An option leg with a quarterly→monthly cliff, plus a plain leg that only
    // provides raw start. The recommendation folds to the latest recommendation
    // (the option leg's monthly floor), clamped into the overlap; the >1-segment
    // band is carried for the slider to shade.
    const segments = [
      { start: '2010-06-07', end: '2016-04-30', cadence: 'quarterly' },
      { start: '2016-05-01', end: '2026-07-27', cadence: 'monthly' },
    ];
    const out = overlapRangeOf([
      { start: '2005-01-01', end: '2026-12-31' }, // plain leg, no cadence
      { start: '2010-06-07', end: '2026-07-27', recommendedStart: '2016-05-01', segments },
    ]);
    expect(out.start).toBe('2010-06-07'); // raw overlap floor (latest raw start)
    expect(out.end).toBe('2026-07-27');
    expect(out.recommendedStart).toBe('2016-05-01'); // monthly-era default
    expect(out.segments).toEqual(segments); // band carried for shading
  });

  it('clamps a recommendation later than the overlap end back to the end', () => {
    const out = overlapRangeOf([
      { start: '2010-01-01', end: '2015-01-01', recommendedStart: '2020-01-01', segments: [] },
    ]);
    expect(out.recommendedStart).toBe('2015-01-01');
  });
});

describe('childPortfolioIds() / childRangeAccessorFor() — non-empty map', () => {
  // Minimal queryClient stub: no real caching needed, just runs the queryFn.
  const fakeQueryClient = () => ({ fetchQuery: ({ queryFn }) => queryFn() });

  beforeEach(() => {
    getPortfolio.mockReset();
    getInstrumentPrices.mockReset();
  });

  it('extracts referenced child ids (camelCase/snake_case, deduped) …', () => {
    const legs = [
      { id: 1, type: 'portfolio', portfolioId: 'child-a', weight: 50 },
      { id: 2, type: 'portfolio', portfolio_id: 'child-b', weight: 50 },
      { id: 3, type: 'portfolio', portfolioId: 'child-a', weight: 0 }, // dup of child-a
      { id: 4, type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 0 }, // not a portfolio leg
    ];
    expect(childPortfolioIds(legs)).toEqual(['child-a', 'child-b']);
  });

  it('… and the accessor resolves each child\'s OWN range, null for an unknown id', async () => {
    const legs = [
      { id: 1, type: 'portfolio', portfolioId: 'child-a', weight: 50 },
      { id: 2, type: 'portfolio', portfolio_id: 'child-b', weight: 50 },
    ];
    getPortfolio.mockImplementation((id) => Promise.resolve({
      id,
      rebalance: 'none',
      legs: [{
        label: 'L', type: 'instrument', collection: 'INDEX',
        symbol: id === 'child-a' ? 'A' : 'B', weight: 100,
      }],
    }));
    getInstrumentPrices.mockImplementation((_collection, symbol) => {
      if (symbol === 'A') return Promise.resolve({ dates: [20200101, 20200630] });
      if (symbol === 'B') return Promise.resolve({ dates: [20190101, 20201231] });
      return Promise.resolve({ dates: [] });
    });

    const accessor = await childRangeAccessorFor(legs, { queryClient: fakeQueryClient() });

    expect(accessor('child-a')).toEqual({ start: '2020-01-01', end: '2020-06-30' });
    expect(accessor('child-b')).toEqual({ start: '2019-01-01', end: '2020-12-31' });
    expect(accessor('unknown-id')).toBeNull();
  });
});
