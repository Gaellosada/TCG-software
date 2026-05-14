// @vitest-environment jsdom
//
// Tests for the shared Chart component's trace-pass-through contract.
//
// Why this test exists
// --------------------
// Many pages build elaborate trace objects — indicator markers with
// ``symbol``/``size``/``line`` styling, volume bars with custom colors,
// equity curves with ``fill``, etc. These pages rely on the shared Chart
// wrapper forwarding the ``traces`` array to Plotly UNMODIFIED. If Chart
// ever grows a normalization/whitelist step that drops unknown marker
// sub-fields, every sparse-output indicator (e.g. swing-pivots) would
// silently lose its visible styling and regress to default dots.
//
// These tests pin the contract: the ``data`` prop Plotly receives is
// referentially the same array (or, at minimum, field-for-field equal
// for styling-relevant keys) as what the caller passed in.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';

// Capture whatever react-plotly.js receives without pulling real Plotly
// into jsdom (Plotly needs a real browser environment).
const plotProps = [];
vi.mock('react-plotly.js', () => {
  // eslint-disable-next-line react/prop-types
  function PlotStub(props) {
    plotProps.push(props);
    return <div data-testid="plot-stub" />;
  }
  return { default: PlotStub };
});

// Import AFTER vi.mock so the stub is wired.
import Chart from './Chart';
import { getChartColors } from '../../utils/chartTheme';

afterEach(() => {
  cleanup();
  plotProps.length = 0;
  // Reset theme back to default for isolation across tests.
  delete document.documentElement.dataset.theme;
});

describe('Chart — trace pass-through', () => {
  it('forwards the traces array verbatim to Plotly as the data prop', () => {
    const traces = [
      { x: [1, 2, 3], y: [10, 11, 12], type: 'scatter', mode: 'lines' },
    ];
    render(<Chart traces={traces} />);
    expect(plotProps).toHaveLength(1);
    // Strict identity: the component should NOT clone, normalize, or
    // whitelist — it must pass the caller's array through directly.
    expect(plotProps[0].data).toBe(traces);
  });

  it('preserves marker.symbol, marker.size, and marker.line sub-fields (sparse-indicator styling)', () => {
    // Regression guard matching the IndicatorChart swing-pivots fix
    // (commit 2c2d4c3). If someone later introduces a trace-normalizer
    // in Chart that drops unknown marker fields, this fails loudly.
    const traces = [
      {
        x: ['2024-01-01', '2024-01-02'],
        y: [100, 200],
        type: 'scatter',
        mode: 'markers',
        marker: {
          color: '#f59e0b',
          size: 10,
          symbol: 'diamond',
          line: { color: '#1a1a1a', width: 1 },
        },
      },
    ];
    render(<Chart traces={traces} />);
    const fwd = plotProps[0].data[0];
    expect(fwd.marker).toBeDefined();
    expect(fwd.marker.symbol).toBe('diamond');
    expect(fwd.marker.size).toBe(10);
    expect(fwd.marker.line).toEqual({ color: '#1a1a1a', width: 1 });
    expect(fwd.marker.color).toBe('#f59e0b');
  });

  it('does not mutate the input traces array or its trace objects', () => {
    // Adjacent invariant: even if Chart grows a transform later, it
    // must not mutate caller state (useMemo identity, etc).
    const marker = { symbol: 'diamond', size: 10, line: { color: '#000', width: 1 } };
    const traces = [{ x: [1], y: [2], type: 'scatter', mode: 'markers', marker }];
    const snapshot = JSON.stringify(traces);
    render(<Chart traces={traces} />);
    expect(JSON.stringify(traces)).toBe(snapshot);
    // Marker object reference preserved too.
    expect(plotProps[0].data[0].marker).toBe(marker);
  });
});

// ---------------------------------------------------------------------------
// markers prop — kind-discriminated overlay points (option rolls, etc).
//
// The added prop is OPTIONAL. When undefined/empty, the existing
// pass-through identity invariant above MUST still hold. When non-empty,
// Chart appends synthesized scatter traces to `traces` before handing
// off to Plotly. The synthesized traces come from the shared
// `chartMarkers` helper — Chart itself does no per-kind branching.
// ---------------------------------------------------------------------------

function sellMarker(overrides = {}) {
  return {
    x: '2024-03-15',
    y: 12.35,
    kind: 'sell',
    tooltip: {
      contract_id: 'OPT_X',
      root: 'IND_SP_500',
      expiration: '2024-04-19',
      strike: 4500,
      type: 'C',
      value: 12.35,
    },
    ...overrides,
  };
}
function buyMarker(overrides = {}) {
  return {
    x: '2024-03-15',
    y: 13.10,
    kind: 'buy',
    tooltip: {
      contract_id: 'OPT_Y',
      root: 'IND_SP_500',
      expiration: '2024-05-17',
      strike: 4500,
      type: 'C',
      value: 13.10,
    },
    ...overrides,
  };
}

