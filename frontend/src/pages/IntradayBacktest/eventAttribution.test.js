import { describe, it, expect } from 'vitest';
import { computeEventAttribution, EVENT_TYPES } from './eventAttribution';

function tradedDay(date, usd) {
  return { date, status: 'ok', pnl: { option_pnl_pts: usd / 10, hedge_pnl_pts: 0, total_pnl_pts: usd / 10, total_pnl_usd: usd } };
}

function calendar(events) {
  return { event_types: EVENT_TYPES, events, all_dates: [], tentative_dates: [] };
}

describe('computeEventAttribution — no calendar data', () => {
  it('returns unavailable with reason no_calendar for a null calendar', () => {
    const result = computeEventAttribution([tradedDay('2025-02-03', 100)], null);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_calendar');
    expect(result.typeBuckets).toEqual([]);
    expect(result.comparison).toEqual([]);
  });

  it('returns unavailable with reason no_calendar for an undefined calendar', () => {
    const result = computeEventAttribution([tradedDay('2025-02-03', 100)], undefined);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_calendar');
  });

  it('returns unavailable with reason no_calendar for a malformed calendar (no events object)', () => {
    const result = computeEventAttribution([tradedDay('2025-02-03', 100)], { event_types: [] });
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_calendar');
  });

  it('does not throw for undefined/null days', () => {
    expect(() => computeEventAttribution(undefined, calendar({}))).not.toThrow();
    expect(() => computeEventAttribution(null, calendar({}))).not.toThrow();
  });
});

describe('computeEventAttribution — no overlap', () => {
  it('returns unavailable with reason no_overlap when the calendar has no dates matching the run', () => {
    const days = [tradedDay('2025-02-03', 100), tradedDay('2025-02-04', -50)];
    const cal = calendar({ FOMC: [{ date: '2025-06-18', tentative: false }] });
    const result = computeEventAttribution(days, cal);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_overlap');
  });

  it('returns unavailable with reason no_overlap when the calendar has empty event arrays', () => {
    const days = [tradedDay('2025-02-03', 100)];
    const cal = calendar({ FOMC: [], NFP: [], CPI: [] });
    const result = computeEventAttribution(days, cal);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_overlap');
  });

  it('returns unavailable with reason no_overlap when there are no traded days at all', () => {
    const cal = calendar({ FOMC: [{ date: '2025-02-03', tentative: false }] });
    const result = computeEventAttribution([], cal);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_overlap');
  });
});

describe('computeEventAttribution — per-type bucketing', () => {
  const days = [
    tradedDay('2025-01-29', 100), // FOMC
    tradedDay('2025-02-07', -40), // NFP
    tradedDay('2025-02-12', 60), // CPI
    tradedDay('2025-02-13', 10), // non-event
  ];
  const cal = calendar({
    FOMC: [{ date: '2025-01-29', tentative: false }],
    NFP: [{ date: '2025-02-07', tentative: false }],
    CPI: [{ date: '2025-02-12', tentative: false }],
  });

  it('buckets exactly one type entry per EVENT_TYPES, in order', () => {
    const result = computeEventAttribution(days, cal);
    expect(result.available).toBe(true);
    expect(result.typeBuckets.map((b) => b.key)).toEqual(['FOMC', 'NFP', 'CPI']);
  });

  it('assigns each event day only to its matching type bucket, with correct N and sum', () => {
    const result = computeEventAttribution(days, cal);
    const byKey = Object.fromEntries(result.typeBuckets.map((b) => [b.key, b]));
    expect(byKey.FOMC.n).toBe(1);
    expect(byKey.FOMC.sumUsd).toBeCloseTo(100);
    expect(byKey.NFP.n).toBe(1);
    expect(byKey.NFP.sumUsd).toBeCloseTo(-40);
    expect(byKey.CPI.n).toBe(1);
    expect(byKey.CPI.sumUsd).toBeCloseTo(60);
  });

  it('excludes a non-event day from every type bucket', () => {
    const result = computeEventAttribution(days, cal);
    const totalTypeN = result.typeBuckets.reduce((acc, b) => acc + b.n, 0);
    expect(totalTypeN).toBe(3); // the 2025-02-13 non-event day contributes to none
  });

  it('computes mean and win rate per type bucket', () => {
    const twoFomcDays = [tradedDay('2025-01-29', 100), tradedDay('2026-01-28', -20)];
    const twoFomcCal = calendar({
      FOMC: [{ date: '2025-01-29', tentative: false }, { date: '2026-01-28', tentative: false }],
    });
    const result = computeEventAttribution(twoFomcDays, twoFomcCal);
    const fomc = result.typeBuckets.find((b) => b.key === 'FOMC');
    expect(fomc.n).toBe(2);
    expect(fomc.sumUsd).toBeCloseTo(80);
    expect(fomc.meanUsd).toBeCloseTo(40);
    expect(fomc.winRate).toBeCloseTo(0.5);
  });
});

