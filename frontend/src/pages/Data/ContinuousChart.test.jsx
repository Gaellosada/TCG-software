// @vitest-environment jsdom
//
// Tests for ContinuousChart — the futures continuous-series page.
//
// Pinned contracts (CONTRACT §E.2 — futures-roll-markers):
//   - Empty roll_dates → markers = []
//   - Per roll, two markers (sell + buy) at the SAME x with y = close[i-1] / close[i]
//   - Missing roll date in `dates` → that roll is skipped (no crash)
//   - markerHovertemplates referencing customdata[0]/customdata[1] is passed to Chart
//   - NO trace with `yaxis: 'y3'` is emitted (vertical-line removal regression guard)
//   - layoutOverrides.yaxis3 is absent
//
// Strategy: mock Chart to capture props, mock api/data to return a synthetic
// continuous-series payload. Mirrors ContinuousOptionsChart.test.jsx.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, waitFor, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks — must be declared before the component import (vitest hoists them).
// ---------------------------------------------------------------------------

let capturedChartProps = null;
vi.mock('../../components/Chart', () => ({
  default: vi.fn((props) => {
    capturedChartProps = props;
    return <div data-testid="chart" />;
  }),
}));

let mockSeriesResult = null;
const mockGetContinuousSeries = vi.fn(() => Promise.resolve(mockSeriesResult));
const mockGetAvailableCycles = vi.fn(() => Promise.resolve([]));

vi.mock('../../api/data', () => ({
  getContinuousSeries: (...args) => mockGetContinuousSeries(...args),
  getAvailableCycles: (...args) => mockGetAvailableCycles(...args),
}));

// Import after mocks.
import ContinuousChart from './ContinuousChart';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makePayload({ dates, close, roll_dates = [], contracts = [], volume = null }) {
  return {
    collection: 'FUT_ES',
    strategy: 'front_month',
    adjustment: 'none',
    cycle: null,
    dates,
    open: close,
    high: close,
    low: close,
    close,
    volume: volume ?? new Array(dates.length).fill(0),
    roll_dates,
    contracts,
  };
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

beforeEach(() => {
  capturedChartProps = null;
  mockSeriesResult = null;
  mockGetContinuousSeries.mockImplementation(() => Promise.resolve(mockSeriesResult));
  mockGetAvailableCycles.mockImplementation(() => Promise.resolve([]));
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ContinuousChart — markers transformation', () => {
  it('passes markers=[] when payload has no roll_dates', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103],
      close: [100, 101, 102],
      roll_dates: [],
      contracts: ['ESH24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
    });

    expect(Array.isArray(capturedChartProps.markers)).toBe(true);
    expect(capturedChartProps.markers).toHaveLength(0);
  });

  it('emits two markers per roll (sell + buy) with correct x/y/kind/customdata', async () => {
    // Roll at index 2 — dates[2] == 20240103. So:
    //   sell.y = close[1] = 101, sell.customdata = ['ESH24', 101]
    //   buy.y  = close[2] = 200, buy.customdata  = ['ESM24', 200]
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103, 20240104],
      close: [100, 101, 200, 201],
      roll_dates: [20240103],
      contracts: ['ESH24', 'ESM24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
      expect(capturedChartProps.markers.length).toBeGreaterThan(0);
    });

    const markers = capturedChartProps.markers;
    expect(markers).toHaveLength(2);

    const sell = markers.find((m) => m.kind === 'sell');
    const buy = markers.find((m) => m.kind === 'buy');

    expect(sell).toBeTruthy();
    expect(buy).toBeTruthy();

    // x is the formatted roll date — same format as the Close line trace.
    expect(sell.x).toBe('2024-01-03');
    expect(buy.x).toBe('2024-01-03');

    // y comes from the close array — sell = close[i-1], buy = close[i].
    expect(sell.y).toBe(101);
    expect(buy.y).toBe(200);

    // customdata is caller-controlled: [contract_id, price].
    expect(sell.customdata).toEqual(['ESH24', 101]);
    expect(buy.customdata).toEqual(['ESM24', 200]);
  });

  it('skips a roll whose date is not in the dates array (defensive)', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103],
      close: [100, 101, 102],
      roll_dates: [20240199],            // bogus date — not in `dates`
      contracts: ['ESH24', 'ESM24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
    });

    // No crash and no markers emitted for the unfindable roll.
    expect(capturedChartProps.markers).toHaveLength(0);
  });

  it('skips a roll occurring on the first date (no predecessor close)', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103],
      close: [100, 101, 102],
      roll_dates: [20240101],            // first bar — no close[i-1]
      contracts: ['ESH24', 'ESM24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
    });

    expect(capturedChartProps.markers).toHaveLength(0);
  });

  it('skips a roll whose close[i-1] or close[i] is null/NaN', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103, 20240104],
      close: [100, null, 200, 201],
      roll_dates: [20240103],
      contracts: ['ESH24', 'ESM24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
    });

    expect(capturedChartProps.markers).toHaveLength(0);
  });

  it('forwards markerHovertemplates referencing customdata[0] (contract) and customdata[1] (price)', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103],
      close: [100, 101, 102],
      roll_dates: [],
      contracts: ['ESH24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
    });

    const mht = capturedChartProps.markerHovertemplates;
    expect(mht).toBeTruthy();
    expect(typeof mht.sell).toBe('string');
    expect(typeof mht.buy).toBe('string');
    // The sparser futures template references the 2-element customdata.
    expect(mht.sell).toContain('%{customdata[0]}');
    expect(mht.sell).toContain('%{customdata[1]');
    expect(mht.buy).toContain('%{customdata[0]}');
    expect(mht.buy).toContain('%{customdata[1]');
    // Verbs distinguish the two kinds.
    expect(mht.sell).toContain('<b>Sell</b>');
    expect(mht.buy).toContain('<b>Buy</b>');
  });
});

