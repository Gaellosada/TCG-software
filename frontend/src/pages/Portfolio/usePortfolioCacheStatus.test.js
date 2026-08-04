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
  // Single-source child-portfolio-id predicate — computeBodyBuilder.js imports
  // this directly, so a full-module mock here must re-export it too (real impl,
  // it's pure) or the builder's composed-leg branch throws on the undefined call.
  getChildPortfolioId: (leg) => (leg && (leg.portfolioId || leg.portfolio_id)) || null,
}));
// A composed ROW resolves its OWN children by id through here (FE-B1 fix).
vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));

import { getPortfolioCacheStatus } from '../../api/portfolio';
import { getPortfolio } from '../../api/persistence';
import { resolvePortfolioRange } from './resolvePortfolioRange';

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

  it('seeds a cadence-cliff row probe from recommendedStart, not raw start (cache-key parity)', async () => {
    // A saved (non-active) row whose overlap has a cadence cliff: Compute keys the
    // result under recommendedStart, so the status probe must key the SAME start —
    // else the row shows a false "not-cached" badge.
    resolvePortfolioRange.mockResolvedValue({
      ranges: {},
      overlapRange: { start: '2010-01-01', end: '2020-12-31', recommendedStart: '2016-05-01' },
    });
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }, { cached: true }] });
    const row = {
      id: 'row-cliff', rebalance: 'none',
      legs: [{ label: 'OPT', type: 'instrument', collection: 'INDEX', symbol: 'X', weight: 100 }],
    };
    renderHook((props) => usePortfolioCacheStatus(props), {
      initialProps: baseProps({ portfolios: [row] }),
    });
    await waitFor(() => expect(getPortfolioCacheStatus).toHaveBeenCalled(), { timeout: 2000 });
    const queries = getPortfolioCacheStatus.mock.calls[0][0];
    // queries[0] = active (from overlapRange prop); queries[1] = the saved row —
    // it must key from recommendedStart (2016-05-01), NOT the raw start (2010-01-01).
    expect(queries[1].start).toBe('2016-05-01');
    // Restore the module default for subsequent tests.
    resolvePortfolioRange.mockResolvedValue({
      ranges: {}, overlapRange: { start: '2020-01-01', end: '2020-12-31' },
    });
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

  // PER-INSTRUMENT: a saved row's source rides its own persisted leg. The probe
  // body must carry the leg's source on the LEG (present only for v2), never as
  // a top-level field. docSignature captures ``doc.legs`` (which include the
  // per-leg source), so the memo stays content-addressed on it.
  it('probes a v1 row with NO source key and a v2 row with data_source:"v2" on the leg', async () => {
    getPortfolioCacheStatus.mockResolvedValue({ results: [{ cached: true }, { cached: true }] });
    const v1Row = {
      id: 'v1-row', rebalance: 'none',
      legs: [{ label: 'NDX', type: 'instrument', collection: 'INDEX', symbol: 'NDX', weight: 100 }],
    };
    const v2Row = {
      id: 'v2-row', rebalance: 'none',
      legs: [{ label: 'NDX', type: 'instrument', collection: 'INDEX', symbol: 'NDX', weight: 100, dataSource: 'v2' }],
    };
    // No active legs → the ONLY probed bodies are the rows'.
    renderHook((p) => usePortfolioCacheStatus(p), {
      initialProps: baseProps({ legs: [], portfolios: [v1Row, v2Row] }),
    });
    await waitFor(() => expect(getPortfolioCacheStatus).toHaveBeenCalled(), { timeout: 2000 });
    // Collect every probed body across all calls, keyed by whether its NDX leg is v2.
    const bodies = getPortfolioCacheStatus.mock.calls.flatMap((c) => c[0]);
    await waitFor(() => {
      const all = getPortfolioCacheStatus.mock.calls.flatMap((c) => c[0]);
      expect(all.length).toBeGreaterThanOrEqual(2);
    }, { timeout: 2000 });
    const finalBodies = getPortfolioCacheStatus.mock.calls.flatMap((c) => c[0]);
    for (const b of finalBodies) {
      expect(b.data_source).toBeUndefined(); // never a top-level field
    }
    const v1Body = finalBodies.find((b) => !('data_source' in b.legs.NDX));
    const v2Body = finalBodies.find((b) => b.legs.NDX.data_source === 'v2');
    expect(v1Body).toBeTruthy();
    expect(v2Body).toBeTruthy();
    expect(bodies).toBeDefined();
  });
});