describe('computeEventAttribution — any-event vs non-event split', () => {
  it('partitions traded days into event/non_event with correct N', () => {
    const days = [
      tradedDay('2025-01-29', 100), // FOMC
      tradedDay('2025-02-07', -40), // NFP
      tradedDay('2025-02-13', 10), // non-event
      tradedDay('2025-02-14', 5), // non-event
    ];
    const cal = calendar({
      FOMC: [{ date: '2025-01-29', tentative: false }],
      NFP: [{ date: '2025-02-07', tentative: false }],
    });
    const result = computeEventAttribution(days, cal);
    const byKey = Object.fromEntries(result.comparison.map((b) => [b.key, b]));
    expect(byKey.event.n).toBe(2);
    expect(byKey.event.sumUsd).toBeCloseTo(60);
    expect(byKey.non_event.n).toBe(2);
    expect(byKey.non_event.sumUsd).toBeCloseTo(15);
    expect(byKey.event.n + byKey.non_event.n).toBe(days.length);
  });

  it('counts a multi-membership day (FOMC+CPI overlap) in EACH matching type bucket but ONCE in the event/non_event split', () => {
    const days = [
      tradedDay('2025-06-18', 200), // FOMC + CPI same day (rare overlap)
      tradedDay('2025-07-01', 5), // non-event
    ];
    const cal = calendar({
      FOMC: [{ date: '2025-06-18', tentative: false }],
      NFP: [],
      CPI: [{ date: '2025-06-18', tentative: false }],
    });
    const result = computeEventAttribution(days, cal);
    const byKey = Object.fromEntries(result.typeBuckets.map((b) => [b.key, b]));
    // Counted in BOTH matching type buckets.
    expect(byKey.FOMC.n).toBe(1);
    expect(byKey.FOMC.sumUsd).toBeCloseTo(200);
    expect(byKey.CPI.n).toBe(1);
    expect(byKey.CPI.sumUsd).toBeCloseTo(200);
    expect(byKey.NFP.n).toBe(0);
    // Sum of type-bucket Ns (2) exceeds the any-event N (1) precisely because
    // of the overlap.
    const totalTypeN = result.typeBuckets.reduce((acc, b) => acc + b.n, 0);
    expect(totalTypeN).toBe(2);
    const comparisonByKey = Object.fromEntries(result.comparison.map((b) => [b.key, b]));
    expect(comparisonByKey.event.n).toBe(1); // counted ONCE in the split
    expect(comparisonByKey.event.sumUsd).toBeCloseTo(200);
    expect(comparisonByKey.non_event.n).toBe(1);
  });
});

describe('computeEventAttribution — non-traded / malformed days', () => {
  it('excludes a non-traded (no pnl) day even if its date matches an event', () => {
    const days = [
      { date: '2025-01-29', status: 'skipped', pnl: null },
      tradedDay('2025-02-07', -40),
    ];
    const cal = calendar({
      FOMC: [{ date: '2025-01-29', tentative: false }],
      NFP: [{ date: '2025-02-07', tentative: false }],
    });
    const result = computeEventAttribution(days, cal);
    const byKey = Object.fromEntries(result.typeBuckets.map((b) => [b.key, b]));
    expect(byKey.FOMC.n).toBe(0);
    expect(byKey.NFP.n).toBe(1);
  });

  it('tolerates null/malformed entries mixed into the days array', () => {
    const cal = calendar({ FOMC: [{ date: '2025-01-29', tentative: false }] });
    const days = [null, undefined, {}, tradedDay('2025-01-29', 100)];
    expect(() => computeEventAttribution(days, cal)).not.toThrow();
    const result = computeEventAttribution(days, cal);
    expect(result.available).toBe(true);
    expect(result.comparison.find((b) => b.key === 'event').n).toBe(1);
  });

  it('tolerates a malformed events entry (missing date) in the calendar', () => {
    const cal = calendar({ FOMC: [{ tentative: false }, null, 'not-an-object'] });
    const days = [tradedDay('2025-01-29', 100)];
    expect(() => computeEventAttribution(days, cal)).not.toThrow();
    const result = computeEventAttribution(days, cal);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_overlap');
  });
});

describe('computeEventAttribution — laddered run (drift lock)', () => {
  // A laddered day (F4.1) retains the day-level AGGREGATE's `date`/`pnl` at
  // the top level AND carries an `entries[]` array of per-rung children
  // (each its own `{ entry_ts, status, weighted_pnl_usd, ... }`). This module
  // must attribute off the day-level aggregate `date`/`pnl.total_pnl_usd`
  // only, and must not be confused, double-count, or crash because `entries`
  // is present — mirrors ladderAnalysis.js's consumption of the same
  // `days[]`.
  it('attributes a laddered FOMC day by its day-level aggregate pnl, ignoring entries[]', () => {
    const laddered = {
      ...tradedDay('2025-01-29', 100),
      entries: [
        { entry_ts: '2025-01-29T14:00:00Z', status: 'ok', weighted_pnl_usd: 260 },
        { entry_ts: '2025-01-29T14:30:00Z', status: 'ok', weighted_pnl_usd: -160 },
      ],
    };
    const days = [laddered, tradedDay('2025-02-13', 10)]; // non-event
    const cal = calendar({ FOMC: [{ date: '2025-01-29', tentative: false }] });
    const result = computeEventAttribution(days, cal);
    expect(result.available).toBe(true);
    const byKey = Object.fromEntries(result.typeBuckets.map((b) => [b.key, b]));
    // N=1 per laddered DAY, not per rung — the 2 entries do not inflate N.
    expect(byKey.FOMC.n).toBe(1);
    expect(byKey.FOMC.sumUsd).toBeCloseTo(100); // the day's own aggregate pnl, not 260 + -160.
    const comparisonByKey = Object.fromEntries(result.comparison.map((b) => [b.key, b]));
    expect(comparisonByKey.event.n).toBe(1);
    expect(comparisonByKey.event.sumUsd).toBeCloseTo(100);
    expect(comparisonByKey.non_event.n).toBe(1);
  });
});
