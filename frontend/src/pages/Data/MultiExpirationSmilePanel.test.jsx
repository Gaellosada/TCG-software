// @vitest-environment jsdom
//
// Tests for MultiExpirationSmilePanel.
//
// Mocks:
//   - ../../components/Chart : captures props to assert on traces.
//   - ../../api/options       : controls getChainSnapshot responses.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';

// Captured Chart props per render.
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

const mockGetChainSnapshot = vi.fn();

vi.mock('../../api/options', () => ({
  getChainSnapshot: (...args) => mockGetChainSnapshot(...args),
}));

import MultiExpirationSmilePanel, { SMILE_PALETTE } from './MultiExpirationSmilePanel';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeSeries(expirations, hasNull = false) {
  return expirations.map((exp, i) => ({
    expiration: exp,
    strikes: [100, 110, 120],
    values: hasNull
      ? [{ value: 0.2 + i * 0.01, source: 'stored' }, null, { value: 0.22 + i * 0.01, source: 'stored' }]
      : [
          { value: 0.2 + i * 0.01, source: 'stored' },
          { value: 0.21 + i * 0.01, source: 'stored' },
          { value: 0.22 + i * 0.01, source: 'stored' },
        ],
  }));
}

const EXPIRATIONS_3 = ['2024-04-19', '2024-05-17', '2024-06-21'];

// ---------------------------------------------------------------------------

beforeEach(() => {
  chartProps.length = 0;
  mockGetChainSnapshot.mockReset();
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('<MultiExpirationSmilePanel> 3 expirations → 3 traces with distinct colors', () => {
  it('renders 3 traces each with a distinct color from SMILE_PALETTE', async () => {
    mockGetChainSnapshot.mockResolvedValueOnce({ series: makeSeries(EXPIRATIONS_3) });

    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={EXPIRATIONS_3}
          onClose={() => {}}
        />,
      );
    });

    const last = chartProps[chartProps.length - 1];
    expect(last).toBeTruthy();
    expect(last.traces).toHaveLength(3);

    const colors = last.traces.map((t) => t.line.color);
    // All distinct.
    const uniqueColors = new Set(colors);
    expect(uniqueColors.size).toBe(3);
    // Colors come from the palette.
    for (const c of colors) {
      expect(SMILE_PALETTE).toContain(c);
    }
  });

  it('labels each trace with its expiration date', async () => {
    mockGetChainSnapshot.mockResolvedValueOnce({ series: makeSeries(EXPIRATIONS_3) });

    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={EXPIRATIONS_3}
          onClose={() => {}}
        />,
      );
    });

    const last = chartProps[chartProps.length - 1];
    const names = last.traces.map((t) => t.name);
    for (const exp of EXPIRATIONS_3) {
      expect(names).toContain(exp);
    }
  });
});

describe('<MultiExpirationSmilePanel> field toggle re-fetches', () => {
  it('clicking Δ toggle re-calls getChainSnapshot with field="delta"', async () => {
    mockGetChainSnapshot.mockResolvedValue({ series: makeSeries(EXPIRATIONS_3) });

    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={EXPIRATIONS_3}
          onClose={() => {}}
        />,
      );
    });

    // First call was with iv (default).
    expect(mockGetChainSnapshot).toHaveBeenCalledTimes(1);
    expect(mockGetChainSnapshot).toHaveBeenCalledWith(
      'OPT_SP_500',
      expect.objectContaining({ field: 'iv' }),
    );

    await act(async () => {
      fireEvent.click(screen.getByText('Δ'));
    });

    expect(mockGetChainSnapshot).toHaveBeenCalledTimes(2);
    expect(mockGetChainSnapshot).toHaveBeenLastCalledWith(
      'OPT_SP_500',
      expect.objectContaining({ field: 'delta' }),
    );
  });
});

describe('<MultiExpirationSmilePanel> empty expirations', () => {
  it('renders a hint instead of fetching when expirations array is empty', async () => {
    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={[]}
          onClose={() => {}}
        />,
      );
    });

    // Should not call API at all.
    expect(mockGetChainSnapshot).not.toHaveBeenCalled();
    // Hint message shown.
    expect(screen.getByText(/select up to 8 expirations/i)).toBeTruthy();
    // No chart rendered.
    expect(screen.queryByTestId('chart-stub')).toBeNull();
  });
});

describe('<MultiExpirationSmilePanel> close button', () => {
  it('clicking Close calls onClose', async () => {
    mockGetChainSnapshot.mockResolvedValueOnce({ series: makeSeries(EXPIRATIONS_3) });

    const onClose = vi.fn();
    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={EXPIRATIONS_3}
          onClose={onClose}
        />,
      );
    });

    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('<MultiExpirationSmilePanel> missing values gap rather than break', () => {
  it('null values in the series produce null y entries (Plotly gap)', async () => {
    mockGetChainSnapshot.mockResolvedValueOnce({
      series: makeSeries(EXPIRATIONS_3, /* hasNull= */ true),
    });

    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={EXPIRATIONS_3}
          onClose={() => {}}
        />,
      );
    });

    const last = chartProps[chartProps.length - 1];
    expect(last.traces).toHaveLength(3);

    // Each trace should have a null in its y array (position 1).
    for (const trace of last.traces) {
      expect(trace.y[1]).toBeNull();
    }

    // connectgaps=false ensures Plotly renders a gap rather than bridging over.
    for (const trace of last.traces) {
      expect(trace.connectgaps).toBe(false);
    }
  });

  it('ComputeResult with source="missing" maps to null y', async () => {
    mockGetChainSnapshot.mockResolvedValueOnce({
      series: [
        {
          expiration: '2024-04-19',
          strikes: [100, 110],
          values: [
            { value: null, source: 'missing', error_code: 'missing_forward_vix_curve' },
            { value: 0.22, source: 'stored' },
          ],
        },
      ],
    });

    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={['2024-04-19']}
          onClose={() => {}}
        />,
      );
    });

    const last = chartProps[chartProps.length - 1];
    expect(last.traces[0].y[0]).toBeNull();
    expect(last.traces[0].y[1]).toBe(0.22);
  });
});

describe('<MultiExpirationSmilePanel> error state', () => {
  it('shows error message when API fails', async () => {
    mockGetChainSnapshot.mockRejectedValueOnce(new Error('network error'));

    await act(async () => {
      render(
        <MultiExpirationSmilePanel
          root="OPT_SP_500"
          date="2024-04-05"
          expirations={EXPIRATIONS_3}
          onClose={() => {}}
        />,
      );
    });

    expect(screen.getByText(/failed to load smile/i)).toBeTruthy();
    expect(screen.getByText(/network error/i)).toBeTruthy();
  });
});
