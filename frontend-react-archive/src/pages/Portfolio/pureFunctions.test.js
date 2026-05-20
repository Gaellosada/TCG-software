import { describe, it, expect } from 'vitest';
import {
  normalizeTo100,
  toLongEquivalent,
  formatReturn,
  cellBgStyle,
  toLogReturn,
} from '../../utils/portfolioMath';

/* ── normalizeTo100 ── */

describe('normalizeTo100', () => {
  it('scales so first value becomes 100', () => {
    expect(normalizeTo100([50, 75, 100])).toEqual([100, 150, 200]);
  });

  it('returns identity when first value is already 100', () => {
    const result = normalizeTo100([100, 110, 90]);
    expect(result[0]).toBeCloseTo(100);
    expect(result[1]).toBeCloseTo(110);
    expect(result[2]).toBeCloseTo(90);
  });

  it('returns input unchanged for empty array', () => {
    expect(normalizeTo100([])).toEqual([]);
  });

  it('returns input unchanged for null/undefined', () => {
    expect(normalizeTo100(null)).toBeNull();
    expect(normalizeTo100(undefined)).toBeUndefined();
  });

  it('returns input unchanged when first value is 0', () => {
    expect(normalizeTo100([0, 10, 20])).toEqual([0, 10, 20]);
  });

  it('handles single-element array', () => {
    expect(normalizeTo100([200])).toEqual([100]);
  });
});

/* ── toLongEquivalent ── */

describe('toLongEquivalent', () => {
  it('un-inverts short leg equity: 2*initial - v', () => {
    // initial=1000, short equity went to 900 → long equivalent = 2*1000 - 900 = 1100
    expect(toLongEquivalent([1000, 900, 800])).toEqual([1000, 1100, 1200]);
  });

  it('is identity when values equal initial', () => {
    expect(toLongEquivalent([100, 100, 100])).toEqual([100, 100, 100]);
  });

  it('returns input unchanged for empty array', () => {
    expect(toLongEquivalent([])).toEqual([]);
  });

  it('returns input unchanged for null/undefined', () => {
    expect(toLongEquivalent(null)).toBeNull();
    expect(toLongEquivalent(undefined)).toBeUndefined();
  });

  it('is self-inverse (applying twice returns original)', () => {
    const original = [1000, 1050, 950, 1100];
    expect(toLongEquivalent(toLongEquivalent(original))).toEqual(original);
  });
});

/* ── formatReturn ── */

describe('formatReturn', () => {
  it('formats positive return with + sign', () => {
    expect(formatReturn(0.05)).toBe('+5.0%');
  });

  it('formats negative return without extra sign', () => {
    expect(formatReturn(-0.03)).toBe('-3.0%');
  });

  it('formats zero as 0.0%', () => {
    expect(formatReturn(0)).toBe('0.0%');
  });

  it('returns dash for null', () => {
    expect(formatReturn(null)).toBe('\u2013');
  });

  it('returns dash for undefined', () => {
    expect(formatReturn(undefined)).toBe('\u2013');
  });

  it('returns dash for NaN', () => {
    expect(formatReturn(NaN)).toBe('\u2013');
  });

  it('handles small returns with precision', () => {
    expect(formatReturn(0.001)).toBe('+0.1%');
  });
});

/* ── cellBgStyle ── */

describe('cellBgStyle', () => {
  it('returns green background for positive value', () => {
    const style = cellBgStyle(0.1, 0.2);
    expect(style.backgroundColor).toMatch(/rgba\(34, 197, 94,/);
  });

  it('returns red background for negative value', () => {
    const style = cellBgStyle(-0.1, 0.2);
    expect(style.backgroundColor).toMatch(/rgba\(239, 68, 68,/);
  });

  it('returns undefined for zero value', () => {
    expect(cellBgStyle(0, 0.2)).toBeUndefined();
  });

  it('returns undefined for null value', () => {
    expect(cellBgStyle(null, 0.2)).toBeUndefined();
  });

  it('returns undefined for NaN value', () => {
    expect(cellBgStyle(NaN, 0.2)).toBeUndefined();
  });

  it('returns undefined when maxAbs is 0', () => {
    expect(cellBgStyle(0.1, 0)).toBeUndefined();
  });

  it('opacity scales with ratio (value/maxAbs * 0.3)', () => {
    // value=0.1, maxAbs=0.2 → ratio=0.5 → opacity=0.15
    const style = cellBgStyle(0.1, 0.2);
    expect(style.backgroundColor).toBe('rgba(34, 197, 94, 0.15)');
  });

  it('caps opacity at 0.3 when value equals maxAbs', () => {
    const style = cellBgStyle(0.2, 0.2);
    expect(style.backgroundColor).toBe('rgba(34, 197, 94, 0.3)');
  });

  it('caps opacity at 0.3 when value exceeds maxAbs', () => {
    const style = cellBgStyle(0.5, 0.2);
    expect(style.backgroundColor).toBe('rgba(34, 197, 94, 0.3)');
  });
});

/* ── toLogReturn ── */

describe('toLogReturn', () => {
  it('converts normal return to log return: ln(1 + R)', () => {
    expect(toLogReturn(0)).toBeCloseTo(0);
    expect(toLogReturn(1)).toBeCloseTo(Math.log(2));
  });

  it('returns NaN for total loss (R = -1)', () => {
    expect(toLogReturn(-1)).toBeNaN();
  });

  it('returns NaN for worse-than-total loss (R < -1)', () => {
    expect(toLogReturn(-1.5)).toBeNaN();
  });

  it('passes through null', () => {
    expect(toLogReturn(null)).toBeNull();
  });

  it('passes through undefined', () => {
    expect(toLogReturn(undefined)).toBeUndefined();
  });

  it('passes through NaN', () => {
    expect(toLogReturn(NaN)).toBeNaN();
  });

  it('handles small positive return', () => {
    // For small R, ln(1+R) ≈ R
    expect(toLogReturn(0.01)).toBeCloseTo(Math.log(1.01));
  });

  it('handles negative return above -1', () => {
    expect(toLogReturn(-0.5)).toBeCloseTo(Math.log(0.5));
  });
});
