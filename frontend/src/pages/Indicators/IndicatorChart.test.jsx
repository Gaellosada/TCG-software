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

  it('honours indicator.chartMode by setting the indicator trace mode (defaults to lines when absent)', () => {
    // Default path — no chartMode set → trace.mode === 'lines'.
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'Default mode', ownPanel: false }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    const traces1 = chartProps[0].traces;
    // Last trace is the indicator trace (series traces come first).
    const indTrace1 = traces1[traces1.length - 1];
    expect(indTrace1.mode).toBe('lines');
    cleanup();
    chartProps.length = 0;

    // Explicit 'markers' override — the common case for sparse outputs
    // like swing-pivots. The chart must pass this straight through to
    // Plotly's trace.mode so the points are visible.
    render(
      <IndicatorChart
        indicator={{ id: 'u2', name: 'Marker mode', ownPanel: false, chartMode: 'markers' }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    const traces2 = chartProps[0].traces;
    const indTrace2 = traces2[traces2.length - 1];
    expect(indTrace2.mode).toBe('markers');
  });

  it('fires the Y2 heuristic when indicator is small-magnitude and price is large (overlay mode)', () => {
    // Indicator values all in [0, 5], price values all > 100. The heuristic
    // at IndicatorChart.jsx:99 says indAbsMax < 10 && priceAbsMax > 100 →
    // promote the indicator trace to a secondary right-side y-axis so it
    // is not squashed against price. Regression guard: without Y2, an
    // RSI-ish indicator would render as a flat line near zero on a [100,
    // 200] price scale.
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'RSI-like', ownPanel: false }}
        result={{
          dates: ['2024-01-01', '2024-01-02', '2024-01-03'],
          series: [
            {
              label: 'close',
              collection: 'INDEX',
              instrument_id: '^GSPC',
              close: [100, 150, 200],
            },
          ],
          indicator: [1, 3, 5],
        }}
        loading={false}
        error={null}
      />,
    );
    const { traces, layoutOverrides } = chartProps[0];
    // Indicator trace pinned to yaxis2, price trace stays on default y.
    const [priceTrace, indTrace] = traces;
    expect(priceTrace.yaxis).toBeUndefined();
    expect(indTrace.yaxis).toBe('y2');
    // Layout has an overlaying secondary axis on the right — the Y2
    // signature specific to overlay mode (different from the ownPanel
    // stacked-subplot layout, which uses disjoint domains instead).
    expect(layoutOverrides.yaxis2).toBeDefined();
    expect(layoutOverrides.yaxis2.overlaying).toBe('y');
    expect(layoutOverrides.yaxis2.side).toBe('right');
  });

  it('does NOT fire the Y2 heuristic when indicator magnitude is large (overlay mode)', () => {
    // indAbsMax >= 10 → heuristic condition is false. Both traces must
    // stay on the default y-axis and no yaxis2 override may appear. This
    // is the symmetric counterpart of the previous test — if the
    // heuristic bar moves, one of these tests will flip.
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'Big-indicator', ownPanel: false }}
        result={{
          dates: ['2024-01-01', '2024-01-02', '2024-01-03'],
          series: [
            {
              label: 'close',
              collection: 'INDEX',
              instrument_id: '^GSPC',
              close: [100, 150, 200],
            },
          ],
          indicator: [50, 75, 120],
        }}
        loading={false}
        error={null}
      />,
    );
    const { traces, layoutOverrides } = chartProps[0];
    expect(traces.every((t) => !t.yaxis)).toBe(true);
    expect(layoutOverrides.yaxis2).toBeUndefined();
  });

  it('sets connectgaps=true on the indicator trace so sparse-output indicators render as a visible zigzag', () => {
    // Regression guard for sparse-output indicators (swing-pivots,
    // engulfment-pattern). These emit non-NaN at only ~5% of bars. With
    // mode='lines' and connectgaps=false, Plotly draws no line segment
    // between non-NaN points separated by NaN, so the indicator would be
    // effectively invisible. connectgaps=true bridges the NaN gaps so
    // consecutive non-NaN points form a visible zigzag. This must apply
    // in BOTH overlay and ownPanel branches.
    //
    // The price trace keeps connectgaps=false — real missing data in the
    // price series must remain as gaps (not interpolated).
    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'Sparse', ownPanel: false }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    const overlayTraces = chartProps[0].traces;
    const overlayPrice = overlayTraces[0];
    const overlayInd = overlayTraces[overlayTraces.length - 1];
    expect(overlayPrice.connectgaps).toBe(false);
    expect(overlayInd.connectgaps).toBe(true);

    cleanup();
    chartProps.length = 0;

    render(
      <IndicatorChart
        indicator={{ id: 'u1', name: 'Sparse', ownPanel: true }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    const splitTraces = chartProps[0].traces;
    const splitPrice = splitTraces[0];
    const splitInd = splitTraces[splitTraces.length - 1];
    expect(splitPrice.connectgaps).toBe(false);
    expect(splitInd.connectgaps).toBe(true);
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
