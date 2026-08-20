// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';

import LadderEntriesView from './LadderEntriesView';
import { groupPnlByWeekday } from './weekdayAttribution';

afterEach(cleanup);

// A laddered-run response: two days, each carrying a day-AGGREGATE pnl (the sum
// of its rungs) plus per-rung `entries`. This is the exact shape run_backtest
// emits for a laddered run.
const LADDERED_DAYS = [
  {
    date: '2025-02-03',
    status: 'ok',
    pnl: { total_pnl_usd: 300, total_pnl_pts: 6, option_pnl_pts: 6, hedge_pnl_pts: 0 },
    entries: [
      {
        date: '2025-02-03', status: 'ok', strike: 5000,
        entry_ts: '2025-02-03T15:00:00Z', contracts: 1,
        pnl: { total_pnl_usd: 100 }, weighted_pnl_usd: 100,
        hedge_trades: [],
      },
      {
        date: '2025-02-03', status: 'ok', strike: 5005,
        entry_ts: '2025-02-03T15:30:00Z', contracts: 1,
        pnl: { total_pnl_usd: 200 }, weighted_pnl_usd: 200,
        hedge_trades: [],
      },
    ],
  },
  {
    date: '2025-02-04',
    status: 'ok',
    pnl: { total_pnl_usd: -50, total_pnl_pts: -1, option_pnl_pts: -1, hedge_pnl_pts: 0 },
    entries: [
      {
        date: '2025-02-04', status: 'ok', strike: 5010,
        entry_ts: '2025-02-04T15:00:00Z', contracts: 2,
        pnl: { total_pnl_usd: -25 }, weighted_pnl_usd: -50,
        hedge_trades: [],
      },
      {
        date: '2025-02-04', status: 'skipped', skip_reason: 'max_concurrent',
        entry_ts: '2025-02-04T15:30:00Z', contracts: 2,
        pnl: null, weighted_pnl_usd: 0,
      },
    ],
  },
];

describe('LadderEntriesView', () => {
  it('renders nothing for a non-laddered run (no entries key)', () => {
    const { container } = render(
      <LadderEntriesView days={[{ date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 5 } }]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing for empty/undefined days', () => {
    const { container } = render(<LadderEntriesView days={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders a per-rung table for each laddered day with the day total', () => {
    render(<LadderEntriesView days={LADDERED_DAYS} />);
    expect(screen.getByTestId('ladder-entries-view')).toBeTruthy();

    // Day 1 total + 2 rung rows.
    expect(screen.getByTestId('ladder-day-total-2025-02-03').textContent).toContain('300');
    const day1 = screen.getByTestId('ladder-day-2025-02-03');
    const rows1 = within(day1).getAllByTestId('ladder-entry-row');
    expect(rows1).toHaveLength(2);
    // Contributions sum to the day total.
    const contribs = within(day1)
      .getAllByTestId('ladder-entry-contribution')
      .map((el) => el.textContent);
    expect(contribs).toEqual(['$100.00', '$200.00']);

    // Day 2: a capped rung is surfaced as its skip reason, not "traded".
    const day2 = screen.getByTestId('ladder-day-2025-02-04');
    const rows2 = within(day2).getAllByTestId('ladder-entry-row');
    expect(rows2).toHaveLength(2);
    expect(rows2[1].getAttribute('data-status')).toBe('skipped');
    expect(rows2[1].textContent).toContain('max_concurrent');
  });

  it('A1 weekday attribution still consumes the retained day aggregate over a laddered run', () => {
    // The NON-NEGOTIABLE invariant: the one-row-per-day aggregate keeps working.
    const buckets = groupPnlByWeekday(LADDERED_DAYS);
    const mon = buckets.find((b) => b.weekday === 'Mon'); // 2025-02-03
    const tue = buckets.find((b) => b.weekday === 'Tue'); // 2025-02-04
    expect(mon.n).toBe(1);
    expect(mon.sumUsd).toBe(300); // the laddered day's AGGREGATE, not a rung
    expect(tue.n).toBe(1);
    expect(tue.sumUsd).toBe(-50);
  });
});
