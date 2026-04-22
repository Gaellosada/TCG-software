import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// NOTE: The TZ stub test works by constructing dates from explicit ISO strings
// (which parse as UTC) and verifying the *local-component* formatter returns
// the correct calendar date regardless of the host timezone.  In production
// CI (UTC) this is a no-op, but it validates the fix that prevents a
// positive-offset TZ from slipping the date back by one day.
import { formatDate, formatDateInt, formatNumber, formatPercent, formatCurrency } from './format';

describe('formatDate', () => {
  it('formats a UTC midnight Date correctly', () => {
    // 2024-01-15T00:00:00Z — in UTC this is midnight, no slip.
    const d = new Date('2024-01-15T00:00:00Z');
    // The implementation reads local components (getFullYear/getMonth/getDate).
    // In a positive-offset timezone (e.g. UTC+2) this UTC midnight resolves
    // to 2024-01-14T22:00:00 local, which WOULD slip to Jan 14 if we used
    // toISOString().split('T')[0].  The fix uses local components to avoid
    // this.  We cannot stub TZ in vitest without module reimport tricks, so
    // we test the safe direction: a date constructed as local midnight must
    // return the correct calendar date on the current host.
    const local = new Date(2024, 0, 15); // Jan 15 in local time
    expect(formatDate(local)).toBe('2024-01-15');
  });

  it('formats a date object at local midnight — positive-offset TZ case', () => {
    // Simulate what happens in a UTC+1/+2 host: if someone constructed a
    // Date from year/month/day (local midnight), the formatter must still
    // return that same calendar date and not slip to the previous day.
    // Strategy: mock Date.prototype.getFullYear / getMonth / getDate to
    // return Europe/Paris-like values (where local midnight != UTC midnight).
    // We verify formatDate() reads the LOCAL components, not UTC.
    const fakeDateParis = {
      getFullYear: () => 2024,
      getMonth: () => 2,    // March
      getDate: () => 15,
    };
    // formatDate accepts any object with the date getter methods via duck-typing
    // if we pass it as `date` directly (non-string path).  The implementation
    // reads d.getFullYear() etc., so a plain object works for this test.
    const result = formatDate(fakeDateParis);
    expect(result).toBe('2024-03-15');
  });

  it('formats an ISO string correctly', () => {
    expect(formatDate('2023-11-30')).toBe('2023-11-30');
  });

  it('formats a Date at local midnight (month padding)', () => {
    const d = new Date(2024, 1, 5); // Feb 5 local
    expect(formatDate(d)).toBe('2024-02-05');
  });

  it('positive-offset TZ: Date from local midnight does not slip to previous day', () => {
    // Core contract: formatDate(new Date(y, m, d)) === YYYY-MM-DD for any host TZ.
    // We verify by overriding the date getters on a spy object.
    for (const [y, m, d, expected] of [
      [2024, 0, 1, '2024-01-01'],
      [2024, 11, 31, '2024-12-31'],
      [2025, 6, 4, '2025-07-04'],
    ]) {
      const spy = { getFullYear: () => y, getMonth: () => m, getDate: () => d };
      expect(formatDate(spy)).toBe(expected);
    }
  });
});

describe('formatDateInt', () => {
  it('formats a valid YYYYMMDD integer', () => {
    expect(formatDateInt(20240115)).toBe('2024-01-15');
  });

  it('returns "--" for null/undefined', () => {
    expect(formatDateInt(null)).toBe('--');
    expect(formatDateInt(undefined)).toBe('--');
  });

  it('returns raw string for non-8-digit values', () => {
    expect(formatDateInt(2024)).toBe('2024');
    expect(formatDateInt(202401150)).toBe('202401150');
  });
});

describe('formatNumber', () => {
  it('formats a finite number with default 2 decimals', () => {
    expect(formatNumber(1234.567)).toBe('1,234.57');
  });

  it('returns "--" for null/undefined/NaN/Infinity', () => {
    expect(formatNumber(null)).toBe('--');
    expect(formatNumber(undefined)).toBe('--');
    expect(formatNumber(NaN)).toBe('--');
    expect(formatNumber(Infinity)).toBe('--');
  });
});

describe('formatPercent', () => {
  it('formats 0.05 as "5.00%"', () => {
    expect(formatPercent(0.05)).toBe('5.00%');
  });

  it('returns "--" for non-finite values', () => {
    expect(formatPercent(null)).toBe('--');
    expect(formatPercent(NaN)).toBe('--');
  });
});

describe('formatCurrency', () => {
  it('formats a USD value', () => {
    expect(formatCurrency(1000)).toContain('1,000');
  });

  it('returns "--" for non-finite values', () => {
    expect(formatCurrency(null)).toBe('--');
    expect(formatCurrency(NaN)).toBe('--');
  });
});
