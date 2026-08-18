// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

afterEach(cleanup);

// Chart pulls in Plotly (canvas) — stub it so jsdom doesn't choke, and so we
// can assert the bar trace handed to it (same pattern as
// IntradayBacktestPage.test.jsx).
vi.mock('../../components/Chart', () => ({
  default: ({ traces, downloadFilename }) => (
    <div
      data-testid="weekday-chart"
      data-fn={downloadFilename}
      data-x={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].x : [])}
      data-y={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].y : [])}
    />
  ),
}));

import WeekdayAttributionView from './WeekdayAttributionView';

function tradedDay(date, usd, pts = usd / 10) {
  return {
    date,
    status: 'ok',
    pnl: { option_pnl_pts: pts * 0.6, hedge_pnl_pts: pts * 0.4, total_pnl_pts: pts, total_pnl_usd: usd },
  };
}

function skippedDay(date) {
  return { date, status: 'skipped', skip_reason: 'no_quote_within_tolerance', pnl: null };
}

const DAYS = [
  tradedDay('2025-02-03', 100), // Mon
  tradedDay('2025-02-04', -50), // Tue
  tradedDay('2025-02-05', 200), // Wed
  tradedDay('2025-02-06', 300), // Thu
  tradedDay('2025-02-07', -400), // Fri
  tradedDay('2025-02-10', -20), // Mon
  skippedDay('2025-02-11'), // Tue — no pnl, excluded from stats
];

describe('WeekdayAttributionView', () => {
  it('mounts and renders the bar chart with one point per weekday', () => {
    render(<WeekdayAttributionView days={DAYS} />);
    const chart = screen.getByTestId('weekday-chart');
    expect(JSON.parse(chart.dataset.x)).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri']);
  });

  it('shows the per-weekday sample size N so thin buckets are visible', () => {
    render(<WeekdayAttributionView days={DAYS} />);
    // Mon has n=2, Tue has n=1 (one traded + one skipped-excluded), Wed/Thu/Fri n=1.
    const table = screen.getByTestId('weekday-attribution-table');
    const monRow = screen.getByTestId('weekday-row-Mon');
    const tueRow = screen.getByTestId('weekday-row-Tue');
    expect(table).toBeTruthy();
    expect(monRow.dataset.n).toBe('2');
    expect(tueRow.dataset.n).toBe('1');
  });

  it('renders a row per weekday even when a weekday has zero traded days', () => {
    render(<WeekdayAttributionView days={[tradedDay('2025-02-05', 200)]} />);
    for (const wd of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']) {
      expect(screen.getByTestId(`weekday-row-${wd}`)).toBeTruthy();
    }
    expect(screen.getByTestId('weekday-row-Mon').dataset.n).toBe('0');
  });

  it('renders gracefully with an empty days array (no crash, N=0 everywhere)', () => {
    render(<WeekdayAttributionView days={[]} />);
    expect(screen.getByTestId('weekday-attribution-view')).toBeTruthy();
    for (const wd of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']) {
      expect(screen.getByTestId(`weekday-row-${wd}`).dataset.n).toBe('0');
    }
  });

  it('renders gracefully with an absent (undefined) days prop', () => {
    render(<WeekdayAttributionView />);
    expect(screen.getByTestId('weekday-attribution-view')).toBeTruthy();
  });

  it('surfaces the small-sample robustness caveat in the view', () => {
    render(<WeekdayAttributionView days={DAYS} />);
    expect(screen.getByTestId('weekday-attribution-caveat').textContent).toMatch(/sample/i);
  });
});
