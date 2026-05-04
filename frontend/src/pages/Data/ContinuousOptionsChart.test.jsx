// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks — declared before the component import so vitest hoists them.
// ---------------------------------------------------------------------------

// Mock Chart — just renders a placeholder.
vi.mock('../../components/Chart', () => ({
  default: vi.fn(({ traces }) => (
    <div data-testid="chart" data-trace-count={traces.length} />
  )),
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
    computePresetRange: (preset) => {
      const months = { '3m': 3, '6m': 6, '1y': 12, '2y': 24 }[preset] || 6;
      return { start: `2025-${String(12 - months + 1).padStart(2, '0')}-01`, end: '2025-12-01' };
    },
    DEFAULT_PRESET: '6m',
  };
});

// Mock getOptionRoots and resolveOptionStream.
const mockRoots = {
  roots: [
    { collection: 'OPT_SP_500', root_label: 'S&P 500', has_greeks: true },
    { collection: 'OPT_AAPL', root_label: 'Apple', has_greeks: false },
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
