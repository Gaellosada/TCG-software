// @vitest-environment jsdom
//
// Unit tests for getRiskFreeRateFraction() and exported constants.
// These tests exercise the single percent→fraction conversion site (Sign 7).

import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  getRiskFreeRateFraction,
  isPortfolioCacheEnabled,
  PORTFOLIO_CACHE_KEY,
  DEFAULT_RISK_FREE_RATE_PCT,
  DEFAULT_RISK_FREE_RATE_FRACTION,
} from './userSettings';

beforeEach(() => {
  localStorage.clear();
});

describe('userSettings — exported constants', () => {
  it('DEFAULT_RISK_FREE_RATE_PCT is 4.0', () => {
    expect(DEFAULT_RISK_FREE_RATE_PCT).toBe(4.0);
  });

  it('DEFAULT_RISK_FREE_RATE_FRACTION is 0.04', () => {
    expect(DEFAULT_RISK_FREE_RATE_FRACTION).toBe(0.04);
  });
});

describe('getRiskFreeRateFraction()', () => {
  // TC4.1
  it('returns 0.04 when localStorage is empty', () => {
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.04, 10);
  });

  it('returns 0.04 when key is absent', () => {
    localStorage.removeItem('tcg-risk-free-rate');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.04, 10);
  });

  // TC4.2
  it('parses "4.5" percent and returns 0.045', () => {
    localStorage.setItem('tcg-risk-free-rate', '4.5');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.045, 10);
  });

  it('parses "5" percent and returns 0.05', () => {
    localStorage.setItem('tcg-risk-free-rate', '5');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.05, 10);
  });

  it('parses "0" percent and returns 0', () => {
    localStorage.setItem('tcg-risk-free-rate', '0');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0, 10);
  });

  // TC4.3
  it('returns 0.04 when stored value is "abc" (non-numeric)', () => {
    localStorage.setItem('tcg-risk-free-rate', 'abc');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.04, 10);
  });

  it('returns 0.04 when stored value is empty string', () => {
    localStorage.setItem('tcg-risk-free-rate', '');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.04, 10);
  });

  // TC4.4
  it('returns 0.04 when stored value is "-1" (negative)', () => {
    localStorage.setItem('tcg-risk-free-rate', '-1');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.04, 10);
  });

  it('returns 0.04 when stored value is "-0.01" (negative)', () => {
    localStorage.setItem('tcg-risk-free-rate', '-0.01');
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.04, 10);
  });

  // TC4.5
  it('returns 0.04 when localStorage.getItem throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(getRiskFreeRateFraction()).toBeCloseTo(0.04, 10);
    spy.mockRestore();
  });
});

describe('isPortfolioCacheEnabled()', () => {
  it('defaults to false when the key is absent', () => {
    expect(isPortfolioCacheEnabled()).toBe(false);
  });

  it('is true only for the exact string "true"', () => {
    localStorage.setItem(PORTFOLIO_CACHE_KEY, 'true');
    expect(isPortfolioCacheEnabled()).toBe(true);
  });

  it('is false for "false"', () => {
    localStorage.setItem(PORTFOLIO_CACHE_KEY, 'false');
    expect(isPortfolioCacheEnabled()).toBe(false);
  });

  it('is false for any non-"true" value (e.g. "1", "TRUE", "yes")', () => {
    for (const v of ['1', 'TRUE', 'yes', '']) {
      localStorage.setItem(PORTFOLIO_CACHE_KEY, v);
      expect(isPortfolioCacheEnabled()).toBe(false);
    }
  });

  it('round-trips String(true)/String(false) written by the Settings toggle', () => {
    localStorage.setItem(PORTFOLIO_CACHE_KEY, String(true));
    expect(isPortfolioCacheEnabled()).toBe(true);
    localStorage.setItem(PORTFOLIO_CACHE_KEY, String(false));
    expect(isPortfolioCacheEnabled()).toBe(false);
  });

  it('returns false (never throws) when localStorage.getItem throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(isPortfolioCacheEnabled()).toBe(false);
    spy.mockRestore();
  });
});
