// @vitest-environment jsdom
//
// SignalChart tests for the iter-3 v2 response shape.
// Response: { timestamps, positions: [{instrument, values, clipped_mask, price}], clipped }
//
// The shared Chart component is mocked with a stub that captures the
// traces + layoutOverrides so Plotly doesn't have to run inside jsdom.

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

function makeV2Result(overrides = {}) {
  // Three timestamps, one instrument, no price, no clipping.
  return {
    timestamps: [1577923200000, 1578009600000, 1578268800000],
    positions: [
      {
        instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
        values: [0, 1, 0],
        clipped_mask: [false, false, false],
        price: null,
      },
    ],
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

describe('<SignalChart> v2', () => {
  it('renders the empty state when no result', () => {
    render(<SignalChart result={null} loading={false} error={null} />);
    expect(screen.getByTestId('signal-chart-empty')).toBeDefined();
  });

  it('renders one position trace per instrument, no price', () => {
    render(<SignalChart result={makeV2Result()} loading={false} error={null} />);
    expect(screen.getByTestId('signal-chart-multi')).toBeDefined();
    expect(chartProps).toHaveLength(1);
    // One position trace for the one instrument.
    expect(chartProps[0].traces).toHaveLength(1);
    expect(chartProps[0].traces[0].name).toMatch(/pos/);
    expect(chartProps[0].layoutOverrides.yaxis).toBeDefined();
  });

  it('overlays price on a right-hand axis when price is present', () => {
    const result = makeV2Result({
      positions: [
        {
          instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
          values: [0, 1, 0],
          clipped_mask: [false, false, false],
          price: { label: '^GSPC.close', values: [100, 101, 102] },
        },
      ],
    });
    render(<SignalChart result={result} loading={false} error={null} />);
    // Position + price = 2 traces.
    expect(chartProps[0].traces).toHaveLength(2);
    expect(chartProps[0].traces.some((t) => t.name.includes('price'))).toBe(true);
  });

  it('stacks multiple instruments with per-instrument axes', () => {
    const result = makeV2Result({
      positions: [
        {
          instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
          values: [0, 1, 0],
          clipped_mask: [false, false, false],
          price: null,
        },
        {
          instrument: { collection: 'INDEX', instrument_id: '^NDX' },
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
  });

  it('shows a clip-banner when clipped=true', () => {
    const result = makeV2Result({
      positions: [
        {
          instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
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
    // Mentions the instrument label + bar count (2 bars).
    expect(banner.textContent).toMatch(/\^GSPC/);
    expect(banner.textContent).toMatch(/2 bars/);
  });

  it('does NOT render the banner when clipped=false', () => {
    render(<SignalChart result={makeV2Result()} loading={false} error={null} />);
    expect(screen.queryByTestId('signal-chart-clip-banner')).toBeNull();
  });
});
