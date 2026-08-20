// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

afterEach(cleanup);

// Chart pulls in Plotly (canvas) — stub it so jsdom doesn't choke, and so we
// can assert the bar trace handed to it (same pattern as
// RegimeSensitivityView.test.jsx / WeekdayAttributionView.test.jsx).
vi.mock('../../components/Chart', () => ({
  default: ({ traces, downloadFilename }) => (
    <div
      data-testid="event-attribution-chart"
      data-fn={downloadFilename}
      data-x={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].x : [])}
      data-y={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].y : [])}
    />
  ),
}));

import EventAttributionView from './EventAttributionView';

function tradedDay(date, usd) {
  return { date, status: 'ok', pnl: { option_pnl_pts: usd / 10, hedge_pnl_pts: 0, total_pnl_pts: usd / 10, total_pnl_usd: usd } };
}

function calendar(events) {
  return { event_types: ['FOMC', 'NFP', 'CPI'], events, all_dates: [], tentative_dates: [] };
}

const DAYS = [
  tradedDay('2025-01-29', 100), // FOMC
  tradedDay('2025-02-07', -40), // NFP
  tradedDay('2025-02-12', 60), // CPI
  tradedDay('2025-02-13', 10), // non-event
];

const CAL = calendar({
  FOMC: [{ date: '2025-01-29', tentative: false }],
  NFP: [{ date: '2025-02-07', tentative: false }],
  CPI: [{ date: '2025-02-12', tentative: false }],
});

describe('EventAttributionView — event calendar present with overlap', () => {
  it('mounts and shows a bucket row per event type plus the event/non-event comparison, each with N', () => {
    render(<EventAttributionView days={DAYS} eventCalendar={CAL} />);
    expect(screen.getByTestId('event-attribution-view')).toBeTruthy();
    expect(screen.getByTestId('event-attribution-row-FOMC').dataset.n).toBe('1');
    expect(screen.getByTestId('event-attribution-row-NFP').dataset.n).toBe('1');
    expect(screen.getByTestId('event-attribution-row-CPI').dataset.n).toBe('1');
    expect(screen.getByTestId('event-attribution-row-event').dataset.n).toBe('3');
    expect(screen.getByTestId('event-attribution-row-non_event').dataset.n).toBe('1');
  });

  it('renders the bar chart with one bar per bucket (3 type buckets + 2 comparison buckets)', () => {
    render(<EventAttributionView days={DAYS} eventCalendar={CAL} />);
    const chart = screen.getByTestId('event-attribution-chart');
    expect(JSON.parse(chart.dataset.x)).toEqual(['FOMC', 'NFP', 'CPI', 'Event day', 'Non-event day']);
    expect(JSON.parse(chart.dataset.y)).toEqual([100, -40, 60, 120, 10]);
  });

  it('surfaces the small-sample robustness caveat mentioning multi-membership handling', () => {
    render(<EventAttributionView days={DAYS} eventCalendar={CAL} />);
    const caveat = screen.getByTestId('event-attribution-caveat').textContent;
    expect(caveat).toMatch(/sample/i);
  });

  it('counts a multi-membership day in each matching type row but once in the comparison', () => {
    const overlapDays = [tradedDay('2025-06-18', 200), tradedDay('2025-07-01', 5)];
    const overlapCal = calendar({
      FOMC: [{ date: '2025-06-18', tentative: false }],
      NFP: [],
      CPI: [{ date: '2025-06-18', tentative: false }],
    });
    render(<EventAttributionView days={overlapDays} eventCalendar={overlapCal} />);
    expect(screen.getByTestId('event-attribution-row-FOMC').dataset.n).toBe('1');
    expect(screen.getByTestId('event-attribution-row-CPI').dataset.n).toBe('1');
    expect(screen.getByTestId('event-attribution-row-NFP').dataset.n).toBe('0');
    expect(screen.getByTestId('event-attribution-row-event').dataset.n).toBe('1');
    expect(screen.getByTestId('event-attribution-row-non_event').dataset.n).toBe('1');
  });
});

describe('EventAttributionView — calendar unavailable / no overlap', () => {
  it('shows a concise hint instead of a chart when the calendar failed to load (null)', () => {
    render(<EventAttributionView days={DAYS} eventCalendar={null} />);
    expect(screen.getByTestId('event-attribution-view')).toBeTruthy();
    expect(screen.getByTestId('event-attribution-hint')).toBeTruthy();
    expect(screen.queryByTestId('event-attribution-chart')).toBeNull();
    expect(screen.queryByTestId('event-attribution-table')).toBeNull();
  });

  it('shows a concise hint when the calendar loaded but has no overlap with this run', () => {
    const cal = calendar({ FOMC: [{ date: '2025-06-18', tentative: false }] });
    render(<EventAttributionView days={DAYS} eventCalendar={cal} />);
    expect(screen.getByTestId('event-attribution-hint')).toBeTruthy();
    expect(screen.queryByTestId('event-attribution-chart')).toBeNull();
  });

  it('shows the hint for an empty days array too', () => {
    render(<EventAttributionView days={[]} eventCalendar={CAL} />);
    expect(screen.getByTestId('event-attribution-hint')).toBeTruthy();
  });

  it('renders gracefully with absent (undefined) days and eventCalendar props', () => {
    render(<EventAttributionView />);
    expect(screen.getByTestId('event-attribution-view')).toBeTruthy();
    expect(screen.getByTestId('event-attribution-hint')).toBeTruthy();
  });
});
