// Unit tests for the portfolio compute/cache API client.
//   - computePortfolio POSTs use_cache in the request body (true and false).
//   - clearPortfolioCache POSTs to the backend clear endpoint.

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client', () => ({
  fetchApi: vi.fn(() => Promise.resolve({ ok: true })),
}));

import { fetchApi } from './client';
import { computePortfolio, clearPortfolioCache } from './portfolio';

beforeEach(() => {
  fetchApi.mockClear();
});

describe('computePortfolio — use_cache flag', () => {
  const base = {
    legs: {}, weights: {}, rebalance: 'none', returnType: 'normal',
    start: '2020-01-01', end: '2020-12-31',
  };

  it('defaults use_cache to true when not specified', async () => {
    await computePortfolio({ ...base });
    const [path, opts] = fetchApi.mock.calls[0];
    expect(path).toBe('/portfolio/compute');
    expect(JSON.parse(opts.body).use_cache).toBe(true);
  });

  it('sends use_cache:false when the toggle is off', async () => {
    await computePortfolio({ ...base, useCache: false });
    expect(JSON.parse(fetchApi.mock.calls[0][1].body).use_cache).toBe(false);
  });

  it('sends use_cache:true when the toggle is on', async () => {
    await computePortfolio({ ...base, useCache: true });
    expect(JSON.parse(fetchApi.mock.calls[0][1].body).use_cache).toBe(true);
  });
});

describe('clearPortfolioCache', () => {
  it('POSTs to /portfolio/cache/clear', async () => {
    await clearPortfolioCache();
    expect(fetchApi).toHaveBeenCalledWith('/portfolio/cache/clear', { method: 'POST' });
  });
});
