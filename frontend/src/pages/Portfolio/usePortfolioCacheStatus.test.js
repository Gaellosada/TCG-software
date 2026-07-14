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
  // Fund-of-funds child-range resolver (composed active/row bodies). Default to
  // an empty map (children carry no inlined range in these key-status tests).
  resolveChildRanges: vi.fn(() => Promise.resolve(new Map())),
  // Single-source child-range accessor (used by the active + row body builders).
  // No inlined ranges in these key-status tests → an accessor that returns null.
  childRangeAccessorFor: vi.fn(() => Promise.resolve(() => null)),
}));
// A composed ROW resolves its OWN children by id through here (FE-B1 fix).
vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));

import { getPortfolioCacheStatus } from '../../api/portfolio';
import { getPortfolio } from '../../api/persistence';

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
    getPortfolio.mockReset();
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

  // FE-B1: a NON-active COMPOSED saved row must resolve its OWN children (not the
  // active editor's resolver) so its status body inlines the child spec — else it
  // is always falsely 'not-cached'.
  it('resolves a composed row\'s own child and reports it cached (not falsely not-cached)', async () => {
    const child = {
      id: 'c1', name: 'Child', kind: 'pure', category: 'RESEARCH', rebalance: 'none',
      legs: [{ label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 }],
    };
    getPortfolio.mockResolvedValue(child);
    const composedRow = {
      id: 'comp-1', rebalance: 'none',
      legs: [{ label: 'Block', type: 'portfolio', portfolioId: 'c1', portfolioName: 'Child', weight: 100 }],
    };
    // No active legs → only the composed row is queried; results[0] → comp-1.
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }] });

    const { result } = renderHook((props) => usePortfolioCacheStatus(props), {
      initialProps: baseProps({ legs: [], portfolios: [composedRow] }),
    });

    await waitFor(() => expect(getPortfolioCacheStatus).toHaveBeenCalled(), { timeout: 2000 });
    // The row's OWN child was fetched by id …
    expect(getPortfolio).toHaveBeenCalledWith('c1');
    // … and the row body inlines the resolved child (not a broken ref).
    const rowQuery = getPortfolioCacheStatus.mock.calls[0][0].find((b) => b.legs && b.legs.Block);
    expect(rowQuery.legs.Block.type).toBe('portfolio');
    expect(rowQuery.legs.Block.portfolio.legs.SPX).toBeTruthy();
    // … so the row shows cached, NOT falsely not-cached.
    await waitFor(() => expect(result.current.rowStatusById['comp-1']).toBe('cached'), { timeout: 2000 });
  });

  it('a pure row still resolves and reports its status', async () => {
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }] });
    const pureRow = {
      id: 'pure-1', rebalance: 'none',
      legs: [{ label: 'NDX', type: 'instrument', collection: 'INDEX', symbol: 'NDX', weight: 100 }],
    };
    const { result } = renderHook((props) => usePortfolioCacheStatus(props), {
      initialProps: baseProps({ legs: [], portfolios: [pureRow] }),
    });
    await waitFor(() => expect(result.current.rowStatusById['pure-1']).toBe('cached'), { timeout: 2000 });
    expect(getPortfolio).not.toHaveBeenCalled(); // pure rows fetch no children
  });
});
