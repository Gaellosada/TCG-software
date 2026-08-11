// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

afterEach(cleanup);

// Chart pulls in Plotly (canvas) — stub it so jsdom doesn't choke, and so we
// can assert the equity trace handed to it.
vi.mock('../../components/Chart', () => ({
  default: ({ traces, downloadFilename }) => (
    <div
      data-testid="chart"
      data-fn={downloadFilename}
      data-points={Array.isArray(traces) && traces[0] ? traces[0].x.length : 0}
      data-last={Array.isArray(traces) && traces[0] ? traces[0].y[traces[0].y.length - 1] : ''}
    />
  ),
}));

import IntradayBacktestPage from './IntradayBacktestPage';

// ---------------------------------------------------------------------------
// PINNED contract fixtures (DESIGN.md).
// ---------------------------------------------------------------------------
const META = {
  window: { min_date: '2025-01-01', max_date: '2026-07-31' },
  expiry_modes: ['0DTE', 'NDTE'],
  roots: ['OPT_SP_500_EW'],
  hedge_instrument: 'FUT_SP_500',
  multiplier: 50,
  timezone: 'America/New_York',
};

const RUN_RESPONSE = {
  params_echo: {},
  window: { min_date: '2025-01-01', max_date: '2026-07-31' },
  days: [
    {
      date: '2025-02-03', status: 'ok', skip_reason: null,
      expiry: '2025-02-03', strike: 5850.0,
      entry: { ts: '2025-02-03T15:00:00Z', underlying: 5851.2, call_mid: 18.5, put_mid: 17.25, straddle_price: 35.75 },
      exit: { ts: '2025-02-03T20:45:00Z', underlying: 5860.0, call_mid: 22.0, put_mid: 9.5, straddle_price: 31.5 },
      hedge_trades: [{ ts: '2025-02-03T15:15:00Z', underlying: 5855.0, net_delta: 0.42, hedge_qty: -0.42 }],
      pnl: { option_pnl_pts: -4.25, hedge_pnl_pts: 3.1, total_pnl_pts: -1.15, total_pnl_usd: -57.5 },
    },
    {
      date: '2025-02-17', status: 'skipped', skip_reason: 'excluded',
      expiry: null, strike: null, entry: null, exit: null, hedge_trades: [],
      pnl: null,
    },
    {
      date: '2025-02-20', status: 'skipped', skip_reason: 'no_quote_within_tolerance',
      expiry: null, strike: null, entry: null, exit: null, hedge_trades: [],
      pnl: null,
    },
  ],
  aggregate: {
    n_days: 3, n_traded: 1, n_skipped: 2,
    total_pnl_usd: -57.5, mean_daily_pnl_usd: -57.5, win_rate: 0.0,
    sharpe: -0.8, max_drawdown_usd: -57.5,
    equity_curve: [{ date: '2025-02-03', cum_pnl_usd: -57.5 }],
  },
  warnings: ['2 days skipped: no quote within tolerance / excluded'],
};

// ---------------------------------------------------------------------------
// Global fetch mock. Captures the POST /run body so we can assert the payload
// matches the pinned request schema.
// ---------------------------------------------------------------------------
let lastRunBody = null;

function jsonResp(payload) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
}

