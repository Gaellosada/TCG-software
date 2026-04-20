// @vitest-environment jsdom
//
// SignalChart tests for the iter-4 v3 response shape.
// Response: {
//   timestamps,
//   positions: [{input_id, instrument: {type,...}, values, clipped_mask, price}],
//   indicators: [],
//   clipped,
// }

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

const chartProps = [];
vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub({ traces, layoutOverrides, downloadFilename }) {
    chartProps.push({ traces, layoutOverrides, downloadFilename });
    return (
      <div
        data-testid="chart-stub"
        data-trace-count={Array.isArray(traces) ? traces.length : 0}
      />
    );
  }
  return { default: ChartStub };
});

import SignalChart from './SignalChart';

function makeV3Result(overrides = {}) {
  return {
    timestamps: [1577923200000, 1578009600000, 1578268800000],
    positions: [
      {
        input_id: 'X',
        instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
        values: [0, 1, 0],
        clipped_mask: [false, false, false],
        price: null,
      },
    ],
    indicators: [],
    clipped: false,
    ...overrides,
  };
}

let consoleErrorSpy;
beforeEach(() => {
  consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  chartProps.length = 0;
  consoleErrorSpy.mockRestore();
});

describe('<SignalChart> v3', () => {
  it('renders the empty state when no result', () => {
    render(<SignalChart result={null} loading={false} error={null} />);
    expect(screen.getByTestId('signal-chart-empty')).toBeDefined();
  });

  it('renders one position trace per input, labelled with the input id', () => {
    render(<SignalChart result={makeV3Result()} loading={false} error={null} />);
    expect(screen.getByTestId('signal-chart-multi')).toBeDefined();
    expect(chartProps).toHaveLength(1);
    expect(chartProps[0].traces).toHaveLength(1);
    const posTrace = chartProps[0].traces[0];
    expect(posTrace.name).toMatch(/pos/);
    // v3: label includes the input id.
    expect(posTrace.name).toMatch(/X/);
    expect(chartProps[0].layoutOverrides.yaxis).toBeDefined();
  });

  it('overlays price on a right-hand axis when price is present', () => {
    const result = makeV3Result({
      positions: [
        {
          input_id: 'X',
          instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
          values: [0, 1, 0],
          clipped_mask: [false, false, false],
          price: { label: 'SPX.close', values: [100, 101, 102] },
        },
      ],
    });
    render(<SignalChart result={result} loading={false} error={null} />);
    expect(chartProps[0].traces).toHaveLength(2);
    expect(chartProps[0].traces.some((t) => t.name.includes('price'))).toBe(true);
  });

  it('stacks multiple inputs with per-input axes (v3 two-input signals)', () => {
    const result = makeV3Result({
      positions: [
        {
          input_id: 'X',
          instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
          values: [0, 1, 0],
          clipped_mask: [false, false, false],
          price: null,
        },
        {
          input_id: 'Y',
          instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'NDX' },
          values: [0, 0.5, -0.3],
          clipped_mask: [false, false, false],
          price: null,
        },
      ],
    });
    render(<SignalChart result={result} loading={false} error={null} />);
    expect(chartProps[0].traces).toHaveLength(2);
    expect(chartProps[0].layoutOverrides.yaxis).toBeDefined();
    expect(chartProps[0].layoutOverrides.yaxis2).toBeDefined();
    const names = chartProps[0].traces.map((t) => t.name);
    expect(names.some((n) => n.includes('X'))).toBe(true);
    expect(names.some((n) => n.includes('Y'))).toBe(true);
  });

  it('labels continuous instruments differently ("cont <collection>")', () => {
    const result = makeV3Result({
      positions: [
        {
          input_id: 'Z',
          instrument: {
            type: 'continuous',
            collection: 'FUT_ES',
            adjustment: 'none',
            cycle: null,
            rollOffset: 2,
            strategy: 'front_month',
          },
          values: [0, 0.2, 0.4],
          clipped_mask: [false, false, false],
          price: null,
        },
      ],
    });
    render(<SignalChart result={result} loading={false} error={null} />);
    const posTrace = chartProps[0].traces[0];
    expect(posTrace.name).toMatch(/Z/);
    expect(posTrace.name).toMatch(/cont FUT_ES/);
  });

  it('shows a clip-banner when clipped=true', () => {
    const result = makeV3Result({
      positions: [
        {
          input_id: 'X',
          instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
          values: [0, 1, 0.5],
          clipped_mask: [false, true, true],
          price: null,
        },
      ],
      clipped: true,
    });
    render(<SignalChart result={result} loading={false} error={null} />);
    const banner = screen.getByTestId('signal-chart-clip-banner');
    expect(banner).toBeDefined();
    expect(banner.textContent).toMatch(/Position clipped/);
    expect(banner.textContent).toMatch(/SPX/);
    expect(banner.textContent).toMatch(/2 bars/);
  });

  it('does NOT render the banner when clipped=false', () => {
    render(<SignalChart result={makeV3Result()} loading={false} error={null} />);
    expect(screen.queryByTestId('signal-chart-clip-banner')).toBeNull();
  });
});
