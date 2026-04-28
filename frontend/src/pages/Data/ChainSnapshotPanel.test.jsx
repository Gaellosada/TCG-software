// @vitest-environment jsdom
//
// Tests for ChainSnapshotPanel.
//
// Mocks:
//   - ../../components/Chart     : minimal stub capturing props (avoids Plotly)
//   - ../../api/options          : getChainSnapshot returns controlled payloads
//   - ../../hooks/useAsync       : driven via mocked getChainSnapshot; NOT directly
//                                  mocked — we let useAsync call the mocked api fn.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Chart stub — captures props to chartCalls array
// ---------------------------------------------------------------------------

const chartCalls = [];

vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub({ traces, layoutOverrides, downloadFilename }) {
    chartCalls.push({ traces, layoutOverrides, downloadFilename });
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

// ---------------------------------------------------------------------------
// API mock — getChainSnapshot is replaceable per-test via resolveWith
// ---------------------------------------------------------------------------

const mockGetChainSnapshot = vi.fn();

vi.mock('../../api/options', () => ({
  getChainSnapshot: (...args) => mockGetChainSnapshot(...args),
}));

// Import AFTER vi.mock declarations so stubs are wired.
import ChainSnapshotPanel from './ChainSnapshotPanel';

// ---------------------------------------------------------------------------
// Sample payload builders
// ---------------------------------------------------------------------------

function cr(value) {
  return {
    value,
    source: value === null ? 'missing' : 'stored',
    model: null,
    inputs_used: null,
    missing_inputs: value === null ? ['iv'] : null,
    error_code: value === null ? 'missing_iv' : null,
    error_detail: null,
  };
}

/**
 * Build a minimal ChainSnapshotResponse with one SmileSeries.
 * @param {Array<{strike, K_over_S, value}>} pts - point descriptors
 * @param {string} expiration
 */
function makeResponse(pts, expiration = '2024-04-19') {
  return {
    root: 'OPT_SP_500',
    date: '2024-03-15',
    underlying_price: cr(5500),
    series: [
      {
        expiration,
        points: pts.map(({ strike, K_over_S, value }) => ({
          strike,
          K_over_S,
          value: cr(value),
        })),
      },
    ],
  };
}

const DEFAULT_PROPS = {
  root: 'OPT_SP_500',
  date: '2024-03-15',
  type: 'C',
  expiration: '2024-04-19',
  onClose: () => {},
};

const SAMPLE_PTS = [
  { strike: 4900, K_over_S: 0.89, value: 0.25 },
  { strike: 5000, K_over_S: 0.91, value: 0.20 },
  { strike: 5100, K_over_S: 0.93, value: 0.17 },
];

// ---------------------------------------------------------------------------
// Reset between tests
// ---------------------------------------------------------------------------

beforeEach(() => {
  mockGetChainSnapshot.mockReset();
  chartCalls.length = 0;
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('<ChainSnapshotPanel> basic render', () => {
  it('shows loading state while fetch is in-flight', async () => {
    // Never resolve — stays loading.
    mockGetChainSnapshot.mockReturnValue(new Promise(() => {}));
    render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    expect(screen.getByText(/loading snapshot/i)).toBeTruthy();
  });

  it('shows error message on fetch failure', async () => {
    mockGetChainSnapshot.mockRejectedValue(new Error('network down'));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });
    expect(screen.getByText(/failed to load snapshot/i)).toBeTruthy();
    expect(screen.getByText(/network down/)).toBeTruthy();
  });

  it('renders the Chart with one trace on successful response', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });

    expect(chartCalls.length).toBeGreaterThan(0);
    const last = chartCalls[chartCalls.length - 1];
    expect(Array.isArray(last.traces)).toBe(true);
    expect(last.traces).toHaveLength(1);
    expect(last.traces[0].name).toBe('IV');
  });

  it('trace has mode lines+markers and connectgaps false', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });
    const last = chartCalls[chartCalls.length - 1];
    const trace = last.traces[0];
    expect(trace.mode).toBe('lines+markers');
    expect(trace.connectgaps).toBe(false);
  });

  it('shows "no data" empty state when series is empty', async () => {
    mockGetChainSnapshot.mockResolvedValue({
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: cr(5500),
      series: [],
    });
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });
    expect(screen.queryByTestId('chart-stub')).toBeNull();
    expect(screen.getByText(/no data for this expiration/i)).toBeTruthy();
  });
});

describe('<ChainSnapshotPanel> field toggle (iv → delta)', () => {
  it('defaults to iv field on first render', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });
    // First call should have field='iv'
    const firstCall = mockGetChainSnapshot.mock.calls[0];
    expect(firstCall[1].field).toBe('iv');
  });

  it('toggling to Delta re-fetches with field=delta', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });

    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /delta/i }));
    });

    // Last call should have field='delta'
    const calls = mockGetChainSnapshot.mock.calls;
    const lastCall = calls[calls.length - 1];
    expect(lastCall[1].field).toBe('delta');
  });

  it('trace name changes to Delta after field toggle', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });

    // Delta response
    mockGetChainSnapshot.mockResolvedValue(makeResponse(
      SAMPLE_PTS.map((p) => ({ ...p, value: p.strike < 5000 ? 0.7 : 0.5 })),
    ));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /delta/i }));
    });

    const last = chartCalls[chartCalls.length - 1];
    expect(last.traces[0].name).toBe('Delta');
  });
});

describe('<ChainSnapshotPanel> xAxis toggle (strike → K/S)', () => {
  it('uses strike values on x by default', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });

    const last = chartCalls[chartCalls.length - 1];
    expect(last.traces[0].x).toEqual([4900, 5000, 5100]);
  });

  it('switching to K/S uses K_over_S values on x without re-fetching', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });

    const callsBefore = mockGetChainSnapshot.mock.calls.length;

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /K\/S/i }));
    });

    // No new API call — xAxis toggle is purely client-side.
    expect(mockGetChainSnapshot.mock.calls.length).toBe(callsBefore);

    const last = chartCalls[chartCalls.length - 1];
    expect(last.traces[0].x).toEqual([0.89, 0.91, 0.93]);
  });
});

describe('<ChainSnapshotPanel> missing values (connectgaps)', () => {
  it('null value points appear as null y — no line break attempted', async () => {
    // Middle point has null value → should be null in y array.
    const pts = [
      { strike: 4900, K_over_S: 0.89, value: 0.25 },
      { strike: 5000, K_over_S: 0.91, value: null },  // missing
      { strike: 5100, K_over_S: 0.93, value: 0.17 },
    ];
    mockGetChainSnapshot.mockResolvedValue(makeResponse(pts));
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} />);
    });

    const last = chartCalls[chartCalls.length - 1];
    const trace = last.traces[0];
    expect(trace.y[1]).toBeNull();
    expect(trace.connectgaps).toBe(false);
  });
});

describe('<ChainSnapshotPanel> close button', () => {
  it('clicking Close calls onClose', async () => {
    mockGetChainSnapshot.mockResolvedValue(makeResponse(SAMPLE_PTS));
    const onClose = vi.fn();
    await act(async () => {
      render(<ChainSnapshotPanel {...DEFAULT_PROPS} onClose={onClose} />);
    });
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
