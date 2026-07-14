// @vitest-environment jsdom
//
// Unit tests for getRiskFreeRateFraction() and exported constants.
// These tests exercise the single percent→fraction conversion site (Sign 7).

import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  getRiskFreeRateFraction,
  isPortfolioCacheEnabled,
  getSlippageBps,
  getFeesBps,
  PORTFOLIO_CACHE_KEY,
  DEFAULT_RISK_FREE_RATE_PCT,
  DEFAULT_RISK_FREE_RATE_FRACTION,
  DEFAULT_SLIPPAGE_BPS,
  DEFAULT_FEES_BPS,
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

describe('getSlippageBps() / getFeesBps() — bps, default 0, guarded', () => {
  it('default to 0 when the key is absent', () => {
    expect(getSlippageBps()).toBe(0);
    expect(getFeesBps()).toBe(0);
    expect(DEFAULT_SLIPPAGE_BPS).toBe(0);
    expect(DEFAULT_FEES_BPS).toBe(0);
  });

  it('parse a stored bps value AS-IS (no unit conversion)', () => {
    localStorage.setItem('tcg-slippage-bps', '5');
    localStorage.setItem('tcg-fees-bps', '2.5');
    expect(getSlippageBps()).toBe(5);
    expect(getFeesBps()).toBe(2.5);
  });

  it('treat "0" as 0', () => {
    localStorage.setItem('tcg-slippage-bps', '0');
    localStorage.setItem('tcg-fees-bps', '0');
    expect(getSlippageBps()).toBe(0);
    expect(getFeesBps()).toBe(0);
  });

  it('fall back to 0 on empty / non-numeric / negative', () => {
    for (const bad of ['', 'abc', '-1', '-0.01']) {
      localStorage.setItem('tcg-slippage-bps', bad);
      localStorage.setItem('tcg-fees-bps', bad);
      expect(getSlippageBps()).toBe(0);
      expect(getFeesBps()).toBe(0);
    }
  });

  it('return 0 (never throw) when localStorage.getItem throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(getSlippageBps()).toBe(0);
    expect(getFeesBps()).toBe(0);
    spy.mockRestore();
  });

  it('round-trip a value written by the Settings input', () => {
    localStorage.setItem('tcg-slippage-bps', '7.5');
    expect(getSlippageBps()).toBe(7.5);
  });
});

describe('isPortfolioCacheEnabled() — DEFAULT ON', () => {
  it('defaults to TRUE when the key is absent', () => {
    expect(isPortfolioCacheEnabled()).toBe(true);
  });

  it('is false ONLY for the exact string "false"', () => {
    localStorage.setItem(PORTFOLIO_CACHE_KEY, 'false');
    expect(isPortfolioCacheEnabled()).toBe(false);
  });

  it('is true for "true"', () => {
    localStorage.setItem(PORTFOLIO_CACHE_KEY, 'true');
    expect(isPortfolioCacheEnabled()).toBe(true);
  });

  it('is true for any non-"false" value (e.g. "1", "FALSE", "no", "")', () => {
    for (const v of ['1', 'FALSE', 'no', '']) {
      localStorage.setItem(PORTFOLIO_CACHE_KEY, v);
      expect(isPortfolioCacheEnabled()).toBe(true);
    }
  });

  it('round-trips String(true)/String(false) written by the Settings toggle', () => {
    localStorage.setItem(PORTFOLIO_CACHE_KEY, String(false));
    expect(isPortfolioCacheEnabled()).toBe(false);
    localStorage.setItem(PORTFOLIO_CACHE_KEY, String(true));
    expect(isPortfolioCacheEnabled()).toBe(true);
  });

  it('returns true (never throws) when localStorage.getItem throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(isPortfolioCacheEnabled()).toBe(true);
    spy.mockRestore();
  });
});
