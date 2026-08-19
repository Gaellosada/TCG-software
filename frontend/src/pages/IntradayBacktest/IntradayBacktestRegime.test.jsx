// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

afterEach(cleanup);

// Stub the Plotly-backed Chart (jsdom has no canvas).
vi.mock('../../components/Chart', () => ({
  default: ({ downloadFilename }) => <div data-testid="chart" data-fn={downloadFilename} />,
}));

import IntradayBacktestPage from './IntradayBacktestPage';

const META = {
  window: { min_date: '2025-01-01', max_date: '2026-07-31' },
  expiry_modes: ['0DTE', 'NDTE'],
  roots: ['OPT_SP_500_EW'],
  hedge_instrument: 'FUT_SP_500',
  multiplier: 50,
  timezone: 'America/New_York',
};

// A run result exercising the F2.2 per-day regime readout: a traded LONG day, a
// regime-FLAT (no-trade) day, and a traded SHORT day — all in one month.
const RUN_RESPONSE = {
  params_echo: {},
  window: { min_date: '2025-01-01', max_date: '2026-07-31' },
  days: [
    {
      date: '2025-02-03', status: 'ok', skip_reason: null,
      expiry: '2025-02-03', strike: 5850.0, entry: null, exit: null, legs: null,
      hedge_trades: [], exit_trigger: null,
      pnl: { option_pnl_pts: 1.0, hedge_pnl_pts: 0.0, total_pnl_pts: 1.0, total_pnl_usd: 50.0 },
      regime: { state: 'hvol_on', side: 'long', asof: 20250131, gate: null,
                signals: { h20: 0.2, h30: 0.18, h100: 0.15, vvix: null } },
    },
    {
      date: '2025-02-04', status: 'skipped', skip_reason: 'regime_flat',
      expiry: null, strike: null, entry: null, exit: null, legs: null,
      hedge_trades: [], exit_trigger: null, pnl: null,
      regime: { state: 'extremely_low', side: 'flat', asof: 20250203, gate: null,
                signals: { h20: 0.03, h30: 0.03, h100: 0.03, vvix: null } },
    },
    {
      date: '2025-02-05', status: 'ok', skip_reason: null,
      expiry: '2025-02-05', strike: 5860.0, entry: null, exit: null, legs: null,
      hedge_trades: [], exit_trigger: null,
      pnl: { option_pnl_pts: -1.0, hedge_pnl_pts: 0.0, total_pnl_pts: -1.0, total_pnl_usd: -25.0 },
      regime: { state: 'hvol_off', side: 'short', asof: 20250204, gate: null,
                signals: { h20: 0.12, h30: 0.16, h100: 0.18, vvix: null } },
    },
  ],
  aggregate: {
    n_days: 3, n_traded: 2, n_skipped: 1, total_pnl_usd: 25.0,
    mean_daily_pnl_usd: 12.5, win_rate: 0.5, sharpe: 0.0, max_drawdown_usd: -25.0,
    total_cost_usd: 0.0, n_fallback_fills: 0,
    equity_curve: [
      { date: '2025-02-03', cum_pnl_usd: 50.0 },
      { date: '2025-02-05', cum_pnl_usd: 25.0 },
    ],
  },
  warnings: [],
};

let lastRunBody = null;
let progressCalls = 0;

function jsonResp(payload) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
}

