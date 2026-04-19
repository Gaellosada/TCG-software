// @vitest-environment jsdom
//
// Tests for IndicatorChart's ownPanel split — verifies that:
//   - overlay mode renders one Chart with combined traces on a single y-axis
//   - ownPanel mode renders ONE Chart whose layout has two y-axis subplots
//     (yaxis for price top, yaxis2 for indicator bottom) sharing the same
//     x-axis, so zoom/pan/hover stay synchronised across the two panes
//
// The shared Chart component is mocked with a minimal stub that captures
// traces + layoutOverrides so the tests can assert on subplot structure
// without pulling Plotly into jsdom.

import { describe, it, expect, vi, afterEach } from 'vitest';
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
        data-download-filename={downloadFilename}
      />
    );
  }
  return { default: ChartStub };
});

// Import AFTER vi.mock so the stub is wired.
import IndicatorChart from './IndicatorChart';

afterEach(() => {
  cleanup();
  chartProps.length = 0;
});

function makeResult() {
  return {
    dates: ['2024-01-01', '2024-01-02', '2024-01-03'],
    series: [
      {
        label: 'close',
        collection: 'INDEX',
        instrument_id: '^GSPC',
        close: [4000, 4010, 4020],
      },
    ],
    indicator: [50, 55, 60],
  };
}

describe('<IndicatorChart> — ownPanel split', () => {
  it('renders a single overlay chart when indicator.ownPanel is false', () => {
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'My ind', ownPanel: false }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    expect(screen.getByTestId('indicator-chart-overlay')).toBeTruthy();
    expect(screen.queryByTestId('indicator-chart-split')).toBeNull();
    const charts = screen.getAllByTestId('chart-stub');
    expect(charts).toHaveLength(1);
    // One price trace + one indicator trace combined on y1.
    expect(charts[0].getAttribute('data-trace-count')).toBe('2');
    // Neither trace targets yaxis2 (overlay path, Y2 heuristic off for this data).
    expect(chartProps[0].traces.every((t) => !t.yaxis)).toBe(true);
    // No yaxis2 subplot configured.
    expect(chartProps[0].layoutOverrides.yaxis2).toBeUndefined();
  });

  it('renders a single chart with stacked subplots when indicator.ownPanel is true', () => {
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'My ind', ownPanel: true }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    expect(screen.getByTestId('indicator-chart-split')).toBeTruthy();
    expect(screen.queryByTestId('indicator-chart-overlay')).toBeNull();
    const charts = screen.getAllByTestId('chart-stub');
    expect(charts).toHaveLength(1);

    const { traces, layoutOverrides, downloadFilename } = chartProps[0];

    // Single download — one chart, one export.
    expect(downloadFilename).toBe('indicator-My ind');

    // Indicator trace is pinned to yaxis2; price stays on default y.
    const [priceTrace, indTrace] = traces;
    expect(priceTrace.yaxis).toBeUndefined();
    expect(indTrace.yaxis).toBe('y2');

    // Layout has two stacked y-axes with disjoint domains so the panes
    // don't overlap, and the x-axis is anchored below the bottom pane.
    expect(layoutOverrides.yaxis.domain).toEqual([0.52, 1.0]);
    expect(layoutOverrides.yaxis2.domain).toEqual([0, 0.48]);
    expect(layoutOverrides.yaxis2.anchor).toBe('x');
    expect(layoutOverrides.xaxis.anchor).toBe('y2');
  });

  it('renders the shared error card (no chart) when an error is present, even with ownPanel=true', () => {
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'My ind', ownPanel: true }}
        result={null}
        loading={false}
        error={{ error_type: 'runtime', message: 'boom' }}
      />,
    );
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.queryByTestId('indicator-chart-split')).toBeNull();
    expect(screen.queryByTestId('indicator-chart-overlay')).toBeNull();
    expect(screen.queryByTestId('chart-stub')).toBeNull();
  });
});
