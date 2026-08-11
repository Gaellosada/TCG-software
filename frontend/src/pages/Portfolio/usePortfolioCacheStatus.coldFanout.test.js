// @vitest-environment jsdom
//
// REGRESSION GUARD (Wave 2 fix) — the cache-status label latency bug.
//
// ORIGINAL DEFECT (Wave 1): the cheap `/portfolio/cache/status` probe for the
// saved-portfolio list was GATED behind a cold N×M dwh fan-out. For every
// visible saved row, `usePortfolioCacheStatus` resolved each leg's date range
// by fetching that leg's FULL price series (`getInstrumentPrices`), reading only
// `dates[0]`/`dates[-1]`, and only AFTER every row's range resolved did it issue
// the single status call. On a cold load that is N×M full-history round-trips
// before the label can resolve — the "checking… for minutes" symptom.
//
// THE FIX: instrument-leg range resolution now uses a CHEAP min/max-trade_date
// bounds lookup (`getInstrumentPriceBounds`) that yields a BYTE-IDENTICAL
// `start`/`end` (min date, max date) to the full-series `dates[0]`/`dates[-1]`,
// so the cache key is unchanged and the label stays exactly as accurate — while
// the full-series hydration is deleted from the range path.
//
// This guard leaves `resolvePortfolioRange` REAL (the fan-out runs); it mocks
// only the leaf dwh fetchers and asserts:
//   1. resolving the labels triggers ZERO full-series instrument fetches
//      (`getInstrumentPrices` is NOT used for range) — the debt is gone;
//   2. each instrument leg drives exactly one CHEAP bounds call instead
//      (N×M `getInstrumentPriceBounds`);
//   3. the status label still resolves (one batched status call, N bodies).
//
// It FAILS on origin/main (main resolves the range via `getInstrumentPrices`
// and never calls `getInstrumentPriceBounds`) and PASSES after the fix.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import usePortfolioCacheStatus from './usePortfolioCacheStatus';

// ── Instrumented leaf dwh fetchers ──
// `getInstrumentPrices` (FULL series) must NOT be hit for range resolution.
// `getInstrumentPriceBounds` (cheap min/max trade_date) is the range path now.
// `getContinuousSeries` stays the continuous-leg fallback (endpoints of the
// STITCHED series can differ from raw facts, so it is deliberately unchanged).
let fullSeriesCount = 0;
let boundsCount = 0;

vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(async () => {
    fullSeriesCount += 1;
    await Promise.resolve();
    return {
      dates: [20200101, 20201231],
      open: [1, 2], high: [1, 2], low: [1, 2], close: [1, 2], volume: [0, 0],
    };
  }),
  getInstrumentPriceBounds: vi.fn(async () => {
    boundsCount += 1;
    await Promise.resolve();
    // Same endpoints the full series would expose (min/max trade_date), as the
    // raw YYYYMMDD ints the endpoint returns — the caller applies formatDateInt.
    return { start: 20200101, end: 20201231 };
  }),
  getContinuousSeries: vi.fn(async () => {
    await Promise.resolve();
    return { dates: [20200101, 20201231], close: [1, 2] };
  }),
}));

vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));

vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));

// The CHEAP status endpoint. Capture how many FULL-series fetches had fired at
// the moment it is first invoked — proves the status call is no longer gated
// behind a full-series fan-out.
let fullSeriesWhenStatusCalled = null;

vi.mock('../../api/portfolio', () => ({
  getPortfolioCacheStatus: vi.fn((bodies) => {
    if (fullSeriesWhenStatusCalled === null) fullSeriesWhenStatusCalled = fullSeriesCount;
    return Promise.resolve({ results: bodies.map(() => ({ cached: false })) });
  }),
}));

import { getInstrumentPrices, getInstrumentPriceBounds } from '../../api/data';
import { getPortfolioCacheStatus } from '../../api/portfolio';

// Build N saved rows × M distinct instrument legs each.
function makeRows(nRows, mLegs) {
  const rows = [];
  for (let r = 0; r < nRows; r += 1) {
    const legs = [];
    for (let l = 0; l < mLegs; l += 1) {
      legs.push({
        label: `L${r}_${l}`,
        type: 'instrument',
        collection: 'INDEX',
        symbol: `SYM_${r}_${l}`, // distinct → distinct query key → distinct cold fetch
        weight: 100 / mLegs,
      });
    }
    rows.push({ id: `row-${r}`, rebalance: 'none', legs });
  }
  return rows;
}

function wrapper({ children }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

function baseProps(overrides = {}) {
  return {
    cacheEnabled: true,
    legs: [],                 // no active config → isolate the saved-row fan-out
    rebalance: 'none',
    startDate: '',
    endDate: '',
    overlapRange: null,
    resolvePortfolio: () => null,
    portfolios: [],
    activeId: null,
    refreshKey: 0,
    ...overrides,
  };
}

describe('usePortfolioCacheStatus — status label is no longer gated behind a cold full-series fan-out', () => {
  beforeEach(() => {
    fullSeriesCount = 0;
    boundsCount = 0;
    fullSeriesWhenStatusCalled = null;
    getInstrumentPrices.mockClear();
    getInstrumentPriceBounds.mockClear();
    getPortfolioCacheStatus.mockClear();
  });

  it('resolves labels via cheap bounds only — ZERO full-series fetches (5 rows × 3 legs)', async () => {
    const N = 5;
    const M = 3;
    const rows = makeRows(N, M);

    renderHook((p) => usePortfolioCacheStatus(p), {
      wrapper,
      initialProps: baseProps({ portfolios: rows }),
    });

    // The cheap status call is what ultimately resolves the label.
    await waitFor(() => expect(getPortfolioCacheStatus).toHaveBeenCalledTimes(1), { timeout: 3000 });

    // 1) THE FIX: no full-series instrument hydration on the range path at all.
    expect(getInstrumentPrices).not.toHaveBeenCalled();
    expect(fullSeriesCount).toBe(0);

    // 2) Each instrument leg drove exactly one CHEAP bounds lookup instead.
    expect(getInstrumentPriceBounds).toHaveBeenCalledTimes(N * M); // 15

    // 3) The status call was NOT gated behind any full-series fan-out.
    expect(fullSeriesWhenStatusCalled).toBe(0);

    // The status body count matches the rows (parity check — not the defect).
    expect(getPortfolioCacheStatus.mock.calls[0][0]).toHaveLength(N);
  });
});
