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

const EVENT_CALENDAR = {
  event_types: ['FOMC', 'NFP', 'CPI'],
  events: {
    FOMC: [{ date: '2025-01-29', tentative: false }],
    NFP: [{ date: '2025-02-07', tentative: false }],
    CPI: [{ date: '2025-02-12', tentative: false }],
  },
  all_dates: ['2025-01-29', '2025-02-07', '2025-02-12'],
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

describe('IntradayBacktestPage — F3.2 date allowlist', () => {
  it('defaults the allowlist OFF and hides its controls', async () => {
    await renderReady();
    expect(screen.getByLabelText('Enable date allowlist').checked).toBe(false);
    expect(screen.queryByTestId('allowlist-controls')).toBeNull();
  });

  it('omits the allowlist block from the payload when off (byte-identical baseline)', async () => {
    await renderReady();
    await setRange();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());
    expect(lastRunBody.allowlist).toBeUndefined();
  });

  it('shows event-type counts and wires event types + explicit dates into the payload', async () => {
    await renderReady();
    await setRange();

    fireEvent.click(screen.getByLabelText('Enable date allowlist'));
    // Per-type counts from the fetched calendar are shown.
    await waitFor(() =>
      expect(screen.getByLabelText('Allowlist event type FOMC')).toBeTruthy());
    expect(screen.getByText(/FOMC \(1\)/)).toBeTruthy();

    // Select two event types.
    fireEvent.click(screen.getByLabelText('Allowlist event type FOMC'));
    fireEvent.click(screen.getByLabelText('Allowlist event type CPI'));

    // Add an explicit date.
    fireEvent.change(screen.getByLabelText('Allowlist date'), { target: { value: '2025-02-14' } });
    fireEvent.click(screen.getByTestId('allowlist-add-date'));
    expect(screen.getByTestId('allowlist-dates-list').textContent).toContain('2025-02-14');

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());

    expect(lastRunBody.allowlist).toEqual({
      mode: 'allowlist',
      dates: ['2025-02-14'],
      event_types: ['FOMC', 'CPI'],
    });
  });

  it('removes an explicit date and reflects it in the payload', async () => {
    await renderReady();
    await setRange();
    fireEvent.click(screen.getByLabelText('Enable date allowlist'));

    fireEvent.change(screen.getByLabelText('Allowlist date'), { target: { value: '2025-02-14' } });
    fireEvent.click(screen.getByTestId('allowlist-add-date'));
    fireEvent.click(screen.getByLabelText('Remove allowlist date 2025-02-14'));
    expect(screen.queryByTestId('allowlist-dates-list')).toBeNull();

    // Even with no dates/types, an active allowlist sends mode:allowlist (the
    // backend 400s on empty — surfaced to the user, never a silent full-range run).
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());
    expect(lastRunBody.allowlist).toEqual({
      mode: 'allowlist', dates: [], event_types: [],
    });
  });
});
