import { describe, it, expect } from 'vitest';
import { groupPnlByWeekday, WEEKDAY_LABELS } from './weekdayAttribution';

// One traded day per weekday, spanning two calendar weeks, with distinct
// known PnL values so sum/mean/median/win-rate are hand-checkable.
//
// Week 1 (2025-02-03 Mon .. 2025-02-07 Fri):
//   Mon 2025-02-03: +100
//   Tue 2025-02-04: -50
//   Wed 2025-02-05: +200
//   Thu 2025-02-06: +300
//   Fri 2025-02-07: -400
// Week 2 (2025-02-10 Mon .. 2025-02-14 Fri):
//   Mon 2025-02-10: -20
//   Tue 2025-02-11: +80
//   Wed 2025-02-12: +150
//   Thu 2025-02-13: -100
//   Fri 2025-02-14: +500
function tradedDay(date, usd, pts = usd / 10) {
  return {
    date,
    status: 'ok',
    pnl: { option_pnl_pts: pts * 0.6, hedge_pnl_pts: pts * 0.4, total_pnl_pts: pts, total_pnl_usd: usd },
  };
}

function skippedDay(date, reason = 'no_quote_within_tolerance') {
  return { date, status: 'skipped', skip_reason: reason, pnl: null };
}

const MULTI_WEEK_DAYS = [
  tradedDay('2025-02-03', 100),
  tradedDay('2025-02-04', -50),
  tradedDay('2025-02-05', 200),
  tradedDay('2025-02-06', 300),
  tradedDay('2025-02-07', -400),
  tradedDay('2025-02-10', -20),
  tradedDay('2025-02-11', 80),
  tradedDay('2025-02-12', 150),
  tradedDay('2025-02-13', -100),
  tradedDay('2025-02-14', 500),
];

describe('groupPnlByWeekday', () => {
  it('returns one bucket per weekday, Mon..Fri, in order', () => {
    const buckets = groupPnlByWeekday(MULTI_WEEK_DAYS);
    expect(buckets.map((b) => b.weekday)).toEqual(WEEKDAY_LABELS);
    expect(WEEKDAY_LABELS).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri']);
  });

  it('lands each date in the correct weekday bucket (count)', () => {
    const buckets = groupPnlByWeekday(MULTI_WEEK_DAYS);
    const byLabel = Object.fromEntries(buckets.map((b) => [b.weekday, b]));
    expect(byLabel.Mon.n).toBe(2);
    expect(byLabel.Tue.n).toBe(2);
    expect(byLabel.Wed.n).toBe(2);
    expect(byLabel.Thu.n).toBe(2);
    expect(byLabel.Fri.n).toBe(2);
  });

  it('computes sum, mean, median and win rate per weekday', () => {
    const buckets = groupPnlByWeekday(MULTI_WEEK_DAYS);
    const byLabel = Object.fromEntries(buckets.map((b) => [b.weekday, b]));

    // Mon: 100, -20 -> sum 80, mean 40, median 40 (avg of the two), win rate 0.5
    expect(byLabel.Mon.sumUsd).toBeCloseTo(80);
    expect(byLabel.Mon.meanUsd).toBeCloseTo(40);
    expect(byLabel.Mon.medianUsd).toBeCloseTo(40);
    expect(byLabel.Mon.winRate).toBeCloseTo(0.5);

    // Wed: 200, 150 -> sum 350, mean 175, median 175, win rate 1.0
    expect(byLabel.Wed.sumUsd).toBeCloseTo(350);
    expect(byLabel.Wed.meanUsd).toBeCloseTo(175);
    expect(byLabel.Wed.medianUsd).toBeCloseTo(175);
    expect(byLabel.Wed.winRate).toBeCloseTo(1.0);

    // Fri: -400, 500 -> sum 100, mean 50, win rate 0.5
    expect(byLabel.Fri.sumUsd).toBeCloseTo(100);
    expect(byLabel.Fri.meanUsd).toBeCloseTo(50);
    expect(byLabel.Fri.winRate).toBeCloseTo(0.5);
  });

  it('also aggregates the pts leg of PnL alongside usd', () => {
    const buckets = groupPnlByWeekday([tradedDay('2025-02-03', 100, 10), tradedDay('2025-02-10', -20, -2)]);
    const mon = buckets.find((b) => b.weekday === 'Mon');
    expect(mon.sumPts).toBeCloseTo(8);
    expect(mon.meanPts).toBeCloseTo(4);
  });

  it('excludes non-traded days (skipped/excluded/no pnl) from the stats', () => {
    const days = [
      tradedDay('2025-02-03', 100), // Mon
      skippedDay('2025-02-04'), // Tue - skipped, no pnl
      { date: '2025-02-17', status: 'excluded', skip_reason: 'excluded', pnl: null }, // Mon
    ];
    const buckets = groupPnlByWeekday(days);
    const byLabel = Object.fromEntries(buckets.map((b) => [b.weekday, b]));
    expect(byLabel.Mon.n).toBe(1); // only the traded Monday counts
    expect(byLabel.Tue.n).toBe(0);
  });

  it('handles an empty days array without throwing, all buckets zeroed', () => {
    const buckets = groupPnlByWeekday([]);
    for (const b of buckets) {
      expect(b.n).toBe(0);
      expect(b.sumUsd).toBe(0);
      expect(b.meanUsd).toBeNull();
      expect(b.medianUsd).toBeNull();
      expect(b.winRate).toBeNull();
    }
  });

  it('handles undefined/null days input the same as empty', () => {
    expect(() => groupPnlByWeekday(undefined)).not.toThrow();
    expect(() => groupPnlByWeekday(null)).not.toThrow();
    expect(groupPnlByWeekday(undefined).every((b) => b.n === 0)).toBe(true);
  });

  it('tolerates null/malformed entries mixed into the days array', () => {
    const days = [
      null,
      undefined,
      {},
      { date: 'not-a-date', pnl: { total_pnl_usd: 5, total_pnl_pts: 1 } },
      tradedDay('2025-02-05', 200), // Wed
    ];
    const buckets = groupPnlByWeekday(days);
    const wed = buckets.find((b) => b.weekday === 'Wed');
    expect(wed.n).toBe(1);
    expect(wed.sumUsd).toBeCloseTo(200);
    const total = buckets.reduce((acc, b) => acc + b.n, 0);
    expect(total).toBe(1);
  });

  it('ignores weekend dates (defensive — should never occur in trading data)', () => {
    // 2025-02-08 is a Saturday, 2025-02-09 is a Sunday.
    const days = [
      { date: '2025-02-08', pnl: { total_pnl_usd: 999, total_pnl_pts: 99 } },
      { date: '2025-02-09', pnl: { total_pnl_usd: 999, total_pnl_pts: 99 } },
      tradedDay('2025-02-05', 200), // Wed
    ];
    const buckets = groupPnlByWeekday(days);
    const total = buckets.reduce((acc, b) => acc + b.n, 0);
    expect(total).toBe(1);
  });
});
