// @vitest-environment jsdom
//
// Tests for the Statistics component:
//   1. Renders all metric labels and values for a mocked /api/statistics response.
//   2. Editing the Rf input triggers a debounced refetch with the new rate.
//   3. Null skew/kurtosis render as "—".
//   4. Loading state shown during the fetch.
//   5. Error message shown on fetch failure.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';

// Mock the API helper so we can assert call args + control resolution.
vi.mock('../../api/statistics', () => ({
  fetchStatistics: vi.fn(),
}));

import Statistics from './Statistics';
import { fetchStatistics } from '../../api/statistics';

// A complete, well-formed response — matches the locked contract.
const RESPONSE = {
  return: {
    total_return: 0.234,
    cagr: 0.121,
    annualized_volatility: 0.183,
    best_day: 0.045,
    worst_day: -0.063,
    best_month: 0.112,
    worst_month: -0.087,
  },
  risk_adjusted: {
    sharpe_ratio: 1.23,
    sortino_ratio: 1.78,
    calmar_ratio: 0.92,
  },
  tail: {
    var_95: -0.024,
    var_99: -0.041,
    cvar_5: -0.035,
    skewness: -0.31,
    kurtosis: 4.12,
  },
  drawdown: {
    max_drawdown: -0.187,
    avg_drawdown: -0.043,
    current_drawdown: -0.012,
    longest_drawdown_days: 47,
    time_underwater_days: 312,
  },
  risk_free_rate_used: 0.04,
  num_observations: 504,
};

const DATES = [20240101, 20240102, 20240103];
const EQUITY = [100000.0, 100120.5, 100200.0];

afterEach(() => {
  cleanup();
  vi.mocked(fetchStatistics).mockReset();
  vi.useRealTimers();
});

describe('<Statistics> — rendering', () => {
  it('renders all metric labels and formatted values from a mocked response', async () => {
    vi.mocked(fetchStatistics).mockResolvedValue(RESPONSE);

    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });

    // Section titles
    expect(screen.getByText('Return')).toBeTruthy();
    expect(screen.getByText('Risk-adjusted')).toBeTruthy();
    expect(screen.getByText('Tail')).toBeTruthy();
    expect(screen.getByText('Drawdown')).toBeTruthy();

    // A sample of labels across all four sections
    expect(screen.getByText('Total Return')).toBeTruthy();
    expect(screen.getByText('CAGR')).toBeTruthy();
    expect(screen.getByText('Ann. Vol')).toBeTruthy();
    expect(screen.getByText('Best Day')).toBeTruthy();
    expect(screen.getByText('Worst Day')).toBeTruthy();
    expect(screen.getByText('Best Month')).toBeTruthy();
    expect(screen.getByText('Worst Month')).toBeTruthy();
    expect(screen.getByText('Sharpe')).toBeTruthy();
    expect(screen.getByText('Sortino')).toBeTruthy();
    expect(screen.getByText('Calmar')).toBeTruthy();
    expect(screen.getByText('VaR 95%')).toBeTruthy();
    expect(screen.getByText('VaR 99%')).toBeTruthy();
    expect(screen.getByText('CVaR 5%')).toBeTruthy();
    expect(screen.getByText('Skew')).toBeTruthy();
    expect(screen.getByText('Kurtosis')).toBeTruthy();
    expect(screen.getByText('Max DD')).toBeTruthy();
    expect(screen.getByText('Avg DD')).toBeTruthy();
    expect(screen.getByText('Current DD')).toBeTruthy();
    expect(screen.getByText('Longest DD')).toBeTruthy();
    expect(screen.getByText('Underwater')).toBeTruthy();

    // Formatted values
    expect(screen.getByText('+23.40%')).toBeTruthy();     // total_return
    expect(screen.getByText('+12.10%')).toBeTruthy();     // cagr
    expect(screen.getByText('+18.30%')).toBeTruthy();     // annualized_volatility
    expect(screen.getByText('-6.30%')).toBeTruthy();      // worst_day
    expect(screen.getByText('1.23')).toBeTruthy();        // sharpe
    expect(screen.getByText('1.78')).toBeTruthy();        // sortino
    expect(screen.getByText('0.92')).toBeTruthy();        // calmar
    expect(screen.getByText('-2.40%')).toBeTruthy();      // var_95
    expect(screen.getByText('-0.31')).toBeTruthy();       // skew
    expect(screen.getByText('4.12')).toBeTruthy();        // kurtosis
    expect(screen.getByText('-18.70%')).toBeTruthy();     // max_drawdown
    expect(screen.getByText('-1.20%')).toBeTruthy();      // current_drawdown (≤ 0)
    expect(screen.getByText('47 days')).toBeTruthy();     // longest_drawdown_days
    expect(screen.getByText('312 days')).toBeTruthy();    // time_underwater_days

    // Observations count
    expect(screen.getByText('504 obs')).toBeTruthy();
  });

  it('passes the default risk-free rate (0.04) to the first fetch', async () => {
    vi.mocked(fetchStatistics).mockResolvedValue(RESPONSE);
    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });
    expect(fetchStatistics).toHaveBeenCalledTimes(1);
    const [payload] = fetchStatistics.mock.calls[0];
    expect(payload.dates).toBe(DATES);
    expect(payload.equity).toBe(EQUITY);
    expect(payload.riskFreeRate).toBeCloseTo(0.04, 10);
  });

  it('honors a custom defaultRiskFreeRate', async () => {
    vi.mocked(fetchStatistics).mockResolvedValue(RESPONSE);
    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} defaultRiskFreeRate={0.025} />);
    });
    const [payload] = fetchStatistics.mock.calls[0];
    expect(payload.riskFreeRate).toBeCloseTo(0.025, 10);
    // Input shows percent form
    const input = screen.getByLabelText(/risk-free rate/i);
    expect(input.value).toBe('2.50');
  });
});

