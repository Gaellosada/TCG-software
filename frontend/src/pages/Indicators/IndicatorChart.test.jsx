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

  it('applies visibility-boosted marker styling when chartMode is markers', () => {
    // Regression guard for sparse-output indicators (swing-pivots,
    // engulfment-pattern). These emit values at only ~5% of bars as
    // markers layered on top of the price line — at Plotly's default
    // size=6 with no border, a small amber dot on a thin price line
    // is visually absorbed and the user sees nothing. The chart layer
    // must therefore bump the size, switch to a distinctive symbol,
    // and add a contrasting border when chartMode === 'markers' so the
    // points pop against the price line regardless of theme.
    render(
      <IndicatorChart
        indicator={{
          id: 'pivots',
          name: 'Swing Pivots',
          ownPanel: false,
          chartMode: 'markers',
        }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    const traces = chartProps[0].traces;
    const indTrace = traces[traces.length - 1];
    expect(indTrace.mode).toBe('markers');
    // Size must be > 6 (default) so markers are actually visible over
    // price; this is the core visibility requirement.
    expect(indTrace.marker.size).toBeGreaterThan(6);
    // Distinctive symbol (anything non-default-circle is acceptable —
    // the regression we're guarding against is "looks like a tiny
    // default dot indistinguishable from noise").
    expect(indTrace.marker.symbol).toBeDefined();
    expect(indTrace.marker.symbol).not.toBe('circle');
    // Border must exist with a visible stroke — a border is what
    // separates the marker from the price line it sits on top of.
    expect(indTrace.marker.line).toBeDefined();
    expect(indTrace.marker.line.width).toBeGreaterThan(0);
    expect(typeof indTrace.marker.line.color).toBe('string');
    expect(indTrace.marker.line.color.length).toBeGreaterThan(0);
  });

  it('keeps default thin-line marker styling when chartMode is lines (unchanged behaviour)', () => {
    // Symmetric counterpart: the sparse-output styling MUST NOT bleed
    // into the default 'lines' path. Most indicators render as a
    // continuous line and their marker style is irrelevant, but if we
    // accidentally set a large diamond marker globally it would appear
    // at every data point on a line trace, which is unwanted.
    render(
      <IndicatorChart
        indicator={{ id: 'rsi', name: 'RSI', ownPanel: false }}
        result={makeResult()}
        loading={false}
        error={null}
      />,
    );
    const traces = chartProps[0].traces;
    const indTrace = traces[traces.length - 1];
    expect(indTrace.mode).toBe('lines');
    // Line-mode marker stays at the historical default (size 6, no
    // symbol override, no border) — documented as the baseline style.
    expect(indTrace.marker.size).toBe(6);
    expect(indTrace.marker.symbol).toBeUndefined();
    expect(indTrace.marker.line).toBeUndefined();
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
