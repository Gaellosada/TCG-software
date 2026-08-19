import { describe, it, expect } from 'vitest';
import { computeLadderAnalysis } from './ladderAnalysis';

// Three laddered days, each with 3 rungs (10:00Z / 10:30Z / 11:00Z), plus one
// day with a max_concurrent-capped 3rd rung and one non-laddered (single
// entry, no `entries` key) day mixed in — the exact shape run_backtest emits.
const MULTI_DAY_LADDERED = [
  {
    date: '2025-02-03',
    status: 'ok',
    pnl: { total_pnl_usd: 300 },
    entries: [
      { date: '2025-02-03', status: 'ok', entry_ts: '2025-02-03T10:00:00Z', contracts: 1, pnl: { total_pnl_usd: 100 }, weighted_pnl_usd: 100 },
      { date: '2025-02-03', status: 'ok', entry_ts: '2025-02-03T10:30:00Z', contracts: 1, pnl: { total_pnl_usd: 150 }, weighted_pnl_usd: 150 },
      { date: '2025-02-03', status: 'ok', entry_ts: '2025-02-03T11:00:00Z', contracts: 1, pnl: { total_pnl_usd: 50 }, weighted_pnl_usd: 50 },
    ],
  },
  {
    date: '2025-02-04',
    status: 'ok',
    pnl: { total_pnl_usd: -60 },
    entries: [
      { date: '2025-02-04', status: 'ok', entry_ts: '2025-02-04T10:00:00Z', contracts: 1, pnl: { total_pnl_usd: -50 }, weighted_pnl_usd: -50 },
      { date: '2025-02-04', status: 'ok', entry_ts: '2025-02-04T10:30:00Z', contracts: 1, pnl: { total_pnl_usd: -10 }, weighted_pnl_usd: -10 },
      // 3rd rung capped by max_concurrent — skipped, no pnl.
      { date: '2025-02-04', status: 'skipped', skip_reason: 'max_concurrent', entry_ts: '2025-02-04T11:00:00Z', contracts: 1, pnl: null, weighted_pnl_usd: 0 },
    ],
  },
  {
    date: '2025-02-05',
    status: 'ok',
    pnl: { total_pnl_usd: 220 },
    entries: [
      { date: '2025-02-05', status: 'ok', entry_ts: '2025-02-05T10:00:00Z', contracts: 1, pnl: { total_pnl_usd: 120 }, weighted_pnl_usd: 120 },
      { date: '2025-02-05', status: 'ok', entry_ts: '2025-02-05T10:30:00Z', contracts: 1, pnl: { total_pnl_usd: 100 }, weighted_pnl_usd: 100 },
    ],
  },
  // Non-laddered day mixed into the same run (e.g. an excluded/allowlisted
  // day resolved to a single entry) — must not contribute to any bucket.
  { date: '2025-02-06', status: 'ok', pnl: { total_pnl_usd: 5 } },
];

