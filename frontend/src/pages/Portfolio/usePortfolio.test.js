// @vitest-environment jsdom
//
// Tests for signal-related additions to the usePortfolio hook:
//   - addSignalLeg adds a leg with type:'signal' and correct fields
//   - handleCalculate builds signal leg API payloads with buildComputeRequestBody
//   - signal legs fetch date ranges from their input instruments

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import usePortfolio from './usePortfolio';

// ── Mocks ──

vi.mock('../../api/portfolio', () => ({
  computePortfolio: vi.fn(() => Promise.resolve({ equity: [100, 110], dates: [1, 2] })),
}));

vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
}));

vi.mock('../Signals/requestBuilder', () => ({
  buildComputeRequestBody: vi.fn(() => ({
    body: {
      spec: { id: 's1', name: 'Test Signal', inputs: [], rules: {} },
      indicators: [],
    },
    missing: [],
  })),
}));

vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => []),
}));

// Mock useAutosave to a no-op so we don't trigger side effects.
vi.mock('../../components/SaveControls', () => ({
  useAutosave: vi.fn(),
  default: () => null,
}));

import { computePortfolio } from '../../api/portfolio';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { buildComputeRequestBody } from '../Signals/requestBuilder';

// Provide a minimal localStorage stub for the hook's internal use.
function createStorageStub() {
  const store = new Map();
  return {
    getItem: vi.fn((k) => (store.has(k) ? store.get(k) : null)),
    setItem: vi.fn((k, v) => { store.set(k, String(v)); }),
    removeItem: vi.fn((k) => { store.delete(k); }),
    clear: vi.fn(() => { store.clear(); }),
  };
}

beforeEach(() => {
  vi.stubGlobal('localStorage', createStorageStub());
  vi.clearAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const fakeSignal = {
  id: 's1',
  name: 'Test Signal',
  inputs: [{ id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } }],
  rules: {
    long_entry: [{ input_id: 'X', weight: 0.5, conditions: [] }],
    long_exit: [],
    short_entry: [],
    short_exit: [],
  },
};

describe('usePortfolio — signal leg support', () => {
  it('addSignalLeg adds a signal type leg with correct fields', () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addSignalLeg(fakeSignal);
    });

    expect(result.current.legs).toHaveLength(1);
    const leg = result.current.legs[0];
    expect(leg.type).toBe('signal');
    expect(leg.signalId).toBe('s1');
    expect(leg.signalName).toBe('Test Signal');
    expect(leg.signalSpec).toBe(fakeSignal);
    expect(leg.weight).toBe(100);
    expect(leg.label).toBe('Test Signal');
    // Non-signal fields should be null/0.
    expect(leg.collection).toBeNull();
    expect(leg.symbol).toBeNull();
    expect(leg.strategy).toBeNull();
    expect(leg.adjustment).toBeNull();
    expect(leg.cycle).toBeNull();
    expect(leg.rollOffset).toBe(0);
    // Should have an id.
    expect(leg.id).toBeDefined();
  });

  it('handleCalculate builds signal leg API payload using buildComputeRequestBody', async () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addSignalLeg(fakeSignal);
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    // buildComputeRequestBody should have been called with the signal spec.
    expect(buildComputeRequestBody).toHaveBeenCalledTimes(1);
    expect(buildComputeRequestBody).toHaveBeenCalledWith(
      fakeSignal,
      expect.any(Array),
    );

    // computePortfolio should have been called with the signal leg payload.
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    const call = computePortfolio.mock.calls[0][0];
    const legLabel = result.current.legs[0].label;
    expect(call.legs[legLabel]).toEqual({
      type: 'signal',
      signal_spec: {
        spec: { id: 's1', name: 'Test Signal', inputs: [], rules: {} },
        indicators: [],
      },
    });
  });

  it('handleCalculate surfaces error when signal references missing indicators', async () => {
    buildComputeRequestBody.mockReturnValueOnce({
      body: { spec: fakeSignal, indicators: [] },
      missing: ['sma-20', 'rsi-14'],
    });

    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addSignalLeg(fakeSignal);
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    // Should set error, not call computePortfolio.
    expect(result.current.error).toMatch(/missing indicators/i);
    expect(result.current.error).toContain('sma-20');
    expect(result.current.error).toContain('rsi-14');
    expect(computePortfolio).not.toHaveBeenCalled();
  });

  it('signal legs fetch date ranges from their input instruments', async () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addSignalLeg(fakeSignal);
    });

    // Wait for the useEffect that fetches date ranges to settle.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Signal leg with a spot input should call getInstrumentPrices for that input.
    expect(getInstrumentPrices).toHaveBeenCalledWith('INDEX', 'SPX');
    expect(getContinuousSeries).not.toHaveBeenCalled();

    // The leg's date range should be derived from the input's dates.
    const range = result.current.legDateRanges['Test Signal'];
    expect(range).toBeDefined();
    expect(range.start).toBeTruthy();
    expect(range.end).toBeTruthy();
  });
});
