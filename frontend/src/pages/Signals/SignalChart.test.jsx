// @vitest-environment jsdom
//
// Tests for SignalChart's position-only fallback path.
//
// When the signal spec references no instrument operand, the backend emits
// `result.price === null` (or omits the field entirely). In that case the
// component must render ONLY the stacked position subplot — no markers
// pane, no price trace, no crash. These tests lock down that contract.
//
// The shared Chart component is mocked with a minimal stub that captures
// traces + layoutOverrides so assertions don't depend on Plotly being
// usable inside jsdom.

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

// Import AFTER vi.mock so the stub is wired.
import SignalChart from './SignalChart';

function makePositionOnlyResult(overrides = {}) {
  return {
    index: ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'],
    position: [0, 1, 1, 0],
    long_score: [0, 1, 1, 0],
    short_score: [0, 0, 0, 0],
    entries_long: [1],
    exits_long: [3],
    entries_short: [],
    exits_short: [],
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

describe('<SignalChart> — position-only fallback (no instrument operand)', () => {
  it('renders the position-only pane when result.price is null', () => {
    const result = makePositionOnlyResult({ price: null });
    render(<SignalChart result={result} loading={false} error={null} />);

    // The position-only testid is present; the full-chart testid is not.
    expect(screen.getByTestId('signal-chart-position-only')).toBeDefined();
    expect(screen.queryByTestId('signal-chart-full')).toBeNull();

    // Exactly one trace reaches Chart: the position series. No price line,
    // no entry/exit marker traces.
    expect(chartProps).toHaveLength(1);
    expect(chartProps[0].traces).toHaveLength(1);
    expect(chartProps[0].traces[0].name).toBe('Position');

    // Layout has no yaxis2 subplot (no stacked price pane).
    expect(chartProps[0].layoutOverrides.yaxis2).toBeUndefined();
    // Position trace uses y (not y2) when price is absent.
    expect(chartProps[0].traces[0].yaxis).toBe('y');

    // No render errors.
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it('behaves identically when the price key is absent entirely', () => {
    const result = makePositionOnlyResult(); // no `price` key at all
    render(<SignalChart result={result} loading={false} error={null} />);

    expect(screen.getByTestId('signal-chart-position-only')).toBeDefined();
    expect(screen.queryByTestId('signal-chart-full')).toBeNull();

    expect(chartProps).toHaveLength(1);
    expect(chartProps[0].traces).toHaveLength(1);
    expect(chartProps[0].traces[0].name).toBe('Position');
    expect(chartProps[0].layoutOverrides.yaxis2).toBeUndefined();

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it('shows a muted subtitle in position-only mode', () => {
    const result = makePositionOnlyResult({ price: null });
    render(<SignalChart result={result} loading={false} error={null} />);
    const subtitle = screen.getByTestId('signal-chart-subtitle');
    expect(subtitle).toBeDefined();
    expect(subtitle.textContent).toMatch(/No instrument operand/i);
    expect(subtitle.textContent).toMatch(/price overlay hidden/i);
  });

  it('does not show the subtitle when price is present', () => {
    const result = makePositionOnlyResult({
      price: { label: 'AAPL.close', values: [100, 101, 102, 103] },
    });
    render(<SignalChart result={result} loading={false} error={null} />);
    expect(screen.queryByTestId('signal-chart-subtitle')).toBeNull();
  });

  it('renders the full (price + markers) pane when result.price is present', () => {
    // Sanity check: the non-null branch still works and exposes the other
    // testid. This guards the null-branch assertions above against
    // accidentally always passing.
    const result = makePositionOnlyResult({
      price: { label: 'AAPL close', values: [100, 101, 102, 103] },
    });
    render(<SignalChart result={result} loading={false} error={null} />);

    expect(screen.getByTestId('signal-chart-full')).toBeDefined();
    expect(screen.queryByTestId('signal-chart-position-only')).toBeNull();

    // Price trace + 4 marker traces + position trace = 6 traces total.
    expect(chartProps).toHaveLength(1);
    expect(chartProps[0].traces.length).toBeGreaterThan(1);
    expect(chartProps[0].layoutOverrides.yaxis2).toBeDefined();

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });
});
