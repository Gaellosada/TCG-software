// @vitest-environment jsdom
//
// Auto-display (read-only cache) additions to usePortfolio:
//   - when the active config resolves to a CACHED key, its result is displayed
//     automatically (no Compute click, no compute call) via /cache/get;
//   - a cache MISS leaves the display blank (never computes);
//   - an edit that changes the key BLANKS the display immediately;
//   - a fresh Compute is never clobbered by a late auto-display get.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import usePortfolio from './usePortfolio';

const HIT_RESULT = { portfolio_equity: [100, 110], dates: ['2020-01-01', '2020-12-31'], from_cache: true };

vi.mock('../../api/portfolio', () => ({
  computePortfolio: vi.fn(() => Promise.resolve({ portfolio_equity: [1, 2], dates: ['a', 'b'], from_cache: false })),
  getPortfolioCachedResult: vi.fn(() => Promise.resolve({ result: null, from_cache: false })),
}));
vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
}));
vi.mock('../../api/options', () => ({
  getOptionCoverage: vi.fn(() => Promise.resolve({ root: 'X', start: '2005-12-01', end: '2025-06-30' })),
}));
vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));
vi.mock('../Signals/requestBuilder', () => ({
  buildComputeRequestBody: vi.fn(() => ({ body: {}, missing: [] })),
}));
vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));
vi.mock('../../components/SaveControls', () => ({ useAutosave: vi.fn(), default: () => null }));

import { computePortfolio, getPortfolioCachedResult } from '../../api/portfolio';

const INSTR_LEG = { type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100, label: 'SPX' };

beforeEach(() => {
  const store = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  });
  vi.clearAllMocks();
  getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
});

describe('usePortfolio — auto-display cached result', () => {
  it('HIT: auto-displays the cached result with no compute call', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    await waitFor(() => expect(result.current.results).toEqual(HIT_RESULT));
    expect(getPortfolioCachedResult).toHaveBeenCalled();
    expect(computePortfolio).not.toHaveBeenCalled(); // auto-display NEVER computes
  });

  it('MISS: leaves the display blank and never computes', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    // Give any pending microtasks a chance; result must remain null.
    await act(async () => { await Promise.resolve(); });
    expect(result.current.results).toBeNull();
    expect(computePortfolio).not.toHaveBeenCalled();
  });

  it('EDIT: an edit that changes the key blanks the displayed result', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(result.current.results).toEqual(HIT_RESULT));

    // The edited config is uncached → the next get is a miss.
    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
    act(() => { result.current.updateLeg(0, { weight: 50 }); });

    // Display blanks (immediately on the key change; stays blank on the miss).
    await waitFor(() => expect(result.current.results).toBeNull());
  });

  it('does not auto-display when caching is disabled', async () => {
    localStorage.setItem('tcg-portfolio-cache-enabled', 'false');
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(getPortfolioCachedResult).not.toHaveBeenCalled();
    expect(result.current.results).toBeNull();
  });
});
