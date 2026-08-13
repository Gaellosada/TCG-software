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
// PINNED v2 contract fixtures (DESIGN.md — "Conditional entry/exit modules +
// independent legs").
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
      entry: { ts: '2025-02-03T15:03:00Z', underlying: 5851.2, call_mid: 18.5, put_mid: 17.25, straddle_price: 35.75 },
      exit: { ts: '2025-02-03T20:44:00Z', underlying: 5860.0, call_mid: 22.0, put_mid: 9.5, straddle_price: 31.5 },
      // v2 independent-leg detail — the two legs fill minutes apart.
      legs: {
        call: { entry_ts: '2025-02-03T15:03:00Z', entry_price: 18.5, exit_ts: '2025-02-03T20:45:00Z', exit_price: 22.0, exit_conditions_met: true, pnl_pts: 3.5 },
        put: { entry_ts: '2025-02-03T15:01:00Z', entry_price: 17.25, exit_ts: '2025-02-03T20:44:00Z', exit_price: 9.5, exit_conditions_met: false, pnl_pts: -7.75 },
      },
      straddle_on_ts: '2025-02-03T15:03:00Z', straddle_off_ts: '2025-02-03T20:44:00Z',
      hedge_trades: [{ ts: '2025-02-03T15:15:00Z', underlying: 5855.0, net_delta: 0.42, hedge_qty: -0.42 }],
      // v3 early-exit trigger — this day closed early on a sigma_move.
      exit_trigger: { type: 'sigma_move', ts: '2025-02-03T19:12:00Z', value: 1.0 },
      pnl: { option_pnl_pts: -4.25, hedge_pnl_pts: 3.1, total_pnl_pts: -1.15, total_pnl_usd: -57.5 },
    },
    {
      // User-excluded day.
      date: '2025-02-17', status: 'excluded', skip_reason: 'excluded',
      expiry: null, strike: null, entry: null, exit: null, hedge_trades: [], pnl: null,
    },
    {
      // Data-gap skip — distinct from an exclusion.
      date: '2025-02-20', status: 'skipped', skip_reason: 'no_quote_within_tolerance',
      expiry: null, strike: null, entry: null, exit: null, hedge_trades: [], pnl: null,
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
// Global fetch mock. Async flow: POST /run-async → { job_id }, then poll
// GET /progress/{job_id}. Captures the POST body to assert the pinned request.
// ---------------------------------------------------------------------------
let lastRunBody = null;
let progressSeq = null;
let progressCalls = 0;

const DEFAULT_PROGRESS = [
  { status: 'running', days_done: 1, total_days: 3, result: null, error: null },
  { status: 'done', days_done: 3, total_days: 3, result: RUN_RESPONSE, error: null },
];

function jsonResp(payload) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
}

