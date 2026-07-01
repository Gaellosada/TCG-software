// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks — declared before the component import so vitest hoists them.
// ---------------------------------------------------------------------------

// Mock Chart — captures props so tests can assert on them. Also stamps
// the trace count and marker count onto the DOM for quick assertions.
let capturedChartProps = null;
vi.mock('../../components/Chart', () => ({
  default: vi.fn((props) => {
    capturedChartProps = props;
    return (
      <div
        data-testid="chart"
        data-trace-count={props.traces.length}
        data-marker-count={Array.isArray(props.markers) ? props.markers.length : -1}
      />
    );
  }),
}));

// Mock OptionStreamForm — captures value/onChange for test driving.
let capturedFormProps = null;
vi.mock('../../components/OptionStreamForm', () => {
  const mock = vi.fn((props) => {
    capturedFormProps = props;
    return <div data-testid="option-stream-form" />;
  });
  mock.buildDefaultOptionStream = ({ availableRoots }) => ({
    type: 'option_stream',
    collection: availableRoots?.[0]?.collection || '',
    option_type: 'C',
    cycle: 'W3 Friday',
    maturity: { kind: 'next_third_friday', offset_months: 0 },
    selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
    stream: 'mid',
  });
  return {
    default: mock,
    buildDefaultOptionStream: mock.buildDefaultOptionStream,
  };
});

// Mock OptionDateRangeControl — captures value/onChange.
let capturedDateRangeProps = null;
vi.mock('../../components/OptionDateRangeControl', () => {
  const mock = vi.fn((props) => {
    capturedDateRangeProps = props;
    return <div data-testid="option-date-range-control" />;
  });
  return {
    default: mock,
    // New contract (PR #58): a plain 1-year window ending today. No presets.
    computeDefaultRange: () => ({ start: '2024-12-01', end: '2025-12-01' }),
  };
});

// Mock getOptionRoots and resolveOptionStream.
const mockRoots = {
  roots: [
    { collection: 'OPT_SP_500', root_label: 'S&P 500', has_greeks: true, last_trade_date: '2025-03-21' },
    { collection: 'OPT_AAPL', root_label: 'Apple', has_greeks: false, last_trade_date: '2025-03-20' },
  ],
};

let mockResolveResult = null;
let mockResolveError = null;
let capturedResolveArgs = null;

const mockGetOptionRoots = vi.fn(() => Promise.resolve(mockRoots));
const mockResolveOptionStream = vi.fn((...args) => {
  capturedResolveArgs = args;
  if (mockResolveError) return Promise.reject(mockResolveError);
  return Promise.resolve(mockResolveResult);
});

vi.mock('../../api/options', () => ({
  getOptionRoots: (...args) => mockGetOptionRoots(...args),
  resolveOptionStream: (...args) => mockResolveOptionStream(...args),
}));

// Import after mocks are set up
import ContinuousOptionsChart from './ContinuousOptionsChart';

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

beforeEach(() => {
  capturedFormProps = null;
  capturedDateRangeProps = null;
  capturedResolveArgs = null;
  capturedChartProps = null;
  mockGetOptionRoots.mockImplementation(() => Promise.resolve(mockRoots));
  mockResolveOptionStream.mockImplementation((...args) => {
    capturedResolveArgs = args;
    if (mockResolveError) return Promise.reject(mockResolveError);
    return Promise.resolve(mockResolveResult);
  });
  mockResolveResult = {
    dates: ['2025-01-02', '2025-01-03', '2025-01-06'],
    streams: {
      'MID / Call / by moneyness': {
        values: [100.5, 101.2, 99.8],
        diagnostics: ['ok', 'ok', 'ok'],
      },
    },
  };
  mockResolveError = null;
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ContinuousOptionsChart — controls de-clip (P1)', () => {
  // The options-stream controls were clipped below a `max-height: 220px;
  // overflow-y: auto` box (`.controlsCapped`), hiding the Selection picker
  // behind an unobvious inner scrollbar. The cap was removed so the whole form
  // (incl. ByDelta/ByMoneyness/ByStrike) is visible. Guard it at the CSS source:
  // `.controlsCapped` must not re-introduce a height cap / inner scroll.
  it('.controlsCapped has no max-height / overflow-y cap (form not clipped)', async () => {
    const fs = await import('node:fs');
    const path = await import('node:path');
    // Resolved from the frontend root (vitest cwd) — robust to the transform's
    // import.meta.url scheme.
    const cssPath = path.resolve(
      process.cwd(),
      'src/pages/Data/ChartBase.module.css',
    );
    const css = fs.readFileSync(cssPath, 'utf8');
    const block = css.slice(
      css.indexOf('.controlsCapped'),
      css.indexOf('}', css.indexOf('.controlsCapped')) + 1,
    );
    expect(block).toContain('.controlsCapped');
    expect(block).not.toMatch(/max-height/);
    expect(block).not.toMatch(/overflow-y/);
  });
});

