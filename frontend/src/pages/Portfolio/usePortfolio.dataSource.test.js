// @vitest-environment jsdom
//
// THE stale-cache guard for the v1/v2 source switch.
//
// ``data_source`` enters the backend cache key, so it MUST enter
// ``autoDisplaySig``. Without that, flipping v1→v2 leaves the v1 result on
// screen (the auto-display effect never re-fires, nothing re-probes) and the
// whole comparison silently reads as "no difference" — the feature looks like it
// works while showing the wrong data. These tests fail if the signature drops it.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import usePortfolio from './usePortfolio';

const V1_RESULT = { portfolio_equity: [100, 110], dates: ['2020-01-01', '2020-12-31'], from_cache: true };

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

describe('usePortfolio — data source', () => {
  it('defaults to v1', () => {
    const { result } = renderHook(() => usePortfolio());
    expect(result.current.dataSource).toBe('v1');
  });

  it('coerces a bogus value back to v1 — nothing can silently route a run to v2', () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.setDataSource('v3'); });
    expect(result.current.dataSource).toBe('v1');
  });

  it('STALE-CACHE GUARD: flipping v1→v2 blanks the displayed result and re-probes', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: V1_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(result.current.results).toEqual(V1_RESULT));
    const callsBefore = getPortfolioCachedResult.mock.calls.length;

    // v2 is not cached → the re-probe misses and the display must stay blank.
    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
    act(() => { result.current.setDataSource('v2'); });

    // If data_source were missing from autoDisplaySig, V1_RESULT would still be
    // on screen here and no new probe would have fired.
    await waitFor(() => expect(result.current.results).toBeNull());
    await waitFor(() =>
      expect(getPortfolioCachedResult.mock.calls.length).toBeGreaterThan(callsBefore),
    );
  });

  it('the cache-get key carries the source PER LEG only on v2 (no top-level field)', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    // v1 (default) — no source on the key body, nor on the leg.
    const first = getPortfolioCachedResult.mock.calls[0][0];
    expect(first.dataSource).toBeUndefined();          // no top-level field
    expect('data_source' in first.legs.SPX).toBe(false); // leg omits it on v1

    // Flipping the page default seeds v2 onto the (unset) leaf via the fold.
    act(() => { result.current.setDataSource('v2'); });
    await waitFor(() => {
      const last = getPortfolioCachedResult.mock.calls.at(-1)[0];
      expect(last.dataSource).toBeUndefined();          // still no top-level field
      expect(last.legs.SPX.data_source).toBe('v2');     // per-leg source
    });
  });

  it('Compute sends the source PER LEG (v2), never as a top-level field', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(result.current.overlapRange).not.toBeNull());

    await act(async () => { await result.current.handleCalculate(); });
    expect(computePortfolio).toHaveBeenCalled();
    const v1Call = computePortfolio.mock.calls.at(-1)[0];
    expect(v1Call.dataSource).toBeUndefined();
    expect('data_source' in v1Call.legs.SPX).toBe(false);

    act(() => { result.current.setDataSource('v2'); });
    await act(async () => { await result.current.handleCalculate(); });
    const v2Call = computePortfolio.mock.calls.at(-1)[0];
    expect(v2Call.dataSource).toBeUndefined();
    expect(v2Call.legs.SPX.data_source).toBe('v2');
  });
});
