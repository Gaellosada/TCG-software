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
// sub-fields, every sparse-output indicator (swing-pivots,
// engulfment-pattern) would silently lose its visible styling and regress
// to default dots.
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

afterEach(() => {
  cleanup();
  plotProps.length = 0;
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
