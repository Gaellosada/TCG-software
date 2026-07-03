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

  it('handleCalculate forwards option_stream roll_offset {value, unit} to the API', async () => {
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
        roll_offset: { value: 2, unit: 'months' },
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
      // A mid (premium) option leg is ALWAYS sent hold-ON ($-P&L): the backend
      // rejects a mid/bs_mid option leg with hold off, so the compute body
      // carries hold_between_rolls + nav_times for every premium leg.
      hold_between_rolls: true,
      nav_times: 1.0,
      roll_offset: { value: 2, unit: 'months' },
    });
    // The stray adjustment was dropped — option streams carry no back-adjustment.
    expect('adjustment' in leg).toBe(false);
    // "End of month" is the maturity, not a separate roll_schedule.
    expect('roll_schedule' in leg).toBe(false);
  });

  it('handleCalculate forwards a legacy bare-int roll_offset as {value, days}', async () => {
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
        roll_offset: 5,  // legacy in-memory int
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
    const leg = computePortfolio.mock.calls[0][0].legs[result.current.legs[0].label];
    expect(leg.roll_offset).toEqual({ value: 5, unit: 'days' });
  });

  it('handleCalculate omits option_stream roll fields when at defaults', async () => {
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
        roll_offset: { value: 0, unit: 'days' },
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
    expect(leg).not.toHaveProperty('roll_schedule');
  });

  // ── Cleanup: roll_offset {value, unit} must SURVIVE the persist round-trip
  // for a direct portfolio option leg (savePortfolio → loadPortfolio).
  it('roll_offset {value, unit} survives the portfolio persist round-trip', async () => {
    const { savePortfolio, loadPortfolio } = await import('./storage');
    const legs = [
      {
        label: 'OPT',
        type: 'option_stream',
        collection: 'OPT_SP_500',
        option_type: 'C',
        cycle: null,
        maturity: { kind: 'end_of_month', offset_months: 1 },
        selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
        stream: 'mid',
        roll_offset: { value: 3, unit: 'months' },
        weight: 100,
      },
    ];
    savePortfolio('rt-opt', { legs, rebalance: 'none' });
    const loaded = loadPortfolio('rt-opt');
    expect(loaded.legs[0].roll_offset).toEqual({ value: 3, unit: 'months' });
  });

  // ── Issue #3: a direct portfolio CONTINUOUS leg's strategy must reach the
  // compute wire AND survive persistence.
  it('handleCalculate forwards continuous strategy=end_of_month to the API', async () => {
    const { result } = renderHook(() => usePortfolio());
    act(() => {
      result.current.addLeg({
        label: 'FUT_ES',
        type: 'continuous',
        collection: 'FUT_ES',
        strategy: 'end_of_month',
        adjustment: 'ratio',
        cycle: 'HMUZ',
        rollOffset: 0,
        weight: 100,
      });
    });
    await act(async () => {
      await result.current.handleCalculate();
    });
    const leg = computePortfolio.mock.calls[0][0].legs[result.current.legs[0].label];
    expect(leg.strategy).toBe('end_of_month');
  });

  it('continuous strategy survives the portfolio persist round-trip', async () => {
    const { savePortfolio, loadPortfolio } = await import('./storage');
    const legs = [
      {
        label: 'FUT',
        type: 'continuous',
        collection: 'FUT_ES',
        strategy: 'end_of_month',
        adjustment: 'none',
        cycle: null,
        rollOffset: 0,
        weight: 100,
      },
    ];
    savePortfolio('rt-fut', { legs, rebalance: 'none' });
    const loaded = loadPortfolio('rt-fut');
    expect(loaded.legs[0].strategy).toBe('end_of_month');
  });
});

// ---------------------------------------------------------------------------
// BUG (PR #67 runtime): a portfolio option leg is blocked by the
// "Option stream legs require explicit start and end dates" guard even in the
// normal flow where the user never drags the TimeRangeSlider. The slider
// treats empty startDate/endDate as "use full min/max" and VISUALLY shows a
// complete range, but handleCalculate treats '' as "no dates" and rejects.
// So the user sees a full-range timeframe yet Compute fails with a confusing
// "please set a date range". A portfolio option leg should resolve over the
// portfolio's available/backtest window without forcing a manual drag.
//
// These tests are RED on the current code and define the acceptance criterion
// for the fix.
// ---------------------------------------------------------------------------

