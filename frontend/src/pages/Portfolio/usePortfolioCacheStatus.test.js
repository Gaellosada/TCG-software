// @vitest-environment jsdom
//
// usePortfolioCacheStatus — builds compute bodies for the active config + the
// visible saved rows and asks the backend (ONE batched call) whether each is
// cached. Editing the active config re-probes (invalidation is visible).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import usePortfolioCacheStatus from './usePortfolioCacheStatus';

vi.mock('../../api/portfolio', () => ({
  getPortfolioCacheStatus: vi.fn(() => Promise.resolve({ results: [] })),
}));
vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));
// Rows resolve a range through this; stub it to a fixed window.
vi.mock('./resolvePortfolioRange', () => ({
  resolvePortfolioRange: vi.fn(() => Promise.resolve({
    ranges: {}, overlapRange: { start: '2020-01-01', end: '2020-12-31' },
  })),
}));

import { getPortfolioCacheStatus } from '../../api/portfolio';

const ACTIVE_LEG = { id: 1, label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 };

function baseProps(overrides = {}) {
  return {
    cacheEnabled: true,
    legs: [ACTIVE_LEG],
    rebalance: 'none',
    startDate: '2020-01-01',
    endDate: '2020-12-31',
    overlapRange: { start: '2020-01-01', end: '2020-12-31' },
    resolvePortfolio: () => null,
    portfolios: [],
    activeId: null,
    refreshKey: 0,
    ...overrides,
  };
}

describe('usePortfolioCacheStatus', () => {
  beforeEach(() => {
    getPortfolioCacheStatus.mockReset();
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }] });
  });

  it('probes the active config and reports activeCached from the response', async () => {
    const { result } = renderHook((props) => usePortfolioCacheStatus(props), {
      initialProps: baseProps(),
    });
    await waitFor(() => expect(getPortfolioCacheStatus).toHaveBeenCalled(), { timeout: 2000 });
    // The batched body[0] is the active config's compute body (instrument leg).
    const queries = getPortfolioCacheStatus.mock.calls[0][0];
    expect(queries[0].legs.SPX).toEqual({ type: 'instrument', collection: 'INDEX', symbol: 'SPX' });
    await waitFor(() => expect(result.current.activeCached).toBe(true), { timeout: 2000 });
  });

  it('batches the active config AND saved rows into ONE call', async () => {
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }, { cached: false }] });
    const row = {
      id: 'row-1', rebalance: 'none',
      legs: [{ label: 'NDX', type: 'instrument', collection: 'INDEX', symbol: 'NDX', weight: 100 }],
    };
    const { result } = renderHook((props) => usePortfolioCacheStatus(props), {
      initialProps: baseProps({ portfolios: [row] }),
    });
    await waitFor(() => expect(getPortfolioCacheStatus).toHaveBeenCalledTimes(1), { timeout: 2000 });
    expect(getPortfolioCacheStatus.mock.calls[0][0]).toHaveLength(2); // active + 1 row, one call
    await waitFor(() => {
      expect(result.current.activeCached).toBe(true);
      expect(result.current.rowStatusById['row-1']).toBe('not-cached');
    }, { timeout: 2000 });
  });

  it('re-probes when the active config changes (edit → flips)', async () => {
    getPortfolioCacheStatus.mockResolvedValueOnce({ results: [{ cached: true }] });
    const { result, rerender } = renderHook((props) => usePortfolioCacheStatus(props), {
      initialProps: baseProps(),
    });
    await waitFor(() => expect(result.current.activeCached).toBe(true), { timeout: 2000 });

    // Edit the config → the next probe reports not-cached.
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: false }] });
    act(() => rerender(baseProps({ legs: [{ ...ACTIVE_LEG, weight: 50 }] })));
    await waitFor(() => expect(result.current.activeCached).toBe(false), { timeout: 2000 });
    expect(getPortfolioCacheStatus.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('does nothing and returns nulls when caching is disabled', async () => {
    const { result } = renderHook((props) => usePortfolioCacheStatus(props), {
      initialProps: baseProps({ cacheEnabled: false }),
    });
    // Give the debounce a chance — it must NOT fire.
    await new Promise((r) => { setTimeout(r, 400); });
    expect(getPortfolioCacheStatus).not.toHaveBeenCalled();
    expect(result.current.activeCached).toBeNull();
    expect(result.current.rowStatusById).toEqual({});
  });
});
