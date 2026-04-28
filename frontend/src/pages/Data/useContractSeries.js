import useAsync from '../../hooks/useAsync';
import { getOptionContract } from '../../api/options';

/**
 * Hook for fetching a single option contract's full time-series.
 *
 * Uses ``useAsync`` for simple fire-and-forget fetching; re-fetches
 * whenever any of the dependency arguments change.
 *
 * Decision C: ``computeMissing`` is transient — not persisted to localStorage.
 *
 * @param {string|null} collection - MongoDB collection name (e.g. 'OPT_SP_500').
 * @param {string|null} contractId - Contract identifier.
 * @param {object} [opts]
 * @param {boolean} [opts.computeMissing=false] - Opt-in to compute missing Greeks (Decision C).
 * @param {string|null} [opts.dateFrom] - ISO date lower bound for the series (inclusive).
 * @param {string|null} [opts.dateTo] - ISO date upper bound for the series (inclusive).
 * @returns {{ data: object|null, loading: boolean, error: Error|null }}
 */
export function useContractSeries(
  collection,
  contractId,
  { computeMissing = false, dateFrom = null, dateTo = null } = {},
) {
  return useAsync(
    () =>
      collection && contractId
        ? getOptionContract(collection, contractId, { computeMissing, dateFrom, dateTo })
        : Promise.resolve(null),
    [collection, contractId, computeMissing, dateFrom, dateTo],
  );
}