describe('usePortfolio — option leg date window (PR #67 bug)', () => {
  const optionLeg = {
    label: 'OPT_SP_500 C mid',
    type: 'option_stream',
    collection: 'OPT_SP_500',
    option_type: 'C',
    cycle: null,
    maturity: { kind: 'nearest_to_target', target_days: 30 },
    selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05, strict: false },
    stream: 'mid',
    weight: 100,
  };

  it('computes an OPTION-ONLY portfolio without a manual slider drag', async () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(optionLeg);
    });

    // Let the per-leg date-range useEffect settle (mirrors the real UI: the
    // user adds a leg, the ranges load, the slider renders showing a full
    // range, then the user clicks Compute WITHOUT dragging).
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    // Expected: the request goes through (BE resolves over the window) and no
    // "set a date range" guard error is surfaced.
    expect(result.current.error).toBeNull();
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    const body = computePortfolio.mock.calls[0][0];
    // The portfolio MUST supply a concrete window for the option leg.
    expect(body.start).toBeTruthy();
    expect(body.end).toBeTruthy();
  });

  it('seeds the platform-standard 5-year default window for an option-only portfolio', async () => {
    // Option-only portfolios have no priced leg to anchor on, so the window
    // must default to ~5 years back from today (matching the basket default),
    // and that window must reach both the slider (via overlapRange) and the
    // compute request.
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(optionLeg);
    });

    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Expected window computed the same way defaultDateRange() (utils/format)
    // does — kept inline so the test is decoupled from the helper's internals.
    const end = new Date();
    const start = new Date();
    start.setFullYear(start.getFullYear() - 5);
    const iso = (d) => d.toISOString().slice(0, 10);
    const expectedStart = iso(start);
    const expectedEnd = iso(end);

    // The slider min/max are bound to overlapRange (PortfolioPage), so it must
    // carry the default window.
    expect(result.current.overlapRange).toEqual({ start: expectedStart, end: expectedEnd });

    await act(async () => {
      await result.current.handleCalculate();
    });

    expect(result.current.error).toBeNull();
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    const body = computePortfolio.mock.calls[0][0];
    expect(body.start).toBe(expectedStart);
    expect(body.end).toBe(expectedEnd);
  });

  it('computes an OPTION + INSTRUMENT portfolio without a manual slider drag', async () => {
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg({
        label: 'SPX',
        type: 'instrument',
        collection: 'INDEX',
        symbol: 'SPX',
        weight: 100,
      });
      result.current.addLeg(optionLeg);
    });

    // Ranges settle: the instrument leg yields a real overlapRange, so the
    // slider shows that full window — but startDate/endDate are still ''.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    expect(result.current.error).toBeNull();
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    const body = computePortfolio.mock.calls[0][0];
    // The window falls back to the available overlap (the instrument's
    // 2020-01-01..2020-12-31 from the getInstrumentPrices mock) — NOT the 5y
    // default — so the BE can enumerate the option leg's trade dates and the
    // window matches what the slider shows.
    expect(body.start).toBe('2020-01-01');
    expect(body.end).toBe('2020-12-31');
  });

  it('an explicit slider selection overrides the fallback window', async () => {
    // The auto-window is only a FALLBACK: when the user has narrowed the range
    // via the slider, those exact dates must be sent (not the overlap/default).
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg(optionLeg);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    act(() => {
      result.current.setStartDate('2022-03-01');
      result.current.setEndDate('2022-09-30');
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    expect(result.current.error).toBeNull();
    const body = computePortfolio.mock.calls[0][0];
    expect(body.start).toBe('2022-03-01');
    expect(body.end).toBe('2022-09-30');
  });

  // ── Edge cases (review hardening) ──

  it('sends the overlap window when only ONE of start/end is set (the other falls back)', async () => {
    // The slider always emits both dates, but the effective-window logic must
    // also handle a half-set window programmatically: the unset side falls
    // back to the overlap (here the instrument's 2020-01-01..2020-12-31).
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg({
        label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100,
      });
      result.current.addLeg(optionLeg);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Only the start is set; the end is left empty.
    act(() => {
      result.current.setStartDate('2020-06-01');
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    expect(result.current.error).toBeNull();
    const body = computePortfolio.mock.calls[0][0];
    expect(body.start).toBe('2020-06-01');        // explicit start preserved
    expect(body.end).toBe('2020-12-31');          // end fell back to overlap end
  });

  it('guard fires (safety net) when priced legs are disjoint so no overlap exists', async () => {
    // Two instrument legs with non-overlapping ranges → overlapStart > overlapEnd
    // → overlapRange is null (the option-stream default branch is NOT reached,
    // since validStarts is non-empty). With an option leg present and no
    // derivable window, the guard correctly fires instead of sending an
    // undefined window to the backend.
    const prev = getInstrumentPrices.getMockImplementation();
    getInstrumentPrices.mockImplementation((_collection, symbol) => {
      if (symbol === 'EARLY') return Promise.resolve({ dates: [20180101, 20181231] });
      if (symbol === 'LATE') return Promise.resolve({ dates: [20230101, 20231231] });
      return Promise.resolve({ dates: [20200101, 20201231] });
    });
    try {
      const { result } = renderHook(() => usePortfolio());

      act(() => {
        result.current.addLeg({
          label: 'EARLY', type: 'instrument', collection: 'INDEX', symbol: 'EARLY', weight: 50,
        });
        result.current.addLeg({
          label: 'LATE', type: 'instrument', collection: 'INDEX', symbol: 'LATE', weight: 50,
        });
        result.current.addLeg(optionLeg);
      });
      await act(async () => {
        await new Promise((r) => setTimeout(r, 50));
      });

      // Disjoint priced legs → no overlap.
      expect(result.current.overlapRange).toBeNull();

      await act(async () => {
        await result.current.handleCalculate();
      });

      expect(result.current.error).toBe(
        'Option stream legs require explicit start and end dates. Please set a date range.',
      );
      expect(computePortfolio).not.toHaveBeenCalled();
    } finally {
      getInstrumentPrices.mockImplementation(prev);
    }
  });

  it('a spot/futures-only portfolio also sends the effective (overlap) window', async () => {
    // Non-option portfolios were never blocked, but they must still send the
    // effective window (the overlap), not undefined — confirms the fix did not
    // change the happy path for instrument/continuous-only portfolios.
    const { result } = renderHook(() => usePortfolio());

    act(() => {
      result.current.addLeg({
        label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100,
      });
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    await act(async () => {
      await result.current.handleCalculate();
    });

    expect(result.current.error).toBeNull();
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    const body = computePortfolio.mock.calls[0][0];
    // Window = the instrument's overlap (2020-01-01..2020-12-31), not undefined.
    expect(body.start).toBe('2020-01-01');
    expect(body.end).toBe('2020-12-31');
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
