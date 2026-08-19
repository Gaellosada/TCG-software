// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

afterEach(cleanup);

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

const EVENT_CALENDAR = {
  event_types: ['FOMC', 'NFP', 'CPI'],
  events: { FOMC: [], NFP: [], CPI: [] },
  all_dates: [],
  tentative_dates: [],
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
    { status: 'running', days_done: 0, total_days: 1, result: null, error: null },
    { status: 'done', days_done: 1, total_days: 1, result: {
      params_echo: {}, window: META.window, days: [], aggregate: {
        n_days: 0, n_traded: 0, n_skipped: 0, total_pnl_usd: 0, mean_daily_pnl_usd: 0,
        win_rate: 0, sharpe: 0, max_drawdown_usd: 0, equity_curve: [],
      }, warnings: [],
    }, error: null },
  ];
  const fn = vi.fn((url, options = {}) => {
    const u = String(url);
    if (u.endsWith('/intraday-backtest/meta')) return jsonResp(META);
    if (u.endsWith('/intraday-backtest/event-calendar')) return jsonResp(EVENT_CALENDAR);
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

async function setRange() {
  fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
  fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-02-28' } });
}

describe('IntradayBacktestPage — F4.1 laddered multi-entry control', () => {
  it('defaults the ladder OFF and hides its controls', async () => {
    await renderReady();
    expect(screen.getByLabelText('Enable laddered multi-entry').checked).toBe(false);
    expect(screen.queryByTestId('ladder-controls')).toBeNull();
  });

  it('omits the ladder block from the payload when off (byte-identical baseline)', async () => {
    await renderReady();
    await setRange();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());
    expect(lastRunBody.ladder).toBeUndefined();
  });

  it('wires the ladder schedule + equal-contracts sizing into the payload', async () => {
    await renderReady();
    await setRange();

    fireEvent.click(screen.getByLabelText('Enable laddered multi-entry'));
    await waitFor(() => expect(screen.getByTestId('ladder-controls')).toBeTruthy());

    fireEvent.change(screen.getByLabelText('Ladder interval minutes'), { target: { value: '15' } });
    fireEvent.change(screen.getByLabelText('Ladder first entry'), { target: { value: '10:00' } });
    fireEvent.change(screen.getByLabelText('Ladder last entry cutoff'), { target: { value: '15:00' } });
    fireEvent.change(screen.getByLabelText('Ladder max concurrent'), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText('Ladder contracts per rung'), { target: { value: '2' } });

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());

    expect(lastRunBody.ladder).toEqual({
      enabled: true,
      interval_minutes: 15,
      first_entry: '10:00',
      last_entry_cutoff: '15:00',
      max_concurrent: 3,
      sizing: { mode: 'equal_contracts', contracts: 2, notional_per_entry_usd: 0 },
    });
  });

  it('blank first/cutoff map to null and equal-notional swaps the sizing input', async () => {
    await renderReady();
    await setRange();

    fireEvent.click(screen.getByLabelText('Enable laddered multi-entry'));
    await waitFor(() => expect(screen.getByTestId('ladder-controls')).toBeTruthy());
    fireEvent.change(screen.getByLabelText('Ladder sizing mode'), { target: { value: 'equal_notional' } });
    // The contracts input is replaced by the notional input.
    expect(screen.queryByLabelText('Ladder contracts per rung')).toBeNull();
    fireEvent.change(screen.getByLabelText('Ladder notional per entry'), { target: { value: '50000' } });

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());

    expect(lastRunBody.ladder.first_entry).toBeNull();
    expect(lastRunBody.ladder.last_entry_cutoff).toBeNull();
    expect(lastRunBody.ladder.sizing.mode).toBe('equal_notional');
    expect(lastRunBody.ladder.sizing.notional_per_entry_usd).toBe(50000);
  });
});
