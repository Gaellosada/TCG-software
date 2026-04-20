// @vitest-environment jsdom
//
// ResultsView tests (iter-5 ask #6). Stubs the shared <Chart> component
// so the tests inspect the traces passed to each of the two Plotly
// instances without actually rendering Plotly.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

const chartCalls = [];
vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub({ traces, layoutOverrides, downloadFilename }) {
    chartCalls.push({ traces, layoutOverrides, downloadFilename });
    return (
      <div
        data-testid={`chart-stub-${downloadFilename}`}
        data-trace-count={Array.isArray(traces) ? traces.length : 0}
      />
    );
  }
  return { default: ChartStub };
});

import ResultsView from './ResultsView';

function makeResult(overrides = {}) {
  return {
    timestamps: [1577923200000, 1578009600000, 1578268800000],
    positions: [
      {
        input_id: 'X',
        instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
        values: [0, 1, 0],
        clipped_mask: [false, false, false],
        price: { label: 'SPX.close', values: [100, 101, 102] },
      },
    ],
    indicators: [],
    events: [],
    realized_pnl: [[0, 1, 2]],
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
  chartCalls.length = 0;
  consoleErrorSpy.mockRestore();
});

describe('<ResultsView>', () => {
  it('renders the empty state when no result', () => {
    render(<ResultsView result={null} loading={false} error={null} />);
    expect(screen.getByTestId('signal-chart-empty')).toBeDefined();
    // No charts mounted in the empty state.
    expect(chartCalls).toHaveLength(0);
  });

  it('renders "Computing…" while loading', () => {
    render(<ResultsView result={null} loading error={null} />);
    expect(screen.getByText(/Computing/)).toBeDefined();
  });

  it('renders the error card when error provided', () => {
    const err = { error_type: 'validation', message: 'bad config' };
    render(<ResultsView result={null} loading={false} error={err} />);
    expect(screen.getByText('Invalid signal')).toBeDefined();
    expect(screen.getByText('bad config')).toBeDefined();
  });

  it('mounts 2 Chart instances when there is data', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    expect(chartCalls).toHaveLength(2);
    expect(screen.getByTestId('results-view')).toBeDefined();
    expect(screen.getByTestId('results-plot-top')).toBeDefined();
    expect(screen.getByTestId('results-plot-bottom')).toBeDefined();
  });

  it('top plot carries the realized P&L trace; bottom plot does not', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    const [top, bottom] = chartCalls;
    const topNames = top.traces.map((t) => t.name);
    const bottomNames = bottom.traces.map((t) => t.name);
    expect(topNames.some((n) => n.includes('realized P&L'))).toBe(true);
    expect(bottomNames.some((n) => n.includes('realized P&L'))).toBe(false);
  });

  it('bottom plot carries indicators + event markers; top plot does not', () => {
    const result = makeResult({
      indicators: [{ input_id: 'X', indicator_id: 'sma', series: [100, 101, 102] }],
      events: [{ input_id: 'X', block_id: 'b1', kind: 'long_entry', latched_indices: [1] }],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    const [top, bottom] = chartCalls;
    const bottomNames = bottom.traces.map((t) => t.name);
    const topNames = top.traces.map((t) => t.name);
    expect(bottomNames.some((n) => n.startsWith('ind:'))).toBe(true);
    expect(bottomNames.some((n) => n.startsWith('long entry'))).toBe(true);
    expect(topNames.some((n) => n.startsWith('ind:'))).toBe(false);
    expect(topNames.some((n) => n.startsWith('long entry'))).toBe(false);
  });

  it('top and bottom charts both include the input price trace', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    const [top, bottom] = chartCalls;
    expect(top.traces.some((t) => t.name.includes('X'))).toBe(true);
    expect(bottom.traces.some((t) => t.name.includes('X'))).toBe(true);
  });

  it('shows the clip banner when result.clipped is true', () => {
    const result = makeResult({
      clipped: true,
      positions: [
        {
          input_id: 'X',
          instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
          values: [0, 1, 0.5],
          clipped_mask: [false, true, true],
          price: { label: 'SPX.close', values: [100, 101, 102] },
        },
      ],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    expect(screen.getByTestId('signal-chart-clip-banner')).toBeDefined();
  });

  it('uses distinct CSV download filenames for the two plots', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    const filenames = chartCalls.map((c) => c.downloadFilename);
    expect(new Set(filenames).size).toBe(2);
  });
});
