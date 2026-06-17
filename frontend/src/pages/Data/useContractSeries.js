import { useOptionContract } from '../../hooks/marketQueries';

/**
 * Hook for fetching a single option contract's full time-series.
 *
 * Backed by TanStack Query (stale-while-revalidate): the series for a given
 * (collection, contractId, computeMissing, dateFrom, dateTo) tuple is cached,
 * so re-selecting a contract renders instantly and revalidates in the
 * background. Resolves to ``data: null`` when collection/contractId are absent
 * (the query stays disabled), matching the previous fire-and-forget guard.
 *
 * Decision C: ``computeMissing`` is transient — not persisted to localStorage.
 *
 * @param {string|null} collection - MongoDB collection name (e.g. 'OPT_SP_500').
 * @param {string|null} contractId - Contract identifier.
 * @param {object} [opts]
 * @param {boolean} [opts.computeMissing=true] - Compute missing Greeks via Black-76. Default flipped to true in Phase 2 so VIX (no stored greeks at CBOE) renders greeks without an opt-in click. Stored-greek collections short-circuit per row.
 * @param {string|null} [opts.dateFrom] - ISO date lower bound for the series (inclusive).
 * @param {string|null} [opts.dateTo] - ISO date upper bound for the series (inclusive).
 * @returns {{ data: object|null, loading: boolean, error: Error|null }}
 */
export function useContractSeries(
  collection,
  contractId,
  { computeMissing = true, dateFrom = null, dateTo = null } = {},
) {
  return useOptionContract(collection, contractId, { computeMissing, dateFrom, dateTo });
}