function installFetch() {
  lastRunBody = null;
  const fn = vi.fn((url, opts = {}) => {
    if (String(url).endsWith('/intraday-backtest/meta')) return jsonResp(META);
    if (String(url).endsWith('/intraday-backtest/run')) {
      lastRunBody = JSON.parse(opts.body);
      return jsonResp(RUN_RESPONSE);
    }
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

beforeEach(() => {
  vi.clearAllMocks();
  installFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// Helper: render and wait for meta to hydrate the controls.
async function renderReady() {
  render(<IntradayBacktestPage />);
  await waitFor(() => {
    expect(screen.getByLabelText('Start date')).toBeTruthy();
  });
}

describe('IntradayBacktestPage', () => {
  it('renders all controls (entry/exit ET, expiry mode, side, hedge, snap, exceptions)', async () => {
    await renderReady();
    // Times labelled ET
    expect(screen.getByLabelText('Entry time (ET)')).toBeTruthy();
    expect(screen.getByLabelText('Exit time (ET)')).toBeTruthy();
    expect(screen.getAllByText(/ET/).length).toBeGreaterThan(0);
    // Expiry mode + side
    expect(screen.getByLabelText(/expiry mode/i)).toBeTruthy();
    expect(screen.getByLabelText(/straddle side/i)).toBeTruthy();
    // Hedge controls
    expect(screen.getByLabelText(/delta.?hedge/i)).toBeTruthy();
    expect(screen.getByLabelText(/interval/i)).toBeTruthy();
    expect(screen.getByLabelText(/delta band/i)).toBeTruthy();
    // Snap tolerance
    expect(screen.getByLabelText(/snap tolerance/i)).toBeTruthy();
    // Exception dates add control
    expect(screen.getByTestId('add-exception')).toBeTruthy();
    // Run button
    expect(screen.getByRole('button', { name: /run backtest/i })).toBeTruthy();
  });

  it('bounds the date range inputs to the /meta window', async () => {
    await renderReady();
    const start = screen.getByLabelText('Start date');
    const end = screen.getByLabelText('End date');
    expect(start.getAttribute('min')).toBe('2025-01-01');
    expect(start.getAttribute('max')).toBe('2026-07-31');
    expect(end.getAttribute('min')).toBe('2025-01-01');
    expect(end.getAttribute('max')).toBe('2026-07-31');
  });

  it('submits a contract-shaped payload on Run', async () => {
    const fetchFn = installFetch();
    await renderReady();

    // Configure a couple of fields so we assert real values flow through.
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-03-31' } });
    fireEvent.change(screen.getByLabelText('Entry time (ET)'), { target: { value: '10:00' } });
    fireEvent.change(screen.getByLabelText('Exit time (ET)'), { target: { value: '15:45' } });
    fireEvent.change(screen.getByLabelText(/straddle side/i), { target: { value: 'short' } });

    // Add an exception date.
    fireEvent.change(screen.getByTestId('exception-date-input'), { target: { value: '2025-02-17' } });
    fireEvent.click(screen.getByTestId('add-exception'));

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => expect(lastRunBody).not.toBeNull());

    // Assert the run POST went to the right endpoint.
    const runCall = fetchFn.mock.calls.find((c) => String(c[0]).endsWith('/intraday-backtest/run'));
    expect(runCall).toBeTruthy();
    expect(runCall[1].method).toBe('POST');

    // Assert body matches the PINNED request schema shape + values.
    expect(lastRunBody.start_date).toBe('2025-02-01');
    expect(lastRunBody.end_date).toBe('2025-03-31');
    expect(lastRunBody.entry_time).toBe('10:00');
    expect(lastRunBody.exit_time).toBe('15:45');
    expect(lastRunBody.expiry_mode).toBe('0DTE');
    expect(typeof lastRunBody.dte).toBe('number');
    expect(lastRunBody.straddle_side).toBe('short');
    expect(lastRunBody.hedge).toEqual({
      enabled: expect.any(Boolean),
      interval_minutes: expect.any(Number),
      delta_band: expect.any(Number),
    });
    expect(typeof lastRunBody.snap_tolerance_minutes).toBe('number');
    expect(lastRunBody.exception_dates).toEqual(['2025-02-17']);
    expect(Array.isArray(lastRunBody.date_overrides)).toBe(true);
  });

  it('renders the days list, aggregate stats and the equity chart after Run', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => expect(screen.getByTestId('days-table')).toBeTruthy());

    // Days rows present (3 days).
    expect(screen.getAllByTestId('day-row').length).toBe(3);
    // The traded day shows its strike (locale-formatted).
    expect(screen.getByText('5,850')).toBeTruthy();

    // Aggregate stats surfaced.
    const agg = screen.getByTestId('aggregate-stats');
    expect(agg.textContent).toMatch(/Sharpe/i);
    expect(agg.textContent).toMatch(/-0\.8/);

    // Equity chart rendered via the shared Chart (stubbed).
    const chart = screen.getByTestId('chart');
    expect(chart.getAttribute('data-points')).toBe('1');
    expect(chart.getAttribute('data-last')).toBe('-57.5');
  });

  it('visibly flags skipped days with their skip_reason', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-table')).toBeTruthy());

    const table = screen.getByTestId('days-table');
    const skipped = table.querySelectorAll('[data-skipped="true"]');
    expect(skipped.length).toBe(2);
    // Skip reasons shown.
    expect(table.textContent).toMatch(/excluded/);
    expect(table.textContent).toMatch(/no_quote_within_tolerance/);
  });

  it('surfaces the warnings array', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('warnings')).toBeTruthy());
    expect(screen.getByTestId('warnings').textContent).toMatch(/2 days skipped/);
  });
});