function installFetch(opts = {}) {
  lastRunBody = null;
  progressCalls = 0;
  progressSeq = opts.progress || DEFAULT_PROGRESS;
  const fn = vi.fn((url, options = {}) => {
    const u = String(url);
    if (u.endsWith('/intraday-backtest/meta')) return jsonResp(META);
    if (u.endsWith('/intraday-backtest/run-async')) {
      lastRunBody = JSON.parse(options.body);
      return jsonResp({ job_id: 'job-123' });
    }
    if (u.includes('/intraday-backtest/progress/')) {
      const idx = Math.min(progressCalls, progressSeq.length - 1);
      progressCalls += 1;
      return jsonResp(progressSeq[idx]);
    }
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

// Helper: render and wait for meta to hydrate the controls.
async function renderReady() {
  render(<IntradayBacktestPage />);
  await waitFor(() => {
    expect(screen.getByLabelText('Start date')).toBeTruthy();
  });
}

describe('IntradayBacktestPage', () => {
  it('renders all controls incl. the Entry/Exit rule modules', async () => {
    await renderReady();
    // Entry & Exit rule modules present, each with its own time + snap tolerance.
    expect(screen.getByTestId('entry-module')).toBeTruthy();
    expect(screen.getByTestId('exit-module')).toBeTruthy();
    expect(screen.getByLabelText('Entry time (ET)')).toBeTruthy();
    expect(screen.getByLabelText('Exit time (ET)')).toBeTruthy();
    // Each module owns a snap tolerance (two total).
    expect(screen.getAllByLabelText(/snap tolerance/i).length).toBe(2);
    // Each module owns an "Add condition" dropdown.
    expect(screen.getByTestId('entry-add-condition')).toBeTruthy();
    expect(screen.getByTestId('exit-add-condition')).toBeTruthy();
    // Times labelled ET.
    expect(screen.getAllByText(/ET/).length).toBeGreaterThan(0);
    // Expiry mode + side.
    expect(screen.getByLabelText(/expiry mode/i)).toBeTruthy();
    expect(screen.getByLabelText(/straddle side/i)).toBeTruthy();
    // Hedge controls.
    expect(screen.getByLabelText(/delta.?hedge/i)).toBeTruthy();
    expect(screen.getByLabelText(/interval/i)).toBeTruthy();
    expect(screen.getByLabelText(/delta band/i)).toBeTruthy();
    // Unified custom-days add control.
    expect(screen.getByTestId('add-custom-day')).toBeTruthy();
    // Run button.
    expect(screen.getByRole('button', { name: /run backtest/i })).toBeTruthy();
  });

  it('adding a condition via the dropdown renders that condition\'s param inputs', async () => {
    await renderReady();
    // No condition rows initially — the empty hint shows instead.
    expect(screen.getByTestId('entry-conditions-empty')).toBeTruthy();

    // Pick max_spread from the entry dropdown → its pct + min_ticks inputs appear.
    fireEvent.change(screen.getByTestId('entry-add-condition'), { target: { value: 'max_spread' } });
    expect(screen.getByTestId('entry-condition-max_spread')).toBeTruthy();
    expect(screen.getByLabelText('entry max_spread pct')).toBeTruthy();
    expect(screen.getByLabelText('entry max_spread min ticks')).toBeTruthy();

    // Pick min_quote_size → its size input appears alongside.
    fireEvent.change(screen.getByTestId('entry-add-condition'), { target: { value: 'min_quote_size' } });
    expect(screen.getByTestId('entry-condition-min_quote_size')).toBeTruthy();
    expect(screen.getByLabelText('entry min_quote_size size')).toBeTruthy();

    // Removing the max_spread row drops just that condition.
    fireEvent.click(screen.getByLabelText('Remove Max spread condition'));
    expect(screen.queryByTestId('entry-condition-max_spread')).toBeNull();
    expect(screen.getByTestId('entry-condition-min_quote_size')).toBeTruthy();
  });

  it('shows in-app snap-tolerance help on each module', async () => {
    await renderReady();
    const help = screen.getByTestId('entry-snap-help');
    expect(help.getAttribute('title')).toMatch(/nearest one within this many minutes/i);
    expect(help.getAttribute('title')).toMatch(/the day is skipped/i);
    expect(help.getAttribute('aria-label')).toMatch(/quotes are sparse/i);
    // Exit module carries the same help copy.
    expect(screen.getByTestId('exit-snap-help').getAttribute('title')).toMatch(/quotes are sparse/i);
  });

  it('shows in-app help for the hedge interval and delta band fields', async () => {
    await renderReady();

    const intervalHelp = screen.getByTestId('hedge-interval-help');
    expect(intervalHelp.getAttribute('title')).toMatch(/fixed clock/i);
    expect(intervalHelp.getAttribute('title')).toMatch(/delta-neutral/i);
    expect(intervalHelp.getAttribute('aria-label')).toMatch(/re-hedged with the ES future/i);

    const bandHelp = screen.getByTestId('delta-band-help');
    expect(bandHelp.getAttribute('title')).toMatch(/open \(unhedged\) net delta/i);
    expect(bandHelp.getAttribute('title')).toMatch(/whichever fires first/i);
    expect(bandHelp.getAttribute('aria-label')).toMatch(/ES-future-equivalent units/i);
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

  it('submits the PINNED v2 payload — entry/exit objects with conditions + full custom_days', async () => {
    const fetchFn = installFetch();
    await renderReady();

    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-03-31' } });
    fireEvent.change(screen.getByLabelText('Entry time (ET)'), { target: { value: '10:00' } });
    fireEvent.change(screen.getByLabelText('Exit time (ET)'), { target: { value: '15:45' } });
    fireEvent.change(screen.getByLabelText(/straddle side/i), { target: { value: 'short' } });

    // Build a global ENTRY conditions list: a max_spread AND another type.
    fireEvent.change(screen.getByTestId('entry-add-condition'), { target: { value: 'max_spread' } });
    fireEvent.change(screen.getByLabelText('entry max_spread pct'), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('entry max_spread min ticks'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('entry-add-condition'), { target: { value: 'min_quote_size' } });
    fireEvent.change(screen.getByLabelText('entry min_quote_size size'), { target: { value: '25' } });

    // An EXIT condition too (min_premium).
    fireEvent.change(screen.getByTestId('exit-add-condition'), { target: { value: 'min_premium' } });
    fireEvent.change(screen.getByLabelText('exit min_premium points'), { target: { value: '0.75' } });

    // EXCLUDED custom day.
    fireEvent.change(screen.getByTestId('custom-day-input'), { target: { value: '2025-02-17' } });
    fireEvent.click(screen.getByTestId('add-custom-day'));
    fireEvent.click(screen.getByLabelText('Exclude 2025-02-17'));

    // OVERRIDE custom day: add, expand, override entry + exit (partial).
    fireEvent.change(screen.getByTestId('custom-day-input'), { target: { value: '2025-02-14' } });
    fireEvent.click(screen.getByTestId('add-custom-day'));
    fireEvent.click(screen.getByTestId('custom-day-toggle-2025-02-14'));
    fireEvent.change(screen.getByLabelText('Entry 2025-02-14 time (ET)'), { target: { value: '11:00' } });
    fireEvent.change(screen.getByLabelText('Exit 2025-02-14 time (ET)'), { target: { value: '14:00' } });
    // A per-day override condition too.
    fireEvent.change(screen.getByTestId('cd-2025-02-14-entry-add-condition'), { target: { value: 'min_premium' } });
    fireEvent.change(screen.getByLabelText('cd-2025-02-14-entry min_premium points'), { target: { value: '1.25' } });

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());

    const runCall = fetchFn.mock.calls.find((c) => String(c[0]).endsWith('/intraday-backtest/run-async'));
    expect(runCall).toBeTruthy();
    expect(runCall[1].method).toBe('POST');

    // Scalars.
    expect(lastRunBody.start_date).toBe('2025-02-01');
    expect(lastRunBody.end_date).toBe('2025-03-31');
    expect(lastRunBody.expiry_mode).toBe('0DTE');
    expect(typeof lastRunBody.dte).toBe('number');
    expect(lastRunBody.straddle_side).toBe('short');
    expect(lastRunBody.hedge).toEqual({
      enabled: expect.any(Boolean),
      interval_minutes: expect.any(Number),
      delta_band: expect.any(Number),
    });

    // Old flat fields are gone.
    expect(lastRunBody.entry_time).toBeUndefined();
    expect(lastRunBody.exit_time).toBeUndefined();
    expect(lastRunBody.snap_tolerance_minutes).toBeUndefined();
    expect(lastRunBody.exception_dates).toBeUndefined();
    expect(lastRunBody.date_overrides).toBeUndefined();

    // ENTRY module object (time + own snap tolerance + conditions array).
    expect(lastRunBody.entry.time).toBe('10:00');
    expect(typeof lastRunBody.entry.snap_tolerance_minutes).toBe('number');
    expect(Array.isArray(lastRunBody.entry.conditions)).toBe(true);
    expect(lastRunBody.entry.conditions).toContainEqual({ type: 'max_spread', pct: 4, min_ticks: 2 });
    expect(lastRunBody.entry.conditions).toContainEqual({ type: 'min_quote_size', size: 25 });

    // EXIT module object.
    expect(lastRunBody.exit.time).toBe('15:45');
    expect(typeof lastRunBody.exit.snap_tolerance_minutes).toBe('number');
    expect(lastRunBody.exit.conditions).toContainEqual({ type: 'min_premium', points: 0.75 });

    // custom_days: an excluded day + a full per-day override.
    expect(Array.isArray(lastRunBody.custom_days)).toBe(true);
    expect(lastRunBody.custom_days).toContainEqual({ date: '2025-02-17', exclude: true });
    const override = lastRunBody.custom_days.find((c) => c.date === '2025-02-14');
    expect(override).toBeTruthy();
    expect(override.exclude).toBeUndefined(); // not excluded — no exclude key
    expect(override.entry.time).toBe('11:00');
    expect(override.entry.conditions).toContainEqual({ type: 'min_premium', points: 1.25 });
    expect(override.exit.time).toBe('14:00');
    // snap tolerance was never set on the override → omitted (partial).
    expect(override.entry.snap_tolerance_minutes).toBeUndefined();
  });

  it('hides the per-day override toggle when a custom day is excluded', async () => {
    await renderReady();
    fireEvent.change(screen.getByTestId('custom-day-input'), { target: { value: '2025-02-17' } });
    fireEvent.click(screen.getByTestId('add-custom-day'));

    // Not excluded → the override affordance is available.
    expect(screen.getByTestId('custom-day-toggle-2025-02-17')).toBeTruthy();

    // Toggle Exclude → the day won't be traded, so no entry/exit override.
    fireEvent.click(screen.getByLabelText('Exclude 2025-02-17'));
    expect(screen.queryByTestId('custom-day-toggle-2025-02-17')).toBeNull();
  });

  it('renders the days calendar grid, aggregate stats and the equity chart after Run', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    expect(screen.getAllByTestId('day-cell').length).toBe(3);
    expect(screen.getByText('February 2025')).toBeTruthy();

    const traded = document.querySelector('[data-testid="day-cell"][data-date="2025-02-03"]');
    expect(traded).toBeTruthy();
    expect(traded.textContent).toMatch(/\$/);
    expect(traded.textContent).toMatch(/3\b/);
    expect(traded.getAttribute('title')).toMatch(/5,?850/);
    expect(traded.getAttribute('data-outcome')).toBe('loss');

    const agg = screen.getByTestId('aggregate-stats');
    expect(agg.textContent).toMatch(/Sharpe/i);
    expect(agg.textContent).toMatch(/-0\.8/);

    const chart = screen.getByTestId('chart');
    expect(chart.getAttribute('data-points')).toBe('1');
    expect(chart.getAttribute('data-last')).toBe('-57.5');
  });

  it('surfaces per-leg fill detail in the day tooltip (independent legs)', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    const traded = document.querySelector('[data-testid="day-cell"][data-date="2025-02-03"]');
    const title = traded.getAttribute('title');
    // Both legs surfaced with their own fill times/prices.
    expect(title).toMatch(/call:/);
    expect(title).toMatch(/put:/);
    expect(title).toMatch(/15:03Z/); // call entry ts (differs from put's 15:01Z)
    expect(title).toMatch(/15:01Z/); // put entry ts
    // The put's exit fell back (exit_conditions_met=false) — surfaced.
    expect(title).toMatch(/exit=fallback/);
    expect(title).toMatch(/exit=ok/);
  });

  it('visibly flags data-gap skipped days with their skip_reason in the grid', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    const grid = screen.getByTestId('days-grid');
    const skipped = grid.querySelectorAll('[data-outcome="skipped"]');
    expect(skipped.length).toBe(1);
    expect(skipped[0].getAttribute('title')).toMatch(/no_quote_within_tolerance/);
  });

  it('renders an entry_conditions_unmet day as a distinct outcome', async () => {
    const UNMET = {
      ...RUN_RESPONSE,
      days: [
        {
          date: '2025-02-05', status: 'skipped', skip_reason: 'entry_conditions_unmet',
          expiry: null, strike: null, entry: null, exit: null, hedge_trades: [], pnl: null,
        },
      ],
    };
    installFetch({ progress: [{ status: 'done', days_done: 1, total_days: 1, result: UNMET, error: null }] });
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    const grid = screen.getByTestId('days-grid');
    const unmet = grid.querySelectorAll('[data-outcome="unmet"]');
    expect(unmet.length).toBe(1);
    const cell = unmet[0];
    // Distinct from a plain "skipped" data gap.
    expect(cell.getAttribute('data-outcome')).not.toBe('skipped');
    expect(cell.textContent).toMatch(/no entry/i);
    expect(cell.getAttribute('title')).toMatch(/entry_conditions_unmet/);
    // Not coloured as the warm data-gap "skipped" cell.
    expect(cell.className).not.toMatch(/cellSkipped/);
  });

  it('renders an excluded day as a distinct excluded cell (not a data-gap skip)', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    const grid = screen.getByTestId('days-grid');
    const excluded = grid.querySelectorAll('[data-outcome="excluded"]');
    expect(excluded.length).toBe(1);
    const cell = excluded[0];
    expect(cell.textContent).toMatch(/no trade/i);
    expect(cell.textContent).not.toMatch(/skipped/i);
    expect(cell.getAttribute('title')).toMatch(/excluded/i);
    expect(cell.getAttribute('data-date')).toBe('2025-02-17');
    expect(cell.className).not.toMatch(/cellSkipped/);
  });

  it('groups days into one calendar block per month and colours profit vs loss', async () => {
    const MULTI = {
      ...RUN_RESPONSE,
      days: [
        {
          date: '2025-02-28', status: 'ok', skip_reason: null, strike: 5900,
          pnl: { option_pnl_pts: -1, hedge_pnl_pts: 0.5, total_pnl_pts: -0.5, total_pnl_usd: -25 },
        },
        {
          date: '2025-03-03', status: 'ok', skip_reason: null, strike: 5950,
          pnl: { option_pnl_pts: 3, hedge_pnl_pts: -1, total_pnl_pts: 2, total_pnl_usd: 100 },
        },
        {
          date: '2025-03-04', status: 'skipped', skip_reason: 'no_quote_within_tolerance',
          strike: null, pnl: null,
        },
      ],
    };
    installFetch({
      progress: [{ status: 'done', days_done: 3, total_days: 3, result: MULTI, error: null }],
    });
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    expect(screen.getAllByTestId('month-block').length).toBe(2);
    expect(screen.getByText('February 2025')).toBeTruthy();
    expect(screen.getByText('March 2025')).toBeTruthy();

    const profit = document.querySelector('[data-testid="day-cell"][data-date="2025-03-03"]');
    const loss = document.querySelector('[data-testid="day-cell"][data-date="2025-02-28"]');
    expect(profit.getAttribute('data-outcome')).toBe('profit');
    expect(loss.getAttribute('data-outcome')).toBe('loss');
    expect(profit.className).toMatch(/\S/);
    expect(loss.className).toMatch(/\S/);
  });

  it('surfaces the warnings array', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('warnings')).toBeTruthy(), { timeout: 3000 });
    expect(screen.getByTestId('warnings').textContent).toMatch(/2 days skipped/);
  });

  it('shows live "X / N days" progress and disables Run while the job runs', async () => {
    installFetch({
      progress: [
        { status: 'running', days_done: 1, total_days: 3, result: null, error: null },
        { status: 'running', days_done: 2, total_days: 3, result: null, error: null },
      ],
    });
    await renderReady();

    const runBtn = screen.getByRole('button', { name: /run/i });
    fireEvent.click(runBtn);

    await waitFor(() => expect(runBtn.disabled).toBe(true), { timeout: 3000 });
    await waitFor(
      () => expect(screen.getByTestId('run-progress').textContent).toMatch(/[12] \/ 3 days/),
      { timeout: 3000 },
    );
    const fill = screen.getByTestId('run-progress-fill');
    expect(fill.style.width).toMatch(/^(33\.|66\.)/);
  });

  it('renders results once the job reports done', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });
    expect(screen.getAllByTestId('day-cell').length).toBe(3);
    expect(screen.queryByTestId('run-progress')).toBeNull();
    expect(screen.getByRole('button', { name: /run backtest/i }).disabled).toBe(false);
  });

  it('marks result cards non-shrink so the page scrolls instead of collapsing (Part A guard)', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    const cards = document.querySelectorAll('[data-result-card="true"]');
    expect(cards.length).toBeGreaterThanOrEqual(3);
    cards.forEach((c) => expect(c.className).toMatch(/resultCard/));
  });

  it('surfaces an error when the job reports status "error"', async () => {
    installFetch({
      progress: [
        { status: 'error', days_done: 0, total_days: 3, result: null, error: 'boom: dwh unreachable' },
      ],
    });
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));

    await waitFor(
      () => expect(screen.getByRole('alert').textContent).toMatch(/boom: dwh unreachable/),
      { timeout: 3000 },
    );
    expect(screen.queryByTestId('days-grid')).toBeNull();
    expect(screen.queryByTestId('run-progress')).toBeNull();
    expect(screen.getByRole('button', { name: /run backtest/i }).disabled).toBe(false);
  });

  // -------------------------------------------------------------------------
  // v3 — early-exit TRIGGERS on the EXIT module (DESIGN.md "Early-exit
  // TRIGGERS on the exit module (PINNED)"). Triggers are EXIT-ONLY.
  // -------------------------------------------------------------------------
  it('shows the Triggers builder on the EXIT module only (not entry)', async () => {
    await renderReady();
    // Exit module has the triggers builder + empty hint.
    expect(screen.getByTestId('exit-triggers-block')).toBeTruthy();
    expect(screen.getByTestId('exit-add-trigger')).toBeTruthy();
    expect(screen.getByTestId('exit-triggers-empty')).toBeTruthy();
    // Entry module has NONE.
    expect(screen.queryByTestId('entry-triggers-block')).toBeNull();
    expect(screen.queryByTestId('entry-add-trigger')).toBeNull();
  });

  it('adding each trigger type reveals its type-specific params', async () => {
    await renderReady();

    // underlying_move → amount + unit.
    fireEvent.change(screen.getByTestId('exit-add-trigger'), { target: { value: 'underlying_move' } });
    expect(screen.getByTestId('exit-trigger-underlying_move')).toBeTruthy();
    expect(screen.getByLabelText('exit underlying_move amount')).toBeTruthy();
    expect(screen.getByLabelText('exit underlying_move unit')).toBeTruthy();

    // sigma_move → n.
    fireEvent.change(screen.getByTestId('exit-add-trigger'), { target: { value: 'sigma_move' } });
    expect(screen.getByLabelText('exit sigma_move n')).toBeTruthy();

    // net_delta → threshold.
    fireEvent.change(screen.getByTestId('exit-add-trigger'), { target: { value: 'net_delta' } });
    expect(screen.getByLabelText('exit net_delta threshold')).toBeTruthy();

    // pnl → amount + unit + direction.
    fireEvent.change(screen.getByTestId('exit-add-trigger'), { target: { value: 'pnl' } });
    expect(screen.getByLabelText('exit pnl amount')).toBeTruthy();
    expect(screen.getByLabelText('exit pnl unit')).toBeTruthy();
    expect(screen.getByLabelText('exit pnl direction')).toBeTruthy();

    // Removing the underlying_move row drops just that trigger.
    fireEvent.click(screen.getByLabelText('Remove Underlying move trigger'));
    expect(screen.queryByTestId('exit-trigger-underlying_move')).toBeNull();
    expect(screen.getByTestId('exit-trigger-pnl')).toBeTruthy();
  });

  it('serializes exit.triggers to the PINNED shapes and leaves entry with none', async () => {
    await renderReady();

    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-03-31' } });

    // sigma_move (n) + a directional pnl (amount + unit + direction).
    fireEvent.change(screen.getByTestId('exit-add-trigger'), { target: { value: 'sigma_move' } });
    fireEvent.change(screen.getByLabelText('exit sigma_move n'), { target: { value: '1.5' } });
    fireEvent.change(screen.getByTestId('exit-add-trigger'), { target: { value: 'pnl' } });
    fireEvent.change(screen.getByLabelText('exit pnl amount'), { target: { value: '750' } });
    fireEvent.change(screen.getByLabelText('exit pnl unit'), { target: { value: 'usd' } });
    fireEvent.change(screen.getByLabelText('exit pnl direction'), { target: { value: 'loss' } });

    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(lastRunBody).not.toBeNull());

    // Exit carries the pinned trigger shapes.
    expect(Array.isArray(lastRunBody.exit.triggers)).toBe(true);
    expect(lastRunBody.exit.triggers).toContainEqual({ type: 'sigma_move', n: 1.5 });
    expect(lastRunBody.exit.triggers).toContainEqual({
      type: 'pnl', amount: 750, unit: 'usd', direction: 'loss',
    });
    // Entry has no triggers key — triggers are exit-only.
    expect(lastRunBody.entry.triggers).toBeUndefined();
  });

  it('renders the firing-trigger marker + tooltip for a day with exit_trigger', async () => {
    await renderReady();
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }));
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy(), { timeout: 3000 });

    const traded = document.querySelector('[data-testid="day-cell"][data-date="2025-02-03"]');
    // Marker present + type surfaced on the cell.
    expect(traded.getAttribute('data-exit-trigger')).toBe('sigma_move');
    expect(traded.querySelector('[data-testid="trigger-marker"]')).toBeTruthy();
    // Tooltip names the firing trigger + its time.
    const title = traded.getAttribute('title');
    expect(title).toMatch(/exited early/i);
    expect(title).toMatch(/sigma_move/);
    expect(title).toMatch(/19:12Z/);

    // A normal time-exit day (exit_trigger null/absent) has no marker.
    const excluded = document.querySelector('[data-testid="day-cell"][data-date="2025-02-17"]');
    expect(excluded.querySelector('[data-testid="trigger-marker"]')).toBeNull();
    expect(excluded.getAttribute('data-exit-trigger')).toBeNull();
  });
});
