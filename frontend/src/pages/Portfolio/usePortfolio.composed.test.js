// @vitest-environment jsdom
//
// usePortfolio composed-leg additions:
//   - addPortfolioLeg stores a live reference (id + name + weight), NOT a
//     snapshot of the child spec.
//   - handleCalculate resolves the child's CURRENT spec via getPortfolio and
//     inlines it under ``portfolio`` in the compute body (design §4).
//   - an unresolvable child blocks compute with a clear error (no compute call).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import usePortfolio from './usePortfolio';

vi.mock('../../api/portfolio', () => ({
  computePortfolio: vi.fn(() => Promise.resolve({ portfolio_equity: [1, 1.1], dates: ['2020-01-01', '2020-01-02'] })),
}));
vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
}));
vi.mock('../../api/options', () => ({
  getOptionCoverage: vi.fn(() => Promise.resolve({ root: 'X', start: '2005-12-01', end: '2025-06-30' })),
}));
vi.mock('../Signals/requestBuilder', () => ({
  buildComputeRequestBody: vi.fn(() => ({ body: {}, missing: [] })),
}));
vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));
vi.mock('../../api/persistence', () => ({
  getPortfolio: vi.fn(),
}));

import { computePortfolio } from '../../api/portfolio';
import { getPortfolio } from '../../api/persistence';

const CHILD = {
  id: 'c1', name: 'Child', kind: 'pure', category: 'RESEARCH', rebalance: 'none',
  legs: [{ label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 }],
};

describe('usePortfolio — composed portfolio legs', () => {
  beforeEach(() => {
    localStorage.clear();
    computePortfolio.mockClear();
    getPortfolio.mockReset();
    getPortfolio.mockResolvedValue(CHILD);
  });

  it('addPortfolioLeg stores a live reference (id + name), not the child spec', () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => result.current.addPortfolioLeg(CHILD));
    const leg = result.current.legs[0];
    expect(leg.type).toBe('portfolio');
    expect(leg.portfolioId).toBe('c1');
    expect(leg.portfolioName).toBe('Child');
    expect(leg.weight).toBe(100);
    // No inlined child spec on the leg — it is resolved fresh at compute.
    expect(leg).not.toHaveProperty('legs');
    expect(leg).not.toHaveProperty('portfolio');
  });

  it('handleCalculate resolves the child fresh and inlines it under `portfolio`', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => result.current.addPortfolioLeg(CHILD));
    act(() => { result.current.setStartDate('2020-01-01'); result.current.setEndDate('2020-12-31'); });

    await act(async () => { await result.current.handleCalculate(); });

    expect(getPortfolio).toHaveBeenCalledWith('c1');
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    const arg = computePortfolio.mock.calls[0][0];
    expect(arg.legs.Child).toEqual({
      type: 'portfolio',
      portfolio_id: 'c1',
      portfolio: {
        legs: { SPX: { type: 'instrument', collection: 'INDEX', symbol: 'SPX' } },
        weights: { SPX: 100 },
        rebalance: 'none',
        return_type: 'normal',
      },
    });
    expect(arg.weights.Child).toBe(100);
    // Cache toggle DEFAULT ON → use_cache flag true on the request.
    expect(arg.useCache).toBe(true);
  });

  it('sends use_cache reflecting the Settings toggle (localStorage "false" → false)', async () => {
    localStorage.setItem('tcg-portfolio-cache-enabled', 'false');
    const { result } = renderHook(() => usePortfolio());
    act(() => result.current.addPortfolioLeg(CHILD));
    act(() => { result.current.setStartDate('2020-01-01'); result.current.setEndDate('2020-12-31'); });

    await act(async () => { await result.current.handleCalculate(); });

    expect(computePortfolio).toHaveBeenCalledTimes(1);
    expect(computePortfolio.mock.calls[0][0].useCache).toBe(false);
  });

  it('blocks compute with a clear error when the child cannot be resolved', async () => {
    getPortfolio.mockRejectedValue(new Error('404'));
    const { result } = renderHook(() => usePortfolio());
    act(() => result.current.addPortfolioLeg(CHILD));
    act(() => { result.current.setStartDate('2020-01-01'); result.current.setEndDate('2020-12-31'); });

    await act(async () => { await result.current.handleCalculate(); });

    expect(computePortfolio).not.toHaveBeenCalled();
    expect(result.current.error).toMatch(/could not be resolved|can't be resolved|deleted, archived, or empty/i);
  });
});
