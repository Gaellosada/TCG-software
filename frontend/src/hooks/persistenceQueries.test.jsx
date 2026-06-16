// @vitest-environment jsdom
//
// Proof of property (d): a persistence mutation invalidates and refetches the
// matching list query — and ONLY that query. Uses the real list + invalidation
// hooks with a mocked persistence api and a shared QueryClient.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { waitFor, act, renderHook } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { makeTestClient } from '../test/queryWrapper';

vi.mock('../api/persistence', () => ({
  listSignals: vi.fn(),
  listPortfolios: vi.fn(),
  listIndicators: vi.fn(),
  listBaskets: vi.fn(),
}));

import { listIndicators, listPortfolios } from '../api/persistence';
import {
  useIndicatorsList,
  usePortfoliosList,
  useInvalidatePersistence,
} from './persistenceQueries';

beforeEach(() => {
  vi.clearAllMocks();
});

function wrapperFor(client) {
  return ({ children }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe('persistence invalidation (property d)', () => {
  it('invalidating indicators refetches the indicators list', async () => {
    listIndicators
      .mockResolvedValueOnce([{ id: 'rsi', name: 'RSI' }]) // initial
      .mockResolvedValueOnce([
        { id: 'rsi', name: 'RSI' },
        { id: 'macd', name: 'MACD' }, // created by the "mutation"
      ]);

    const client = makeTestClient();
    const wrapper = wrapperFor(client);

    const list = renderHook(() => useIndicatorsList(), { wrapper });
    const invalidate = renderHook(() => useInvalidatePersistence(), { wrapper });

    await waitFor(() => expect(list.result.current.data).toHaveLength(1));
    expect(listIndicators).toHaveBeenCalledTimes(1);

    // Simulate a create handler calling the resource invalidator.
    await act(async () => {
      await invalidate.result.current.indicators();
    });

    // The list refetched and now reflects the new doc.
    await waitFor(() => expect(list.result.current.data).toHaveLength(2));
    expect(listIndicators).toHaveBeenCalledTimes(2);
    expect(list.result.current.data.map((d) => d.id)).toEqual(['rsi', 'macd']);
  });

  it('invalidating one resource does NOT refetch an unrelated resource', async () => {
    listIndicators.mockResolvedValue([{ id: 'rsi', name: 'RSI' }]);
    listPortfolios.mockResolvedValue([{ id: 'p1', name: 'Port 1' }]);

    const client = makeTestClient();
    const wrapper = wrapperFor(client);

    const indicators = renderHook(() => useIndicatorsList(), { wrapper });
    const portfolios = renderHook(() => usePortfoliosList('RESEARCH'), { wrapper });
    const invalidate = renderHook(() => useInvalidatePersistence(), { wrapper });

    await waitFor(() => expect(indicators.result.current.data).toBeTruthy());
    await waitFor(() => expect(portfolios.result.current.data).toBeTruthy());
    expect(listIndicators).toHaveBeenCalledTimes(1);
    expect(listPortfolios).toHaveBeenCalledTimes(1);

    // Invalidate ONLY portfolios.
    await act(async () => {
      await invalidate.result.current.portfolios('p1');
    });

    await waitFor(() => expect(listPortfolios).toHaveBeenCalledTimes(2));
    // Indicators list must NOT have refetched.
    expect(listIndicators).toHaveBeenCalledTimes(1);
  });

  it('invalidating portfolios refreshes EVERY category list (cross-category move)', async () => {
    // A doc archived from RESEARCH → ARCHIVE must refresh both category lists.
    listPortfolios.mockImplementation((category) =>
      Promise.resolve([{ id: 'p1', name: 'Port 1', category }]),
    );

    const client = makeTestClient();
    const wrapper = wrapperFor(client);

    const research = renderHook(() => usePortfoliosList('RESEARCH'), { wrapper });
    const archive = renderHook(() => usePortfoliosList('ARCHIVE'), { wrapper });
    const invalidate = renderHook(() => useInvalidatePersistence(), { wrapper });

    await waitFor(() => expect(research.result.current.data).toBeTruthy());
    await waitFor(() => expect(archive.result.current.data).toBeTruthy());
    const callsBefore = listPortfolios.mock.calls.length; // 2 (one per category)

    await act(async () => {
      await invalidate.result.current.portfolios();
    });

    // Both category lists refetched → +2 calls (prefix match over all categories).
    await waitFor(() => expect(listPortfolios.mock.calls.length).toBe(callsBefore + 2));
  });
});
