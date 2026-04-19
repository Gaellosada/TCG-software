// @vitest-environment jsdom
//
// Tests for IndicatorChart's ownPanel split — verifies the branch that
// renders two stacked <Chart> components when indicator.ownPanel is true,
// vs the historical single overlaid chart when it's false.
//
// The shared Chart component is mocked with a minimal stub so the tests
// run in jsdom without pulling Plotly in. The stub renders a
// ``data-testid="chart-stub"`` div so we can count how many charts
// appear and inspect their props.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';

vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub({ traces, downloadFilename }) {
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
    // Overlay container present, split container absent.
    expect(screen.getByTestId('indicator-chart-overlay')).toBeTruthy();
    expect(screen.queryByTestId('indicator-chart-split')).toBeNull();
    // Exactly one <Chart> stub, with price + indicator traces combined.
    const charts = screen.getAllByTestId('chart-stub');
    expect(charts).toHaveLength(1);
    expect(charts[0].getAttribute('data-trace-count')).toBe('2');
  });

  it('renders two stacked charts when indicator.ownPanel is true', () => {
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'My ind', ownPanel: true }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    // Split container present, overlay container absent.
    const split = screen.getByTestId('indicator-chart-split');
    expect(split).toBeTruthy();
    expect(screen.queryByTestId('indicator-chart-overlay')).toBeNull();
    // Two Chart stubs — top has the price trace(s), bottom has exactly the indicator.
    const charts = within(split).getAllByTestId('chart-stub');
    expect(charts).toHaveLength(2);
    expect(charts[0].getAttribute('data-trace-count')).toBe('1');
    expect(charts[1].getAttribute('data-trace-count')).toBe('1');
    // Download filenames are suffixed so the user can tell the two exports apart.
    expect(charts[0].getAttribute('data-download-filename')).toMatch(/-price$/);
    expect(charts[1].getAttribute('data-download-filename')).toMatch(/-indicator$/);
  });

  it('renders the shared error card (no split) when an error is present, even with ownPanel=true', () => {
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
    expect(screen.queryByTestId('chart-stub')).toBeNull();
  });
});
