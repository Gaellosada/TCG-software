// @vitest-environment jsdom
//
// NIT-1 regression lock (Sign 8): the reactive cache-key effect must hydrate
// indicators the SAME way handleCalculate does, so a COMPOSED portfolio whose
// referenced PURE child contains a SIGNAL leg still keys (non-null) and the
// reactive key EQUALS the compute key. Before the fix, the effect gated
// hydration on PARENT legs only (`legs.some(type==='signal')`) → for a
// composed→signal-child it passed availableIndicators=[] → the child's signal
// leg reported a missing indicator → key nulled, while handleCalculate (which
// always hydrates) computed fine. That divergence is what this test guards.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
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
// Range resolution is irrelevant here (we set dates explicitly); stub it so the
// composed leg never touches the warehouse.
vi.mock('./resolvePortfolioRange', () => ({
  resolvePortfolioRange: vi.fn(() => Promise.resolve({ ranges: {}, overlapRange: null })),
}));
// The signal-leg builder reports a MISSING indicator iff it was handed an EMPTY
// indicator set — this is the exact failure the parent-only gate triggered.
vi.mock('../Signals/requestBuilder', () => ({
  buildComputeRequestBody: vi.fn((spec, indicators) => (
    indicators && indicators.length > 0
      ? { body: { spec: { id: (spec && spec.id) || 's1' }, indicators: [] }, missing: [] }
      : { body: {}, missing: ['ind1'] }
  )),
}));
vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([{ id: 'ind1', name: 'Ind' }])),
}));
vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));
// Cache ON so the reactive cache-key effect runs.
vi.mock('../../lib/userSettings', () => ({ isPortfolioCacheEnabled: () => true }));
// Cache store is a no-op sink for this test.
vi.mock('../../lib/portfolioCache', () => ({
  getCached: vi.fn(() => Promise.resolve(null)),
  putCached: vi.fn(() => Promise.resolve()),
}));

import { computePortfolio } from '../../api/portfolio';
import { getPortfolio } from '../../api/persistence';
import { putCached } from '../../lib/portfolioCache';

// A PURE child whose only leg is a SIGNAL (in-scope per §4).
const CHILD_WITH_SIGNAL = {
  id: 'c1', name: 'Signal Child', kind: 'pure', category: 'RESEARCH', rebalance: 'none',
  legs: [{
    label: 'Sig', type: 'signal', signalId: 's1', signalName: 'S',
    signalSpec: { id: 's1', name: 'S', inputs: [], rules: {} },
  }],
};

describe('usePortfolio — reactive cache key for a composed portfolio with a signal-bearing child (NIT-1)', () => {
  beforeEach(() => {
    computePortfolio.mockClear();
    putCached.mockClear();
    getPortfolio.mockReset();
    getPortfolio.mockResolvedValue(CHILD_WITH_SIGNAL);
  });

  it('reactive key is NON-NULL and EQUALS the compute key (hydration parity)', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => result.current.addPortfolioLeg(CHILD_WITH_SIGNAL));
    act(() => { result.current.setStartDate('2020-01-01'); result.current.setEndDate('2020-12-31'); });

    // The reactive cache-key effect (debounced 275ms) must resolve a NON-NULL
    // key even though the signal leg lives inside the referenced child.
    await waitFor(
      () => expect(result.current.currentCacheKey).toBeTruthy(),
      { timeout: 2000 },
    );
    const reactiveKey = result.current.currentCacheKey;

    // handleCalculate builds + hashes the compute body and stores it under the
    // COMPUTE key (putCached's first arg). Parity ⇒ reactiveKey === computeKey.
    await act(async () => { await result.current.handleCalculate(); });
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    expect(putCached).toHaveBeenCalled();
    const computeKey = putCached.mock.calls[0][0];

    expect(computeKey).toBeTruthy();
    expect(reactiveKey).toBe(computeKey);
  });
});