describe('Chart — markers prop', () => {
  it('preserves the identity invariant when markers is undefined', () => {
    const traces = [{ x: [1, 2], y: [3, 4], type: 'scatter', mode: 'lines' }];
    render(<Chart traces={traces} />);
    // No `markers` prop → data must be referentially the caller's array.
    expect(plotProps[0].data).toBe(traces);
  });

  it('preserves the identity invariant when markers is an empty array', () => {
    const traces = [{ x: [1, 2], y: [3, 4], type: 'scatter', mode: 'lines' }];
    render(<Chart traces={traces} markers={[]} />);
    expect(plotProps[0].data).toBe(traces);
  });

  it('appends one extra trace when only sell markers are provided', () => {
    const traces = [{ x: [1, 2], y: [3, 4], type: 'scatter', mode: 'lines' }];
    render(<Chart traces={traces} markers={[sellMarker()]} />);
    expect(plotProps[0].data).toHaveLength(traces.length + 1);
    // Existing traces still pass through verbatim.
    expect(plotProps[0].data[0]).toBe(traces[0]);
    expect(plotProps[0].data[1].marker.symbol).toBe('circle-open');
    expect(plotProps[0].data[1].name).toBe('Roll — close');
  });

  it('appends one extra trace when only buy markers are provided', () => {
    const traces = [{ x: [1, 2], y: [3, 4], type: 'scatter', mode: 'lines' }];
    render(<Chart traces={traces} markers={[buyMarker()]} />);
    expect(plotProps[0].data).toHaveLength(traces.length + 1);
    expect(plotProps[0].data[0]).toBe(traces[0]);
    expect(plotProps[0].data[1].marker.symbol).toBe('circle');
    expect(plotProps[0].data[1].name).toBe('Roll — open');
  });

  it('appends two extra traces when both sells and buys are provided', () => {
    const traces = [{ x: [1, 2], y: [3, 4], type: 'scatter', mode: 'lines' }];
    render(
      <Chart traces={traces} markers={[sellMarker(), buyMarker()]} />,
    );
    expect(plotProps[0].data).toHaveLength(traces.length + 2);
    expect(plotProps[0].data[0]).toBe(traces[0]);
    // BUY comes first, SELL last — pinned by MARKER_STYLE insertion order so
    // the hollow sell ring renders ON TOP of the filled buy dot at overlap
    // (CONTRACT §C "ring-around-dot"). See z-order regression test below.
    expect(plotProps[0].data[1].marker.symbol).toBe('circle');
    expect(plotProps[0].data[2].marker.symbol).toBe('circle-open');
  });

  it('renders the SELL trace LAST so the hollow ring overlays the filled dot at overlap (CONTRACT §C)', () => {
    // Regression pin for the overlap "ring-around-dot" rule. Plotly draws
    // later traces in `data` ON TOP, so when a sell and a buy marker share
    // (x, y) the sell trace must come AFTER the buy trace in `data`,
    // otherwise the filled circle occludes the hollow ring and the
    // overlap becomes visually indistinguishable from a buy-only point.
    const traces = [{ x: [1, 2], y: [3, 4], type: 'scatter', mode: 'lines' }];
    // Same x and y for both markers — the overlap case the contract pins.
    const x = '2024-03-15';
    const y = 12.35;
    render(
      <Chart
        traces={traces}
        markers={[sellMarker({ x, y }), buyMarker({ x, y })]}
      />,
    );
    const data = plotProps[0].data;
    // Last trace must be the sell (hollow) so it renders on top.
    expect(data[data.length - 1].marker.symbol).toBe('circle-open');
    expect(data[data.length - 1].name).toBe('Roll — close');
    // And the buy (filled) sits one slot earlier, underneath.
    expect(data[data.length - 2].marker.symbol).toBe('circle');
    expect(data[data.length - 2].name).toBe('Roll — open');
  });

  it('wires both marker traces into the same legendgroup with showlegend', () => {
    const traces = [{ x: [1, 2], y: [3, 4], type: 'scatter', mode: 'lines' }];
    render(
      <Chart traces={traces} markers={[sellMarker(), buyMarker()]} />,
    );
    // Order: traces[0] is the user line, then buy, then sell (z-order pin).
    const [, buyTrace, sellTrace] = plotProps[0].data;
    expect(sellTrace.legendgroup).toBe('roll-markers');
    expect(buyTrace.legendgroup).toBe('roll-markers');
    expect(sellTrace.showlegend).toBe(true);
    expect(buyTrace.showlegend).toBe(true);
  });

  it('picks marker color from the dark palette when data-theme=dark', () => {
    // Set BEFORE render so useTheme reads it on mount.
    document.documentElement.dataset.theme = 'dark';
    const traces = [{ x: [1], y: [2], type: 'scatter', mode: 'lines' }];
    const dark = getChartColors('dark');
    render(
      <Chart traces={traces} markers={[sellMarker(), buyMarker()]} />,
    );
    // Order: user line, then buy, then sell (z-order pin).
    const [, buyTrace, sellTrace] = plotProps[0].data;
    expect(sellTrace.marker.color).toBe(dark.markerSell);
    expect(buyTrace.marker.color).toBe(dark.markerBuy);
  });

  it('picks marker color from the light palette by default (no data-theme)', () => {
    // useTheme falls back to 'light' when data-theme is not set.
    const traces = [{ x: [1], y: [2], type: 'scatter', mode: 'lines' }];
    const light = getChartColors('light');
    const dark = getChartColors('dark');
    render(
      <Chart traces={traces} markers={[sellMarker(), buyMarker()]} />,
    );
    // Order: user line, then buy, then sell (z-order pin).
    const [, buyTrace, sellTrace] = plotProps[0].data;
    expect(sellTrace.marker.color).toBe(light.markerSell);
    expect(buyTrace.marker.color).toBe(light.markerBuy);
    // Sanity: ensure the light values are not accidentally the dark ones.
    expect(sellTrace.marker.color).not.toBe(dark.markerSell);
  });
});
