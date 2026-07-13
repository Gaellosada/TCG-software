import { fetchApi } from './client';

export async function computePortfolio({
  legs, weights, rebalance, returnType, start, end, useCache = true, signal,
}) {
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