describe('ContinuousChart — adjustment-mode invariance (parametric)', () => {
  // The FE is mode-agnostic: it just reads `close[i-1]` and `close[i]` from the
  // payload. The backend has ALREADY applied any ratio/difference adjustment
  // before serialising, so:
  //   - none       → raw close → sell.y != buy.y at a real roll (gap visible)
  //   - ratio      → adjusted close → sell.y == buy.y (continuous line)
  //   - difference → adjusted close → sell.y == buy.y (continuous line)
  //
  // This regression guard makes the mode-agnostic invariant on the
  // close-derivation path explicit.
  const cases = [
    { mode: 'none',       sellClose: 101, buyClose: 200, expectGap: true  },
    { mode: 'ratio',      sellClose: 150, buyClose: 150, expectGap: false },
    { mode: 'difference', sellClose: 150, buyClose: 150, expectGap: false },
  ];

  it.each(cases)(
    'adjustment=$mode → markers read from close[i-1]/close[i] verbatim (gap=$expectGap)',
    async ({ mode, sellClose, buyClose, expectGap }) => {
      mockSeriesResult = {
        ...makePayload({
          dates: [20240101, 20240102, 20240103, 20240104],
          close: [100, sellClose, buyClose, 201],
          roll_dates: [20240103],
          contracts: ['ESH24', 'ESM24'],
        }),
        adjustment: mode,
      };

      render(<ContinuousChart collection="FUT_ES" />);

      await waitFor(() => {
        expect(capturedChartProps).not.toBeNull();
        expect(capturedChartProps.markers.length).toBeGreaterThan(0);
      });

      const markers = capturedChartProps.markers;
      expect(markers).toHaveLength(2);

      const sell = markers.find((m) => m.kind === 'sell');
      const buy = markers.find((m) => m.kind === 'buy');

      expect(sell).toBeTruthy();
      expect(buy).toBeTruthy();

      // Mode-agnostic invariant: FE reads exactly close[i-1] / close[i].
      expect(sell.y).toBe(sellClose);
      expect(buy.y).toBe(buyClose);

      if (expectGap) {
        // Raw close: sell and buy diverge — the price gap is visible.
        expect(sell.y).not.toBe(buy.y);
      } else {
        // Adjusted close: sell and buy overlap — ring-around-dot at the roll.
        expect(sell.y).toBe(buy.y);
      }
    },
  );
});

describe('ContinuousChart — vertical-line removal (regression guards)', () => {
  it('emits NO trace with yaxis: "y3" (the old vertical-line overlay axis)', async () => {
    // Pre-removal the futures roll dates were drawn as a gray dotted polyline
    // on a hidden `yaxis3` overlay. Post-removal that trace must NOT appear.
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103, 20240104],
      close: [100, 101, 200, 201],
      roll_dates: [20240103],
      contracts: ['ESH24', 'ESM24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
    });

    const offending = capturedChartProps.traces.filter((t) => t.yaxis === 'y3');
    expect(offending).toHaveLength(0);
  });

  it('does NOT set yaxis3 in layoutOverrides (the overlay axis is gone)', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102, 20240103],
      close: [100, 101, 102],
      roll_dates: [],
      contracts: ['ESH24'],
    });

    render(<ContinuousChart collection="FUT_ES" />);

    await waitFor(() => {
      expect(capturedChartProps).not.toBeNull();
    });

    expect(capturedChartProps.layoutOverrides).toBeTruthy();
    expect(capturedChartProps.layoutOverrides.yaxis3).toBeUndefined();
  });
});

describe('ContinuousChart — roll strategy control (Issue #3)', () => {
  it('defaults the query to strategy=front_month', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102],
      close: [100, 101],
      roll_dates: [],
      contracts: ['ESH24'],
    });
    render(<ContinuousChart collection="FUT_ES" />);
    await waitFor(() => expect(mockGetContinuousSeries).toHaveBeenCalled());
    const firstCall = mockGetContinuousSeries.mock.calls[0];
    expect(firstCall[1]).toMatchObject({ strategy: 'front_month' });
  });

  it('re-queries with strategy=end_of_month when the select changes', async () => {
    mockSeriesResult = makePayload({
      dates: [20240101, 20240102],
      close: [100, 101],
      roll_dates: [],
      contracts: ['ESH24'],
    });
    render(<ContinuousChart collection="FUT_ES" />);
    // Controls render only after the series resolves (loading guard) — wait for
    // the Chart to mount, which means the controls panel is present.
    await waitFor(() => expect(capturedChartProps).not.toBeNull());

    fireEvent.change(screen.getByLabelText(/Roll strategy/i), {
      target: { value: 'end_of_month' },
    });

    await waitFor(() => {
      const last = mockGetContinuousSeries.mock.calls.at(-1);
      expect(last[1]).toMatchObject({ strategy: 'end_of_month' });
    });
  });
});