describe('ContinuousOptionsChart — initial render', () => {
  it('shows loading state while roots are being fetched', () => {
    mockGetOptionRoots.mockReturnValueOnce(new Promise(() => {}));

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);
    expect(screen.getByText('Loading option roots...')).toBeTruthy();
  });

  it('renders the form and date range control after roots load', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('option-stream-form')).toBeTruthy();
    });

    expect(screen.getByTestId('option-date-range-control')).toBeTruthy();
    expect(screen.getByTestId('resolve-button')).toBeTruthy();
    expect(screen.getByText(/Configure the stream/)).toBeTruthy();
  });

  it('shows the collection in the title', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByText('OPT_SP_500 — Continuous Options')).toBeTruthy();
    });
  });

  it('passes availableRoots to OptionStreamForm', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(capturedFormProps).not.toBeNull();
    });
    expect(capturedFormProps.availableRoots).toEqual(mockRoots.roots);
  });
});

describe('ContinuousOptionsChart — roots error', () => {
  it('renders error when roots fail to load', async () => {
    mockGetOptionRoots.mockReturnValueOnce(Promise.reject(new Error('Network down')));

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to load option roots.*Network down/)).toBeTruthy();
    });
  });
});

describe('ContinuousOptionsChart — resolve flow', () => {
  it('calls resolveOptionStream with the correct arguments on Resolve click', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(capturedResolveArgs).not.toBeNull();
    });

    const [streams, start, end, opts] = capturedResolveArgs;
    expect(streams).toHaveLength(1);
    expect(streams[0].ref.collection).toBe('OPT_SP_500');
    expect(streams[0].label).toBeTruthy();
    expect(typeof start).toBe('string');
    expect(typeof end).toBe('string');
    expect(opts.signal).toBeTruthy();
    expect(typeof opts.onProgress).toBe('function');
  });

  it('renders the chart after successful resolution', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('chart')).toBeTruthy();
    });

    expect(screen.getByText(/3 points/)).toBeTruthy();
    expect(screen.getByText(/2025-01-02 to 2025-01-06/)).toBeTruthy();
  });

  it('renders error state on resolution failure', async () => {
    mockResolveError = new Error('Server error: 500');

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('resolve-error')).toBeTruthy();
    });
    expect(screen.getByText(/Server error: 500/)).toBeTruthy();
  });
});

describe('ContinuousOptionsChart — maturity snap notice (Issue #2 D2)', () => {
  async function resolveAndWaitForChart() {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);
    await waitFor(() => expect(screen.getByTestId('resolve-button')).toBeTruthy());
    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });
    await waitFor(() => expect(screen.getByTestId('chart')).toBeTruthy());
  }

  it('shows a snap notice naming the snapped-to expiration when diagnostics carry snapped_to', async () => {
    mockResolveResult = {
      dates: ['2025-01-02', '2025-01-03', '2025-01-06'],
      streams: {
        'MID / Call / end of month': {
          values: [100.5, 101.2, 99.8],
          diagnostics: [
            'snapped_to:2025-01-17',
            'snapped_to:2025-01-17',
            'snapped_to:2025-01-17',
          ],
        },
      },
    };
    await resolveAndWaitForChart();
    const notice = await screen.findByTestId('snap-notice');
    expect(notice).toBeTruthy();
    // The notice names the (unique) snapped-to expiration date.
    expect(notice.textContent).toMatch(/2025-01-17/);
    expect(notice.textContent.toLowerCase()).toMatch(/snap/);
  });

  it('dedupes and lists multiple distinct snapped-to expirations', async () => {
    mockResolveResult = {
      dates: ['2025-01-02', '2025-02-03', '2025-02-04'],
      streams: {
        'MID / Call / end of month': {
          values: [100.5, 101.2, 99.8],
          diagnostics: [
            'snapped_to:2025-01-17',
            'snapped_to:2025-02-21',
            'snapped_to:2025-02-21',
          ],
        },
      },
    };
    await resolveAndWaitForChart();
    const notice = await screen.findByTestId('snap-notice');
    expect(notice.textContent).toMatch(/2025-01-17/);
    expect(notice.textContent).toMatch(/2025-02-21/);
    // 2025-02-21 appears once despite two diagnostic entries (deduped).
    expect(notice.textContent.match(/2025-02-21/g)).toHaveLength(1);
  });

  it('shows NO snap notice when no diagnostic is a snap', async () => {
    // Default mockResolveResult has diagnostics ['ok','ok','ok'].
    await resolveAndWaitForChart();
    expect(screen.queryByTestId('snap-notice')).toBeNull();
  });

  it('sends the type field in the ref for backend OptionStreamRef validation', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(capturedResolveArgs).not.toBeNull();
    });

    const ref = capturedResolveArgs[0][0].ref;
    expect(ref.type).toBe('option_stream');
  });
});

describe('ContinuousOptionsChart — empty result', () => {
  it('shows "No data" message when result has empty dates', async () => {
    mockResolveResult = { dates: [], streams: {} };

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByText(/No data returned/)).toBeTruthy();
    });
  });
});

