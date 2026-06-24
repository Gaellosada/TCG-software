/**
 * Persistence list queries + invalidation helpers.
 *
 * Persistence lists (signals / portfolios / indicators / baskets) are
 * user-MUTABLE: creating, updating, locking, or archiving a doc must refresh
 * any list showing it. With TanStack Query the pattern is:
 *   1. read the list via ``useQuery`` (so the list IS a cache entry), and
 *   2. after a mutation, ``queryClient.invalidateQueries`` the matching key —
 *      which marks it stale and refetches it in the background.
 *
 * staleTime is short (10s) for these because they change in response to user
 * actions, not the daily warehouse cadence — we want a re-navigation to a list
 * page to revalidate promptly, while still rendering the cached list instantly.
 *
 * The ``useInvalidatePersistence`` hook returns a small set of resource-scoped
 * invalidators. Call the matching one in a create/update/delete handler (or a
 * ``useMutation`` ``onSuccess``) so exactly the affected list/detail refreshes
 * and nothing else. This is the seam the stateful persistence pages can adopt
 * incrementally without restructuring their editor state machines.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '../queryKeys';
import {
  listSignals,
  listPortfolios,
  listIndicators,
  listBaskets,
} from '../api/persistence';
import { listTickets } from '../api/tickets';

const LIST_STALE_TIME = 10 * 1000; // 10s — user-mutable, revalidate promptly

/** Signals in a category. */
export function useSignalsList(category, options = {}) {
  return useQuery({
    queryKey: queryKeys.persistence.signals.list(category),
    queryFn: () => listSignals(category),
    enabled: !!category && (options.enabled ?? true),
    staleTime: LIST_STALE_TIME,
    ...options,
  });
}

/** Portfolios in a category. */
export function usePortfoliosList(category, options = {}) {
  return useQuery({
    queryKey: queryKeys.persistence.portfolios.list(category),
    queryFn: () => listPortfolios(category),
    enabled: !!category && (options.enabled ?? true),
    staleTime: LIST_STALE_TIME,
    ...options,
  });
}

/** All active indicators (flat list, no category). */
export function useIndicatorsList(options = {}) {
  return useQuery({
    queryKey: queryKeys.persistence.indicators.list(),
    queryFn: () => listIndicators(),
    staleTime: LIST_STALE_TIME,
    ...options,
  });
}

/** Baskets in a category. */
export function useBasketsList(category, options = {}) {
  return useQuery({
    queryKey: queryKeys.persistence.baskets.list(category),
    queryFn: () => listBaskets(category),
    enabled: !!category && (options.enabled ?? true),
    staleTime: LIST_STALE_TIME,
    ...options,
  });
}

/** All tickets (flat list, no category), newest-first. */
export function useTicketsList(options = {}) {
  return useQuery({
    queryKey: queryKeys.persistence.tickets.list(),
    queryFn: () => listTickets(),
    staleTime: LIST_STALE_TIME,
    ...options,
  });
}

/**
 * Resource-scoped invalidators for persistence queries.
 *
 * Each method invalidates the relevant list(s) — and, where an id is given,
 * the matching detail — so a mutation refreshes precisely what changed. List
 * invalidators omit the category argument deliberately: a create/archive may
 * move a doc BETWEEN categories (e.g. archive → ARCHIVE), so every category's
 * list for that resource must be revalidated. TanStack treats a partial key
 * (``['persistence','signals','list']``) as a prefix match over all
 * categories — exactly the behaviour we want.
 *
 * @returns {{
 *   signals:    (id?: string) => Promise<void>,
 *   portfolios: (id?: string) => Promise<void>,
 *   indicators: (id?: string) => Promise<void>,
 *   baskets:    (id?: string) => Promise<void>,
 *   tickets:    () => Promise<void>,
 *   all:        () => Promise<void>,
 * }}
 */
export function useInvalidatePersistence() {
  const queryClient = useQueryClient();

  function invalidateResource(resource, resourceKeys, id) {
    // Prefix match: ['persistence', <resource>, 'list'] hits every category's
    // list (TanStack matches the leading sub-array of each cached key).
    const tasks = [
      queryClient.invalidateQueries({ queryKey: ['persistence', resource, 'list'] }),
    ];
    if (id != null) {
      tasks.push(queryClient.invalidateQueries({ queryKey: resourceKeys.detail(id) }));
    }
    return Promise.all(tasks);
  }

  return {
    signals: (id) => invalidateResource('signals', queryKeys.persistence.signals, id),
    portfolios: (id) => invalidateResource('portfolios', queryKeys.persistence.portfolios, id),
    indicators: (id) => invalidateResource('indicators', queryKeys.persistence.indicators, id),
    baskets: (id) => invalidateResource('baskets', queryKeys.persistence.baskets, id),
    /**
     * Tickets have a flat list and no per-id detail query, so this just
     * revalidates the single tickets list (no id argument). A prefix match on
     * ``['persistence','tickets','list']`` hits it exactly.
     */
    tickets: () => queryClient.invalidateQueries({
      queryKey: queryKeys.persistence.tickets.list(),
    }),
    /** Coarse: invalidate every persistence query. */
    all: () => queryClient.invalidateQueries({ queryKey: queryKeys.persistence.all() }),
  };
}
