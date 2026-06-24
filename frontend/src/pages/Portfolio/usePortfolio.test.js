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
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
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
    entries: [{ id: 'e1', input_id: 'X', weight: 50, conditions: [] }],
    exits: [],
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
    const legId = result.current.legs[0].id;
    const range = result.current.legDateRanges[legId];
    expect(range).toBeDefined();
    expect(range.start).toBeTruthy();
    expect(range.end).toBeTruthy();
  });

  it('handleCalculate forwards option_stream roll_offset (no adjustment) to the API', async () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg({
        label: 'OPT_SP_500 C mid',
        type: 'option_stream',
        collection: 'OPT_SP_500',
        option_type: 'C',
        cycle: null,
        maturity: { kind: 'nearest_to_target', target_days: 30 },
        selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
        stream: 'mid',
        // A stray adjustment must NOT be forwarded — option streams have none.
        adjustment: 'ratio',
        roll_offset: 5,
        weight: 100,
      });
    });

    // Option-stream legs require explicit dates (the BE can't infer range).
    act(() => {
      result.current.setStartDate('2024-01-01');
      result.current.setEndDate('2024-12-31');
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    expect(computePortfolio).toHaveBeenCalledTimes(1);
    const call = computePortfolio.mock.calls[0][0];
    const legLabel = result.current.legs[0].label;
    const leg = call.legs[legLabel];
    expect(leg).toEqual({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'mid',
      roll_offset: 5,
    });
    // The stray adjustment was dropped — option streams carry no back-adjustment.
    expect('adjustment' in leg).toBe(false);
  });

  it('handleCalculate omits option_stream roll fields when at defaults (none/0)', async () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg({
        label: 'OPT_SP_500 C iv',
        type: 'option_stream',
        collection: 'OPT_SP_500',
        option_type: 'C',
        cycle: null,
        maturity: { kind: 'nearest_to_target', target_days: 30 },
        selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
        stream: 'iv',
        adjustment: 'none',
        roll_offset: 0,
        weight: 100,
      });
    });
    act(() => {
      result.current.setStartDate('2024-01-01');
      result.current.setEndDate('2024-12-31');
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    const call = computePortfolio.mock.calls[0][0];
    const leg = call.legs[result.current.legs[0].label];
    // Minimal request body — defaults are omitted (BE defaults them).
    expect(leg).not.toHaveProperty('adjustment');
    expect(leg).not.toHaveProperty('roll_offset');
  });
});

// ---------------------------------------------------------------------------
// addLeg auto-suffix tests (item 4 in review-nits spec)
// ---------------------------------------------------------------------------

describe('usePortfolio — addLeg auto-suffix on duplicate labels', () => {
  const baseLeg = {
    label: 'SPX',
    type: 'instrument',
    collection: 'INDEX',
    symbol: 'SPX',
    weight: 100,
  };

  it('first leg gets the label unchanged', () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(baseLeg);
    });

    expect(result.current.legs).toHaveLength(1);
    expect(result.current.legs[0].label).toBe('SPX');
  });

  it('duplicate label is suffixed with " (2)"', () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(baseLeg);
      result.current.addLeg(baseLeg);
    });

    const labels = result.current.legs.map((l) => l.label);
    expect(labels).toContain('SPX');
    expect(labels).toContain('SPX (2)');
  });

  it('third duplicate gets suffix " (3)"', () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(baseLeg);
      result.current.addLeg(baseLeg);
      result.current.addLeg(baseLeg);
    });

    const labels = result.current.legs.map((l) => l.label);
    expect(labels).toContain('SPX');
    expect(labels).toContain('SPX (2)');
    expect(labels).toContain('SPX (3)');
  });

  it('API-dict keys never collide after auto-suffix', () => {
    // When handleCalculate sends { legs: { label: payload } }, each key must be unique.
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(baseLeg);
      result.current.addLeg(baseLeg);
    });

    const labels = result.current.legs.map((l) => l.label);
    const uniqueLabels = new Set(labels);
    expect(uniqueLabels.size).toBe(labels.length);
  });

  it('UI-facing label reflects the renamed version', () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(baseLeg);
      result.current.addLeg(baseLeg);
    });

    // The second leg's label must differ from the first.
    const [first, second] = result.current.legs;
    expect(first.label).toBe('SPX');
    expect(second.label).not.toBe('SPX');
    expect(second.label).toBe('SPX (2)');
  });

  it('addSignalLeg also auto-suffixes on duplicate signal names', () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addSignalLeg(fakeSignal);
      result.current.addSignalLeg(fakeSignal);
    });

    const labels = result.current.legs.map((l) => l.label);
    expect(labels).toContain('Test Signal');
    expect(labels).toContain('Test Signal (2)');
    // API dict keys must not collide.
    expect(new Set(labels).size).toBe(labels.length);
  });
});
