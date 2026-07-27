import { fetchApi } from './client';
import { dataSourceFieldsForRequest } from '../lib/dataSource';

export async function computePortfolio({
  legs, weights, rebalance, returnType, start, end, useCache = true,
  slippageBps, feesBps, dataSource, signal,
}) {
  // Global execution costs (basis points) ride the request body when > 0;
  // omitted otherwise so a default request stays byte-identical to a
  // pre-feature payload (must match buildPortfolioComputeBody so the backend
  // cache key is consistent between the compute call and the status probe).
  const costFields = {};
  if (typeof slippageBps === 'number' && Number.isFinite(slippageBps) && slippageBps > 0) {
    costFields.slippage_bps = slippageBps;
  }
  if (typeof feesBps === 'number' && Number.isFinite(feesBps) && feesBps > 0) {
    costFields.fees_bps = feesBps;
  }
  const res = await fetchApi('/portfolio/compute', {
    method: 'POST',
    body: JSON.stringify({
      legs,
      weights,
      rebalance,
      return_type: returnType,
      start: start || undefined,
      end: end || undefined,
      // Ask the backend to serve/store from its on-disk result cache. When the
      // Settings toggle is OFF this is false → the backend recomputes fresh.
      use_cache: useCache,
      ...costFields,
      // Market data source — present only for v2, so a v1 compute POST stays
      // byte-identical to a pre-feature payload (backend defaults absent → v1).
      ...dataSourceFieldsForRequest(dataSource),
    }),
    signal,
  });
  return res;
}

/**
 * Clear the backend's on-disk portfolio result cache.
 * POST /api/portfolio/cache/clear — resolves to the endpoint's JSON (or null).
 */
export async function clearPortfolioCache() {
  return fetchApi('/portfolio/cache/clear', { method: 'POST' });
}

/**
 * Batched cache-status probe — a PURE key lookup (no compute) for many compute
 * bodies at once. POST /api/portfolio/cache/status with
 * ``{ queries: [<compute_body>, ...] }`` → ``{ results: [{cached: bool}, ...] }``
 * parallel to ``queries``. The bodies MUST be built by the SAME
 * ``buildPortfolioComputeBody`` the compute path uses, so a match here means an
 * identical Compute would be served from cache.
 *
 * @param {Array<object>} queries  compute-request bodies
 * @param {{ signal?: AbortSignal }} [options]
 * @returns {Promise<{ results: Array<{ cached: boolean }> }>}
 */
export async function getPortfolioCacheStatus(queries, { signal } = {}) {
  return fetchApi('/portfolio/cache/status', {
    method: 'POST',
    body: JSON.stringify({ queries }),
    ...(signal ? { signal } : {}),
  });
}

/**
 * Read-only cache fetch — returns a cached compute result WITHOUT ever computing.
 * POST /api/portfolio/cache/get with a compute-request body (the SAME shape/body
 * ``computePortfolio`` sends, so the backend key matches). Backs the auto-display
 * UX: a HIT returns the cached blob (``from_cache: true``); a MISS returns
 * ``{ result: null, from_cache: false }`` and NEVER triggers a compute.
 *
 * @param {object} p  compute-request body { legs, weights, rebalance, returnType, start, end, slippageBps, feesBps }
 * @param {{ signal?: AbortSignal }} [options]
 * @returns {Promise<{ result: object|null, from_cache: boolean }>}
 */
export async function getPortfolioCachedResult({
  legs, weights, rebalance, returnType, start, end, slippageBps, feesBps, dataSource,
}, { signal } = {}) {
  // Global execution costs ride the key body identically to computePortfolio (>0
  // only, else omitted) so this read-only cache-get keys to the SAME entry a
  // costed Compute stored — otherwise a costed result would falsely read as a MISS.
  const costFields = {};
  if (typeof slippageBps === 'number' && Number.isFinite(slippageBps) && slippageBps > 0) {
    costFields.slippage_bps = slippageBps;
  }
  if (typeof feesBps === 'number' && Number.isFinite(feesBps) && feesBps > 0) {
    costFields.fees_bps = feesBps;
  }
  return fetchApi('/portfolio/cache/get', {
    method: 'POST',
    body: JSON.stringify({
      legs,
      weights,
      rebalance,
      return_type: returnType,
      start: start || undefined,
      end: end || undefined,
      ...costFields,
      // Same source field the Compute POST carries — otherwise a v2 result
      // would falsely read as a MISS (and a v1 probe could serve a v2 entry).
      ...dataSourceFieldsForRequest(dataSource),
    }),
    ...(signal ? { signal } : {}),
  });
}
