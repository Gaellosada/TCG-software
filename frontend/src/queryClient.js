import { QueryClient } from '@tanstack/react-query';

/**
 * Shared TanStack Query defaults for the TCG app.
 *
 * The whole point of this layer is the stale-while-revalidate UX: when a page
 * remounts on navigation, the cached data renders INSTANTLY and a background
 * refetch silently patches only what changed. The defaults below encode that:
 *
 *   - staleTime (5 min): market data (collections / instruments / option
 *     roots / price series) changes at most once a day (EOD warehouse). For
 *     5 minutes a re-navigation is served purely from cache with NO refetch
 *     at all — the fastest possible path. After that window, the cached data
 *     still renders immediately and a background refetch runs.
 *
 *   - gcTime (30 min): keep unmounted query data in the cache long enough that
 *     navigating away and back within a normal working session always hits a
 *     warm cache (this is what kills the spinner). Default is 5 min, which can
 *     evict between navigations; 30 min is comfortably longer than a tab-switch.
 *
 *   - refetchOnWindowFocus false: market data is daily-grained — refetching
 *     every time the user alt-tabs back is wasted work and can cause a visible
 *     flash on charts. Background revalidation on mount (staleness-driven) is
 *     enough.
 *
 *   - retry 1: the api client already classifies/raises a clear FetchError;
 *     one retry smooths a transient blip without making a truly-down backend
 *     feel sluggish.
 *
 * Per-query overrides (e.g. a shorter staleTime for user-mutable persistence
 * lists) are set at the useQuery call site; these are only the defaults.
 *
 * Exposed as a factory so tests can spin up an isolated client (with retry
 * off) without sharing cache across test cases.
 */
export function createQueryClient(overrides = {}) {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5 * 60 * 1000, // 5 minutes
        gcTime: 30 * 60 * 1000, // 30 minutes
        refetchOnWindowFocus: false,
        retry: 1,
        ...overrides.queries,
      },
      mutations: {
        ...overrides.mutations,
      },
    },
  });
}

export default createQueryClient;
