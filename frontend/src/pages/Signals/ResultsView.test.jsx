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

  it('renders a third Chart for an indicator with ownPanel: true', () => {
    const result = makeResult({
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [100, 101, 102], ownPanel: false },
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
      ],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    // 2 standard charts + 1 ownPanel chart = 3 total
    expect(chartCalls).toHaveLength(3);
    const panelChart = chartCalls[2];
    expect(panelChart.downloadFilename).toBe('signal-indicator-rsi');
    // The panel chart should have the indicator trace on y2
    const y2Trace = panelChart.traces.find((t) => t.yaxis === 'y2');
    expect(y2Trace).toBeDefined();
    expect(y2Trace.y).toEqual([50, 60, 70]);
  });

  it('does not render ownPanel chart when indicator series is empty', () => {
    const result = makeResult({
      indicators: [
        { input_id: 'X', indicator_id: 'empty', series: [], ownPanel: true },
      ],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    // Only 2 standard charts since hasData is false for the empty ownPanel
    expect(chartCalls).toHaveLength(2);
  });

  it('renders multiple ownPanel Charts for multiple ownPanel indicators', () => {
    const result = makeResult({
      indicators: [
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
        { input_id: 'X', indicator_id: 'macd', series: [0.1, 0.2, 0.3], ownPanel: true },
      ],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    // 2 standard + 2 ownPanel = 4
    expect(chartCalls).toHaveLength(4);
    expect(chartCalls[2].downloadFilename).toBe('signal-indicator-rsi');
    expect(chartCalls[3].downloadFilename).toBe('signal-indicator-macd');
  });
});