describe('computeLadderAnalysis', () => {
  it('flags an absent-entries run as not laddered', () => {
    const result = computeLadderAnalysis([
      { date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 5 } },
      { date: '2025-02-04', status: 'ok', pnl: { total_pnl_usd: -5 } },
    ]);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('no_entries');
    expect(result.buckets).toEqual([]);
    expect(result.overall).toBeNull();
  });

  it('flags empty/undefined days as not laddered', () => {
    expect(computeLadderAnalysis([]).available).toBe(false);
    expect(computeLadderAnalysis(undefined).available).toBe(false);
  });

  it('flags a single-rung-per-day run as not laddered (nothing to compare)', () => {
    const result = computeLadderAnalysis([
      {
        date: '2025-02-03',
        status: 'ok',
        pnl: { total_pnl_usd: 100 },
        entries: [{ date: '2025-02-03', status: 'ok', entry_ts: '2025-02-03T10:00:00Z', contracts: 1, pnl: { total_pnl_usd: 100 }, weighted_pnl_usd: 100 }],
      },
      {
        date: '2025-02-04',
        status: 'ok',
        pnl: { total_pnl_usd: -20 },
        entries: [{ date: '2025-02-04', status: 'ok', entry_ts: '2025-02-04T10:00:00Z', contracts: 1, pnl: { total_pnl_usd: -20 }, weighted_pnl_usd: -20 }],
      },
    ]);
    expect(result.available).toBe(false);
    expect(result.reason).toBe('single_rung');
  });

  it('buckets by rung index across days, correct count/sum/mean/win-rate, only traded rows counted', () => {
    const result = computeLadderAnalysis(MULTI_DAY_LADDERED);
    expect(result.available).toBe(true);
    expect(result.buckets).toHaveLength(3);

    const [rung0, rung1, rung2] = result.buckets;

    // Rung 0 (10:00Z): 100, -50, 120 -> n=3, sum=170, mean=170/3, 2 wins.
    expect(rung0.rung).toBe(0);
    expect(rung0.label).toBe('10:00Z');
    expect(rung0.timeVaries).toBe(false);
    expect(rung0.n).toBe(3);
    expect(rung0.nSkipped).toBe(0);
    expect(rung0.sumUsd).toBeCloseTo(170, 6);
    expect(rung0.meanUsd).toBeCloseTo(170 / 3, 6);
    expect(rung0.winRate).toBeCloseTo(2 / 3, 6);

    // Rung 1 (10:30Z): 150, -10, 100 -> n=3, sum=240, 2 wins.
    expect(rung1.label).toBe('10:30Z');
    expect(rung1.n).toBe(3);
    expect(rung1.sumUsd).toBeCloseTo(240, 6);
    expect(rung1.winRate).toBeCloseTo(2 / 3, 6);

    // Rung 2 (11:00Z): 50 (traded) + 1 skipped (max_concurrent, not present
    // on day 3 at all -> only 2 rows total, 1 traded, 1 skipped).
    expect(rung2.n).toBe(1);
    expect(rung2.nSkipped).toBe(1);
    expect(rung2.sumUsd).toBeCloseTo(50, 6);
    expect(rung2.meanUsd).toBeCloseTo(50, 6);
    expect(rung2.winRate).toBe(1);

    // Overall: 3+3+1 = 7 traded rungs across 3 laddered days; the 4th
    // (non-laddered) day contributes nothing.
    expect(result.overall.nDays).toBe(3);
    expect(result.overall.nEntries).toBe(8); // 3 + 3 + 2 raw rows (incl. the skip)
    expect(result.overall.nTraded).toBe(7);
    expect(result.overall.sumUsd).toBeCloseTo(170 + 240 + 50, 6);
  });

  it('does not misplace a rung across a DST boundary (buckets by index, flags timeVaries)', () => {
    // Same logical "first rung" (10:00 ET) resolved to different UTC offsets
    // either side of a DST change: 15:00Z (EST, UTC-5) vs 14:00Z (EDT, UTC-4).
    const result = computeLadderAnalysis([
      {
        date: '2025-03-06', status: 'ok', pnl: { total_pnl_usd: 40 },
        entries: [
          { date: '2025-03-06', status: 'ok', entry_ts: '2025-03-06T15:00:00Z', contracts: 1, pnl: { total_pnl_usd: 10 }, weighted_pnl_usd: 10 },
          { date: '2025-03-06', status: 'ok', entry_ts: '2025-03-06T15:30:00Z', contracts: 1, pnl: { total_pnl_usd: 30 }, weighted_pnl_usd: 30 },
        ],
      },
      {
        date: '2025-03-10', status: 'ok', pnl: { total_pnl_usd: 40 },
        entries: [
          { date: '2025-03-10', status: 'ok', entry_ts: '2025-03-10T14:00:00Z', contracts: 1, pnl: { total_pnl_usd: 15 }, weighted_pnl_usd: 15 },
          { date: '2025-03-10', status: 'ok', entry_ts: '2025-03-10T14:30:00Z', contracts: 1, pnl: { total_pnl_usd: 25 }, weighted_pnl_usd: 25 },
        ],
      },
    ]);
    expect(result.available).toBe(true);
    // Both days' first entries land in rung 0 despite the 1-hour UTC shift.
    expect(result.buckets[0].n).toBe(2);
    expect(result.buckets[0].sumUsd).toBeCloseTo(25, 6);
    expect(result.buckets[0].timeVaries).toBe(true);
    expect(['15:00Z', '14:00Z']).toContain(result.buckets[0].label);
  });

  it('silently drops malformed entries without crashing', () => {
    const result = computeLadderAnalysis([
      {
        date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 10 },
        entries: [
          null,
          'not-an-object',
          { status: 'ok', weighted_pnl_usd: 10 }, // missing entry_ts
          { entry_ts: 'not-a-date', status: 'ok', weighted_pnl_usd: 10 },
          { entry_ts: '2025-02-03T10:00:00Z', status: 'ok', weighted_pnl_usd: 10 },
          { entry_ts: '2025-02-03T10:30:00Z', status: 'ok', weighted_pnl_usd: 20 },
        ],
      },
      {
        date: '2025-02-04', status: 'ok', pnl: { total_pnl_usd: -5 },
        entries: [
          { entry_ts: '2025-02-04T10:00:00Z', status: 'ok', weighted_pnl_usd: -5 },
          { entry_ts: '2025-02-04T10:30:00Z', status: 'ok', weighted_pnl_usd: -15 },
        ],
      },
    ]);
    expect(result.available).toBe(true);
    expect(result.buckets).toHaveLength(2);
    expect(result.buckets[0].n).toBe(2);
    expect(result.overall.nEntries).toBe(4);
  });
});
