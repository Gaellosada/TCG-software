// @vitest-environment jsdom
//
// ResultsView tests — unified subplot chart. Stubs the shared <Chart>
// component so the tests inspect the traces passed to the single Plotly
// instance without actually rendering Plotly.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

const chartCalls = [];
vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub({ traces, layoutOverrides, downloadFilename, style }) {
    chartCalls.push({ traces, layoutOverrides, downloadFilename, style });
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

  it('renders "Computing..." while loading', () => {
    render(<ResultsView result={null} loading error={null} />);
    expect(screen.getByText(/Computing/)).toBeDefined();
  });

  it('renders the error card when error provided', () => {
    const err = { error_type: 'validation', message: 'bad config' };
    render(<ResultsView result={null} loading={false} error={err} />);
    expect(screen.getByText('Invalid signal')).toBeDefined();
    expect(screen.getByText('bad config')).toBeDefined();
  });

  it('mounts a SINGLE Chart instance when there is data', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    expect(chartCalls).toHaveLength(1);
    expect(screen.getByTestId('results-view')).toBeDefined();
    expect(screen.getByTestId('results-plot-unified')).toBeDefined();
  });

  it('includes the realized P&L trace in the unified chart', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    const [chart] = chartCalls;
    const names = chart.traces.map((t) => t.name);
    expect(names.some((n) => n.includes('realized P&L'))).toBe(true);
  });

  it('includes indicators and event markers in the unified chart', () => {
    const result = makeResult({
      indicators: [{ input_id: 'X', indicator_id: 'sma', series: [100, 101, 102] }],
      events: [{ input_id: 'X', block_id: 'b1', kind: 'long_entry', fired_indices: [1], latched_indices: [1] }],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    expect(chartCalls).toHaveLength(1);
    const names = chartCalls[0].traces.map((t) => t.name);
    expect(names.some((n) => n.startsWith('ind:'))).toBe(true);
    expect(names.some((n) => n.startsWith('long entry'))).toBe(true);
  });

  it('includes price traces for both top and bottom subplots', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    const [chart] = chartCalls;
    // Price traces appear twice — once for top (y default) and once for bottom (y2)
    const priceTraces = chart.traces.filter((t) => t.name.includes('X'));
    expect(priceTraces.length).toBeGreaterThanOrEqual(2);
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

  it('uses the unified download filename', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    expect(chartCalls[0].downloadFilename).toBe('signal-results');
  });

  it('renders ownPanel indicators as additional subplots in the SAME chart', () => {
    const result = makeResult({
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [100, 101, 102], ownPanel: false },
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
      ],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    // Still a single Chart component
    expect(chartCalls).toHaveLength(1);
    // The ownPanel indicator should appear as a trace on y3
    const rsiTrace = chartCalls[0].traces.find((t) => t.name && t.name.includes('rsi'));
    expect(rsiTrace).toBeDefined();
    expect(rsiTrace.yaxis).toBe('y3');
    // Layout should have yaxis3 for the ownPanel
    expect(chartCalls[0].layoutOverrides.yaxis3).toBeDefined();
    expect(chartCalls[0].layoutOverrides.yaxis3.title.text).toBe('rsi');
  });

  it('does not include ownPanel indicators with empty series', () => {
    const result = makeResult({
      indicators: [
        { input_id: 'X', indicator_id: 'empty', series: [], ownPanel: true },
      ],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    expect(chartCalls).toHaveLength(1);
    // No yaxis3 because the empty ownPanel is excluded
    expect(chartCalls[0].layoutOverrides.yaxis3).toBeUndefined();
  });

  it('chart fills container with 100% height/width', () => {
    render(<ResultsView result={makeResult()} loading={false} error={null} />);
    expect(chartCalls).toHaveLength(1);
    expect(chartCalls[0].style).toEqual({ width: '100%', height: '100%' });
  });

  it('renders multiple ownPanel indicators in the same single chart', () => {
    const result = makeResult({
      indicators: [
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
        { input_id: 'X', indicator_id: 'macd', series: [0.1, 0.2, 0.3], ownPanel: true },
      ],
    });
    render(<ResultsView result={result} loading={false} error={null} />);
    expect(chartCalls).toHaveLength(1);
    const lo = chartCalls[0].layoutOverrides;
    expect(lo.yaxis3).toBeDefined();
    expect(lo.yaxis3.title.text).toBe('rsi');
    expect(lo.yaxis4).toBeDefined();
    expect(lo.yaxis4.title.text).toBe('macd');
  });
});
