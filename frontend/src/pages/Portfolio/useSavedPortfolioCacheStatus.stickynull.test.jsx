// @vitest-environment jsdom
//
// Regression for the sticky-null cached-status bug: resolvePortfolioRange never
// throws (a leg range error resolves to {start:null,end:null}), so a TRANSIENT
// dwh flake yields overlapRange=null and the row key stays null. The old code
// memoized {sig, key:null} into keyCacheRef, permanently marking an actually-
// cached portfolio "Not cached" for the whole session — a later cacheVersion
// bump reused the memoized null instead of retrying the resolve.
//
// FIX: only memoize a SUCCESSFULLY-resolved key. A failed/null resolution stays
// unmemoized so the next cacheVersion bump retries it. This test proves that a
// row that flakes first, then resolves + is present in IDB, flips to cached.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import useSavedPortfolioCacheStatus from './useSavedPortfolioCacheStatus';

// ── Mocks (module-level) ──
vi.mock('./resolvePortfolioRange', () => ({
  resolvePortfolioRange: vi.fn(),
}));
vi.mock('./persistedDoc', () => ({
  persistedDocToLegs: vi.fn(() => [{ label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 }]),
}));
vi.mock('./computeBodyBuilder', () => ({
  buildPortfolioComputeBody: vi.fn(() => ({ body: { legs: {}, weights: { SPX: 100 } }, missing: [] })),
}));
vi.mock('../../lib/computeCacheKey', () => ({
  computeCacheKey: vi.fn(async () => 'key-spx'),
}));
vi.mock('../../lib/portfolioCache', () => ({
  hasCached: vi.fn(async () => true), // the row IS present in IDB once its key resolves
}));
vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(async () => []),
}));

import { resolvePortfolioRange } from './resolvePortfolioRange';

// One STABLE client — a fresh client per render would change the queryClient
// context value, re-fire the effect (queryClient is a dep), and loop forever.
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
function wrapper({ children }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

const DOC = { id: 'px', legs: [{ label: 'SPX' }], rebalance: 'none' };
// STABLE array reference — a fresh `[DOC]` per render would change the
// `portfolios` dep and re-fire the effect every render (infinite loop).
const PORTFOLIOS = [DOC];

beforeEach(() => {
  vi.clearAllMocks();
});

describe('useSavedPortfolioCacheStatus — transient dwh flake must not stick a row at not-cached', () => {
  it('flakes first (not-cached), then a cacheVersion bump re-resolves → flips to cached (no sticky null)', async () => {
    // 1st range resolution = transient dwh flake → overlapRange null (never throws).
    // Every subsequent resolution succeeds.
    let calls = 0;
    resolvePortfolioRange.mockImplementation(async () => {
      calls += 1;
      if (calls === 1) return { ranges: {}, overlapRange: null };
      return { ranges: {}, overlapRange: { start: '2020-01-01', end: '2020-12-31' } };
    });

    const { result, rerender } = renderHook(
      ({ cacheVersion }) => useSavedPortfolioCacheStatus({
        portfolios: PORTFOLIOS,
        cacheEnabled: true,
        cacheVersion,
        activeId: null,
      }),
      { initialProps: { cacheVersion: 0 }, wrapper },
    );

    // After the flake, the row resolves to not-cached (no key → not-cached).
    await waitFor(() => expect(result.current.px).toBe('not-cached'));
    expect(calls).toBe(1);

    // Bump cacheVersion — the fix leaves the null key UNMEMOIZED, so the row
    // re-resolves its range (now successful), hashes the key, finds it in IDB,
    // and flips to cached. The buggy code reused the memoized {key:null} and
    // stayed stuck at not-cached forever.
    rerender({ cacheVersion: 1 });
    await waitFor(() => expect(result.current.px).toBe('cached'));
    expect(calls).toBeGreaterThanOrEqual(2); // proves the resolve was RETRIED
  });
});
