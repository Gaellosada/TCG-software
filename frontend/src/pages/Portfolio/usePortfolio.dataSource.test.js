// @vitest-environment jsdom
//
// Per-instrument source on the portfolio wire — immutable model.
//
// A source is chosen ONCE, at add time, and rides each LEG. There is NO
// page/run-level source state and NO top-level ``data_source`` field. These
// tests pin that (a) a v1 leg emits no source key anywhere, (b) a v2 leg carries
// ``data_source:"v2"`` on the leg only, and (c) adding a v2 leg re-probes the
// cache (the per-leg source enters ``autoDisplaySig`` via the leg's JSON).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import usePortfolio from './usePortfolio';

vi.mock('../../api/portfolio', () => ({
  computePortfolio: vi.fn(() => Promise.resolve({ portfolio_equity: [1, 2], dates: ['a', 'b'], from_cache: false })),
  getPortfolioCachedResult: vi.fn(() => Promise.resolve({ result: null, from_cache: false })),
}));
vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
  getInstrumentPriceBounds: vi.fn(() => Promise.resolve({ start: 20200101, end: 20201231 })),
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

describe('usePortfolio — per-instrument data source', () => {
  it('exposes no page-level dataSource state or setter', () => {
    const { result } = renderHook(() => usePortfolio());
    expect(result.current.dataSource).toBeUndefined();
    expect(result.current.setDataSource).toBeUndefined();
  });

  it('a v1 leg emits no source key — top level or leg (byte-identity)', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    const body = getPortfolioCachedResult.mock.calls[0][0];
    expect(body.dataSource).toBeUndefined();               // no top-level field
    expect('data_source' in body.legs.SPX).toBe(false);    // leg omits it on v1
  });

  it('a v2 leg carries data_source:"v2" on the leg only, never at the top level', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg({ ...INSTR_LEG, dataSource: 'v2' }); });
    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    const body = getPortfolioCachedResult.mock.calls.at(-1)[0];
    expect(body.dataSource).toBeUndefined();               // still no top-level field
    expect(body.legs.SPX.data_source).toBe('v2');          // per-leg source
  });

  it('Compute sends the per-leg source (v2), never as a top-level field', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg({ ...INSTR_LEG, dataSource: 'v2' }); });
    await waitFor(() => expect(result.current.overlapRange).not.toBeNull());
    await act(async () => { await result.current.handleCalculate(); });
    expect(computePortfolio).toHaveBeenCalled();
    const call = computePortfolio.mock.calls.at(-1)[0];
    expect(call.dataSource).toBeUndefined();
    expect(call.legs.SPX.data_source).toBe('v2');
  });

  it('the per-leg source enters autoDisplaySig — a v2 leg re-probes the cache', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    const callsBefore = getPortfolioCachedResult.mock.calls.length;
    // Adding a v2 leg changes the per-leg JSON in the signature → re-probe fires.
    act(() => { result.current.addLeg({ ...INSTR_LEG, symbol: 'ES', label: 'ES', dataSource: 'v2' }); });
    await waitFor(() =>
      expect(getPortfolioCachedResult.mock.calls.length).toBeGreaterThan(callsBefore),
    );
    const last = getPortfolioCachedResult.mock.calls.at(-1)[0];
    expect('data_source' in last.legs.SPX).toBe(false);
    expect(last.legs.ES.data_source).toBe('v2');
  });
});