function installFetch() {
  lastRunBody = null;
  progressCalls = 0;
  const seq = [
    { status: 'running', days_done: 0, total_days: 2, result: null, error: null },
    { status: 'done', days_done: 2, total_days: 2, result: RUN_RESPONSE, error: null },
  ];
  const fn = vi.fn((url, options = {}) => {
    const u = String(url);
    if (u.endsWith('/intraday-backtest/meta')) return jsonResp(META);
    if (u.endsWith('/intraday-backtest/event-calendar')) {
      return jsonResp({
        event_types: ['FOMC', 'NFP', 'CPI'],
        events: { FOMC: [], NFP: [], CPI: [] },
        all_dates: [],
        tentative_dates: [],
      });
    }
    if (u.endsWith('/intraday-backtest/run-async')) {
      lastRunBody = JSON.parse(options.body);
      return jsonResp({ job_id: 'job-1' });
    }
    if (u.includes('/intraday-backtest/progress/')) {
      const idx = Math.min(progressCalls, seq.length - 1);
      progressCalls += 1;
      return jsonResp(seq[idx]);
    }
    if (u.includes('/intraday-backtest/cache/')) return jsonResp({ cached: false });
    return Promise.reject(new Error(`unexpected fetch: ${u}`));
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

async function renderReady() {
  render(<IntradayBacktestPage />);
  await waitFor(() => expect(screen.getByLabelText('Start date')).toBeTruthy());
}

describe('IntradayBacktestPage — F2.2 regime-driven side', () => {
  it('defaults regime-driven side OFF and hides its inputs', async () => {
    await renderReady();
    expect(screen.getByLabelText('Enable regime-driven side').checked).toBe(false);
    expect(screen.queryByLabelText('HVOL ladder tolerance')).toBeNull();
    expect(screen.queryByLabelText('Extremely-low H20 floor')).toBeNull();
    expect(screen.queryByLabelText('Enable VVIX gate')).toBeNull();
  });

  it('omits the regime block from the payload when off (byte-identical baseline)', async () => {
    await renderReady();
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-02-28' } });
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());
    expect(lastRunBody.regime).toBeUndefined();
  });

  it('reveals the decision inputs and wires the regime block (incl. VVIX gate) into the payload', async () => {
    await renderReady();
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-02-28' } });

    fireEvent.click(screen.getByLabelText('Enable regime-driven side'));
    fireEvent.change(screen.getByLabelText('HVOL ladder tolerance'), { target: { value: '0.05' } });
    fireEvent.change(screen.getByLabelText('Extremely-low H20 floor'), { target: { value: '0.06' } });
    // VVIX gate level only appears once the gate is enabled.
    expect(screen.queryByLabelText('VVIX gate level')).toBeNull();
    fireEvent.click(screen.getByLabelText('Enable VVIX gate'));
    fireEvent.change(screen.getByLabelText('VVIX gate level'), { target: { value: '120' } });

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());

    expect(lastRunBody.regime).toEqual({
      side_mode: 'regime_driven',
      hvol_tolerance: 0.05,
      extremely_low_h20: 0.06,
      gates: [{ enabled: true, signal: 'vvix', above: 120, action: 'flat' }],
    });
  });

  it('sends an empty gates list when the VVIX gate is left disabled', async () => {
    await renderReady();
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-02-28' } });
    fireEvent.click(screen.getByLabelText('Enable regime-driven side'));
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());
    expect(lastRunBody.regime.side_mode).toBe('regime_driven');
    expect(lastRunBody.regime.gates).toEqual([]);
  });

  it('surfaces the per-day regime side/state in the results calendar', async () => {
    await renderReady();
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-02-28' } });
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    const cells = screen.getAllByTestId('day-cell');
    const byDate = Object.fromEntries(cells.map((c) => [c.getAttribute('data-date'), c]));

    // Traded LONG day: carries the regime side attribute + an L badge.
    const long = byDate['2025-02-03'];
    expect(long.getAttribute('data-regime-side')).toBe('long');
    expect(long.getAttribute('title')).toContain('regime: long (hvol_on');
    expect(long.getAttribute('title')).toContain('as-of 2025-01-31');

    // Regime-FLAT day: a deliberate no-trade outcome, "flat" tag, WHY in title.
    const flat = byDate['2025-02-04'];
    expect(flat.getAttribute('data-outcome')).toBe('regime_flat');
    expect(flat.textContent).toContain('flat');
    expect(flat.getAttribute('title')).toContain('flat (regime)');
    expect(flat.getAttribute('title')).toContain('extremely_low');

    // Traded SHORT day: S badge.
    const short = byDate['2025-02-05'];
    expect(short.getAttribute('data-regime-side')).toBe('short');
    expect(short.getAttribute('title')).toContain('regime: short (hvol_off');
  });
});
