// @vitest-environment jsdom
import {
  describe, it, expect, vi, beforeEach, afterEach,
} from 'vitest';
import {
  render, screen, fireEvent, waitFor, cleanup,
} from '@testing-library/react';

afterEach(cleanup);

// Chart pulls in Plotly (canvas) — stub it (mirrors IntradayBacktestPage.test).
vi.mock('../../components/Chart', () => ({
  default: ({ downloadFilename }) => <div data-testid="chart" data-fn={downloadFilename} />,
}));

// Mock the API client so we can drive meta + control the cache-get HIT/MISS and
// assert it is called with the loaded config's payload.
vi.mock('../../api/intradayBacktest', () => ({
  getIntradayBacktestMeta: vi.fn(),
  startIntradayBacktest: vi.fn(),
  getIntradayBacktestProgress: vi.fn(),
  getIntradayBacktestCachedResult: vi.fn(),
}));

import {
  getIntradayBacktestMeta,
  getIntradayBacktestCachedResult,
} from '../../api/intradayBacktest';
import IntradayBacktestPage from './IntradayBacktestPage';

const META = {
  window: { min_date: '2025-01-01', max_date: '2026-07-31' },
  expiry_modes: ['0DTE', 'NDTE'],
  roots: ['OPT_SP_500_EW'],
  hedge_instrument: 'FUT_SP_500',
  multiplier: 50,
  timezone: 'America/New_York',
};

const CACHED_RESULT = {
  params_echo: {},
  window: { min_date: '2025-01-01', max_date: '2026-07-31' },
  days: [
    {
      date: '2025-02-03', status: 'ok', skip_reason: null, strike: 5850,
      pnl: { option_pnl_pts: -1, hedge_pnl_pts: 0.5, total_pnl_pts: -0.5, total_pnl_usd: -57.5 },
    },
  ],
  aggregate: {
    n_days: 1, n_traded: 1, n_skipped: 0,
    total_pnl_usd: -57.5, mean_daily_pnl_usd: -57.5, win_rate: 0,
    sharpe: -0.8, max_drawdown_usd: -57.5,
    equity_curve: [{ date: '2025-02-03', cum_pnl_usd: -57.5 }],
  },
  warnings: [],
  from_cache: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  // Overwrite-confirm is auto-approved in these tests.
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  getIntradayBacktestMeta.mockResolvedValue(META);
  getIntradayBacktestCachedResult.mockResolvedValue({ cached: false });
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function renderReady() {
  render(<IntradayBacktestPage />);
  await waitFor(() => expect(screen.getByLabelText('Start date')).toBeTruthy());
}

describe('IntradayBacktest save/load simulations', () => {
  it('saving then loading restores inputs exactly and calls cache-get; a HIT renders results', async () => {
    getIntradayBacktestCachedResult.mockResolvedValue(CACHED_RESULT);
    await renderReady();

    // Configure a distinctive input set, then save under a new name.
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-10' } });
    fireEvent.change(screen.getByLabelText(/straddle side/i), { target: { value: 'short' } });
    fireEvent.change(screen.getByTestId('sim-name-input'), { target: { value: 'My sim' } });
    fireEvent.click(screen.getByTestId('save-sim'));

    // It appears in the saved list; freshly saved ⇒ not dirty.
    const rows = await screen.findAllByTestId('saved-sim-row');
    expect(rows.length).toBe(1);
    expect(rows[0].textContent).toMatch(/My sim/);
    expect(screen.getByTestId('dirty-indicator').getAttribute('data-dirty')).toBe('false');

    // Change the inputs away from the saved snapshot → dirty marker appears.
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-03-01' } });
    fireEvent.change(screen.getByLabelText(/straddle side/i), { target: { value: 'long' } });
    expect(screen.getByTestId('dirty-indicator').getAttribute('data-dirty')).toBe('true');

    // Load the saved sim → inputs restored EXACTLY.
    fireEvent.click(screen.getByLabelText('Load My sim'));
    await waitFor(() => {
      expect(screen.getByLabelText('Start date').value).toBe('2025-02-10');
    });
    expect(screen.getByLabelText(/straddle side/i).value).toBe('short');
    // Back in sync with the loaded snapshot ⇒ not dirty.
    expect(screen.getByTestId('dirty-indicator').getAttribute('data-dirty')).toBe('false');

    // cache-get was called with the LOADED config's payload.
    expect(getIntradayBacktestCachedResult).toHaveBeenCalled();
    const payload = getIntradayBacktestCachedResult.mock.calls[0][0];
    expect(payload.start_date).toBe('2025-02-10');
    expect(payload.straddle_side).toBe('short');

    // HIT → results render (aggregate + days grid) without a Run.
    await waitFor(() => expect(screen.getByTestId('days-grid')).toBeTruthy());
    expect(screen.getByTestId('aggregate-stats').textContent).toMatch(/-0\.8/);
  });

  it('a cache MISS on load leaves results empty', async () => {
    getIntradayBacktestCachedResult.mockResolvedValue({ cached: false });
    await renderReady();

    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-10' } });
    fireEvent.change(screen.getByTestId('sim-name-input'), { target: { value: 'Miss sim' } });
    fireEvent.click(screen.getByTestId('save-sim'));
    await screen.findAllByTestId('saved-sim-row');

    fireEvent.click(screen.getByLabelText('Load Miss sim'));
    await waitFor(() => expect(getIntradayBacktestCachedResult).toHaveBeenCalled());

    // No results rendered on a miss.
    expect(screen.queryByTestId('days-grid')).toBeNull();
    expect(screen.queryByTestId('aggregate-stats')).toBeNull();
  });

  it('deletes a saved sim', async () => {
    await renderReady();
    fireEvent.change(screen.getByTestId('sim-name-input'), { target: { value: 'ToDelete' } });
    fireEvent.click(screen.getByTestId('save-sim'));
    expect((await screen.findAllByTestId('saved-sim-row')).length).toBe(1);

    fireEvent.click(screen.getByLabelText('Delete ToDelete'));
    await waitFor(() => expect(screen.queryByTestId('saved-sim-row')).toBeNull());
    expect(screen.getByTestId('saved-sims-empty')).toBeTruthy();
  });

  it('no dirty indicator until a sim is loaded or saved', async () => {
    await renderReady();
    // Nothing loaded → no indicator, even after editing.
    expect(screen.queryByTestId('dirty-indicator')).toBeNull();
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-02-10' } });
    expect(screen.queryByTestId('dirty-indicator')).toBeNull();
  });
});