describe('<Statistics> — Rf debounced refetch', () => {
  it('refetches with the new rate after the Rf input changes (debounced 300ms)', async () => {
    vi.useFakeTimers();
    vi.mocked(fetchStatistics).mockResolvedValue(RESPONSE);

    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });

    // First fetch — default rate.
    expect(fetchStatistics).toHaveBeenCalledTimes(1);

    const input = screen.getByLabelText(/risk-free rate/i);

    // Change Rf to 5.00 (= 0.05). No fetch yet (debounced).
    await act(async () => {
      fireEvent.change(input, { target: { value: '5.00' } });
    });
    expect(fetchStatistics).toHaveBeenCalledTimes(1);

    // Advance past the debounce window — refetch fires.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(350);
    });
    expect(fetchStatistics).toHaveBeenCalledTimes(2);
    const [payload2] = fetchStatistics.mock.calls[1];
    expect(payload2.riskFreeRate).toBeCloseTo(0.05, 10);
  });

  it('coalesces rapid keystrokes into a single refetch', async () => {
    vi.useFakeTimers();
    vi.mocked(fetchStatistics).mockResolvedValue(RESPONSE);

    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });
    expect(fetchStatistics).toHaveBeenCalledTimes(1);

    const input = screen.getByLabelText(/risk-free rate/i);
    await act(async () => {
      fireEvent.change(input, { target: { value: '4.5' } });
      await vi.advanceTimersByTimeAsync(100);
      fireEvent.change(input, { target: { value: '5.0' } });
      await vi.advanceTimersByTimeAsync(100);
      fireEvent.change(input, { target: { value: '5.5' } });
    });
    expect(fetchStatistics).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(350);
    });
    // Exactly one extra fetch — with the final value.
    expect(fetchStatistics).toHaveBeenCalledTimes(2);
    const [payload2] = fetchStatistics.mock.calls[1];
    expect(payload2.riskFreeRate).toBeCloseTo(0.055, 10);
  });
});

describe('<Statistics> — null handling', () => {
  it('renders "—" for null skew and kurtosis', async () => {
    const withNulls = {
      ...RESPONSE,
      tail: { ...RESPONSE.tail, skewness: null, kurtosis: null },
    };
    vi.mocked(fetchStatistics).mockResolvedValue(withNulls);

    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });

    // Other tail metrics still render normally.
    expect(screen.getByText('-2.40%')).toBeTruthy();
    // Skew + Kurtosis cells show the em-dash placeholder.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });
});

describe('<Statistics> — loading and error states', () => {
  it('shows the loading indicator while the fetch is in flight', async () => {
    let resolveIt;
    vi.mocked(fetchStatistics).mockImplementation(
      () => new Promise((resolve) => { resolveIt = resolve; }),
    );

    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });

    expect(screen.getByText(/loading/i)).toBeTruthy();

    // Resolve the fetch — loading clears.
    await act(async () => {
      resolveIt(RESPONSE);
    });
    expect(screen.queryByText(/loading/i)).toBeNull();
  });

  it('shows an error message when the fetch fails', async () => {
    vi.mocked(fetchStatistics).mockRejectedValue(new Error('Boom: statistics backend unreachable.'));

    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });

    expect(screen.getByRole('alert').textContent).toMatch(/boom: statistics backend unreachable/i);
  });

  it('keeps the previous successful result visible while a refetch is in flight', async () => {
    vi.useFakeTimers();

    // First call resolves with RESPONSE; second hangs to simulate in-flight.
    let resolveSecond;
    vi.mocked(fetchStatistics)
      .mockResolvedValueOnce(RESPONSE)
      .mockImplementationOnce(
        () => new Promise((resolve) => { resolveSecond = resolve; }),
      );

    await act(async () => {
      render(<Statistics dates={DATES} equity={EQUITY} />);
    });
    // First result visible.
    expect(screen.getByText('1.23')).toBeTruthy(); // Sharpe

    // Trigger a refetch via Rf change.
    const input = screen.getByLabelText(/risk-free rate/i);
    await act(async () => {
      fireEvent.change(input, { target: { value: '6.00' } });
      await vi.advanceTimersByTimeAsync(350);
    });

    // Loading visible AND previous values still rendered.
    expect(screen.getByText(/loading/i)).toBeTruthy();
    expect(screen.getByText('1.23')).toBeTruthy();

    // Clean up the dangling promise.
    await act(async () => {
      resolveSecond(RESPONSE);
    });
  });
});