describe('ContinuousOptionsChart — button states', () => {
  it('shows "Resolving..." text while loading', async () => {
    let resolvePromise;
    mockResolveOptionStream.mockImplementationOnce((...args) => {
      capturedResolveArgs = args;
      return new Promise((resolve) => { resolvePromise = resolve; });
    });

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    act(() => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button').textContent).toMatch(/Resolving/);
    });

    // Resolve to clean up
    await act(async () => {
      resolvePromise(mockResolveResult);
    });
  });
});

describe('ContinuousOptionsChart — roll markers', () => {
  function sold(overrides = {}) {
    return {
      contract_id: 'OPT_OLD',
      root: 'IND_SP_500',
      expiration: '2024-04-19',
      strike: 4500,
      type: 'C',
      value: 12.35,
      ...overrides,
    };
  }
  function bought(overrides = {}) {
    return {
      contract_id: 'OPT_NEW',
      root: 'IND_SP_500',
      expiration: '2024-05-17',
      strike: 4500,
      type: 'C',
      value: 13.10,
      ...overrides,
    };
  }

  it('passes an empty markers array when result has no rolls field', async () => {
    // mockResolveResult already has no `rolls` field by default.
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('chart')).toBeTruthy();
    });

    expect(capturedChartProps).not.toBeNull();
    expect(Array.isArray(capturedChartProps.markers)).toBe(true);
    expect(capturedChartProps.markers).toHaveLength(0);
  });

  it('flattens a single roll event into two markers (sell + buy)', async () => {
    mockResolveResult = {
      ...mockResolveResult,
      rolls: {
        'MID / Call / by moneyness': [
          { date: '2025-01-03', sold: sold(), bought: bought() },
        ],
      },
    };

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('chart')).toBeTruthy();
    });

    const markers = capturedChartProps.markers;
    expect(markers).toHaveLength(2);

    const sell = markers.find((m) => m.kind === 'sell');
    const buy = markers.find((m) => m.kind === 'buy');
    expect(sell).toBeTruthy();
    expect(buy).toBeTruthy();

    expect(sell.x).toBe('2025-01-03');
    expect(sell.y).toBe(12.35);
    expect(sell.tooltip.contract_id).toBe('OPT_OLD');

    expect(buy.x).toBe('2025-01-03');
    expect(buy.y).toBe(13.10);
    expect(buy.tooltip.contract_id).toBe('OPT_NEW');
  });

  it('skips marker entries whose value is null (cannot pin a Y position)', async () => {
    mockResolveResult = {
      ...mockResolveResult,
      rolls: {
        'MID / Call / by moneyness': [
          // Sell side has null value → only the buy-side marker is emitted.
          { date: '2025-01-03', sold: sold({ value: null }), bought: bought() },
          // Both sides have null value → no markers emitted.
          { date: '2025-01-06', sold: sold({ value: null }), bought: bought({ value: null }) },
        ],
      },
    };

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('chart')).toBeTruthy();
    });

    const markers = capturedChartProps.markers;
    expect(markers).toHaveLength(1);
    expect(markers[0].kind).toBe('buy');
    expect(markers[0].x).toBe('2025-01-03');
  });

  it('flattens rolls across multiple stream labels into one flat array', async () => {
    mockResolveResult = {
      dates: ['2025-01-02', '2025-01-03', '2025-01-06'],
      streams: {
        'Stream A': { values: [100.5, 101.2, 99.8], diagnostics: ['ok', 'ok', 'ok'] },
        'Stream B': { values: [50.1, 51.0, 49.5], diagnostics: ['ok', 'ok', 'ok'] },
      },
      rolls: {
        'Stream A': [{ date: '2025-01-03', sold: sold(), bought: bought() }],
        'Stream B': [{ date: '2025-01-06', sold: sold(), bought: bought() }],
      },
    };

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('chart')).toBeTruthy();
    });

    expect(capturedChartProps.markers).toHaveLength(4);
  });

  it('passes empty markers when rolls is present but empty for every label', async () => {
    mockResolveResult = {
      ...mockResolveResult,
      rolls: {
        'MID / Call / by moneyness': [],
      },
    };

    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(screen.getByTestId('resolve-button')).toBeTruthy();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('resolve-button'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('chart')).toBeTruthy();
    });

    expect(capturedChartProps.markers).toHaveLength(0);
  });
});

describe('ContinuousOptionsChart — default date range', () => {
  it('initialises the date range control with the default 1-year window', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(capturedDateRangeProps).not.toBeNull();
    });

    // computeDefaultRange() is mocked to return this window.
    expect(capturedDateRangeProps.value).toEqual({ start: '2024-12-01', end: '2025-12-01' });
  });

  it('no longer passes an anchorEnd prop (presets removed in PR #58)', async () => {
    render(<ContinuousOptionsChart collection="OPT_SP_500" />);

    await waitFor(() => {
      expect(capturedDateRangeProps).not.toBeNull();
    });

    expect(capturedDateRangeProps.anchorEnd).toBeUndefined();
  });
});
