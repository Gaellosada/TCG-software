// @vitest-environment node
//
// Unit tests for the pure overlap-range logic shared by the active editor and
// the saved-list cache detection.

import { describe, it, expect } from 'vitest';
import { overlapRangeOf } from './resolvePortfolioRange';

describe('overlapRangeOf()', () => {
  it('returns null when no leg resolved a range', () => {
    expect(overlapRangeOf([])).toBe(null);
    expect(overlapRangeOf([{ start: null, end: null }, { start: null, end: null }])).toBe(null);
  });

  it('single leg → that leg’s range', () => {
    expect(overlapRangeOf([{ start: '2020-01-01', end: '2020-12-31' }]))
      .toEqual({ start: '2020-01-01', end: '2020-12-31' });
  });

  it('overlap = latest start → earliest end', () => {
    const out = overlapRangeOf([
      { start: '2010-01-01', end: '2022-12-31' },
      { start: '2015-06-01', end: '2020-03-15' },
      { start: '2012-01-01', end: '2025-01-01' },
    ]);
    expect(out).toEqual({ start: '2015-06-01', end: '2020-03-15' });
  });

  it('ignores null-range legs but uses the valid ones', () => {
    const out = overlapRangeOf([
      { start: null, end: null },
      { start: '2018-01-01', end: '2019-01-01' },
    ]);
    expect(out).toEqual({ start: '2018-01-01', end: '2019-01-01' });
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
    expect(out).toEqual({ start: '2020-06-30', end: '2020-06-30' });
  });
});
