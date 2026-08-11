// @vitest-environment jsdom
//
// PROGRESSIVE cache-status labels (latency fix). The saved-list cache label must
// populate PER ROW, as soon as THAT row's range resolves — NOT gated behind the
// slowest row. This asserts a fast row's status is committed while a slow row is
// still `checking`, and that a superseded run's late results are dropped.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import usePortfolioCacheStatus from './usePortfolioCacheStatus';

// A shared deferred registry so a test can resolve each row's range on demand,
// simulating staggered dwh completion. Hoisted so the vi.mock factory can see it.
const H = vi.hoisted(() => {
  const rangeDeferreds = new Map(); // symbol -> { promise, resolve }
  function rangeDeferred(symbol) {
    if (!rangeDeferreds.has(symbol)) {
      let resolve;
      const promise = new Promise((r) => { resolve = r; });
      rangeDeferreds.set(symbol, { promise, resolve });
    }
    return rangeDeferreds.get(symbol);
  }
  const statusDeferreds = []; // one per getPortfolioCacheStatus call
  return { rangeDeferreds, rangeDeferred, statusDeferreds };
});

vi.mock('./resolvePortfolioRange', () => ({
  // Range resolves only when the test resolves THIS row's deferred (staggered).
  resolvePortfolioRange: vi.fn((rowLegs) => {
    const symbol = rowLegs[0].symbol;
    return H.rangeDeferred(symbol).promise.then(() => ({
      ranges: {}, overlapRange: { start: '2020-01-01', end: '2020-12-31' },
    }));
  }),
  childRangeAccessorFor: vi.fn(() => Promise.resolve(() => null)),
  getChildPortfolioId: (leg) => (leg && (leg.portfolioId || leg.portfolio_id)) || null,
}));

vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));

vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));

vi.mock('../../api/portfolio', () => ({
  getPortfolioCacheStatus: vi.fn(() => Promise.resolve({ results: [{ cached: true }] })),
}));

import { getPortfolioCacheStatus } from '../../api/portfolio';
import { resolvePortfolioRange } from './resolvePortfolioRange';

function baseProps(overrides = {}) {
  return {
    cacheEnabled: true,
    legs: [],                 // no active config → isolate saved-row progression
    rebalance: 'none',
    startDate: '',
    endDate: '',
    overlapRange: null,
    resolvePortfolio: () => null,
    portfolios: [],
    activeId: null,
    refreshKey: 0,
    ...overrides,
  };
}

function row(id, symbol) {
  return {
    id,
    rebalance: 'none',
    legs: [{ label: symbol, type: 'instrument', collection: 'INDEX', symbol, weight: 100 }],
  };
}

describe('usePortfolioCacheStatus — progressive per-row labels', () => {
  beforeEach(() => {
    H.rangeDeferreds.clear();
    H.statusDeferreds.length = 0;
    resolvePortfolioRange.mockClear();
    getPortfolioCacheStatus.mockReset();
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }] });
  });

  it('commits a fast row while a slow row is still checking (no all-or-nothing barrier)', async () => {
    const rows = [row('row-fast', 'FAST'), row('row-slow', 'SLOW')];
    const { result } = renderHook((p) => usePortfolioCacheStatus(p), {
      initialProps: baseProps({ portfolios: rows }),
    });

    // Both rows seed to `checking` and, past the debounce, both start resolving.
    await waitFor(() => {
      expect(resolvePortfolioRange).toHaveBeenCalledTimes(2);
    }, { timeout: 2000 });
    expect(result.current.rowStatusById['row-fast']).toBe('checking');
    expect(result.current.rowStatusById['row-slow']).toBe('checking');

    // Resolve ONLY the fast row's range → its status must commit while the slow
    // row's range is still pending. On the OLD all-or-nothing hook this never
    // happens (the single batched call waits for BOTH ranges) → this times out.
    await act(async () => { H.rangeDeferred('FAST').resolve(); });
    await waitFor(() => {
      expect(result.current.rowStatusById['row-fast']).toBe('cached');
    }, { timeout: 2000 });
    // The slow row is STILL checking — proof the barrier is gone.
    expect(result.current.rowStatusById['row-slow']).toBe('checking');

    // Now release the slow row → it too resolves.
    await act(async () => { H.rangeDeferred('SLOW').resolve(); });
    await waitFor(() => {
      expect(result.current.rowStatusById['row-slow']).toBe('cached');
    }, { timeout: 2000 });
  });

  it('drops a superseded run\'s late per-row result (stale-run guard)', async () => {
    // Run 1: probe returns not-cached but resolves LATE (deferred by the test).
    let firstStatusResolve;
    getPortfolioCacheStatus.mockImplementationOnce(() => new Promise((r) => {
      firstStatusResolve = () => r({ results: [{ cached: false }] });
    }));

    const rows = [row('row-1', 'S1')];
    const { result, rerender } = renderHook((p) => usePortfolioCacheStatus(p), {
      initialProps: baseProps({ portfolios: rows }),
    });

    // Let run 1 reach its status probe: resolve the range so the row body builds.
    await waitFor(() => expect(resolvePortfolioRange).toHaveBeenCalled(), { timeout: 2000 });
    await act(async () => { H.rangeDeferred('S1').resolve(); });
    await waitFor(() => expect(getPortfolioCacheStatus).toHaveBeenCalledTimes(1), { timeout: 2000 });

    // Supersede run 1 with run 2 (refreshKey bump). Run 2's probe returns cached.
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }] });
    act(() => rerender(baseProps({ portfolios: rows, refreshKey: 1 })));
    // Run 2 re-resolves the same range (fresh deferred after clear? no — reuse):
    // the S1 deferred is already resolved, so run 2 proceeds to its probe.
    await waitFor(() => expect(result.current.rowStatusById['row-1']).toBe('cached'), { timeout: 2000 });

    // NOW deliver run 1's stale not-cached result. It must be DROPPED — the row
    // stays `cached` (run 2's truth), never flips back.
    await act(async () => { firstStatusResolve(); await Promise.resolve(); });
    expect(result.current.rowStatusById['row-1']).toBe('cached');
  });
});
