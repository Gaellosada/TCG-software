import { fetchApi } from './client';

export async function computePortfolio({ legs, weights, rebalance, returnType, start, end, signal }) {
  const res = await fetchApi('/portfolio/compute', {
    method: 'POST',
    body: JSON.stringify({
      legs,
      weights,
      rebalance,
      return_type: returnType,
      start: start || undefined,
      end: end || undefined,
    }),
    signal,
  });
  return res;
}