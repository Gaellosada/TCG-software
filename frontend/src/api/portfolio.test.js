// Unit tests for the portfolio compute/cache API client.
//   - computePortfolio POSTs use_cache in the request body (true and false).
//   - clearPortfolioCache POSTs to the backend clear endpoint.

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client', () => ({
  fetchApi: vi.fn(() => Promise.resolve({ ok: true })),
}));

import { fetchApi } from './client';
import { computePortfolio, clearPortfolioCache, getPortfolioCacheStatus } from './portfolio';

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

describe('computePortfolio — slippage/fees (bps)', () => {
  const base = {
    legs: {}, weights: {}, rebalance: 'none', returnType: 'normal',
    start: '2020-01-01', end: '2020-12-31',
  };

  it('omits slippage_bps/fees_bps when unset (byte-identical body)', async () => {
    await computePortfolio({ ...base });
    const body = JSON.parse(fetchApi.mock.calls[0][1].body);
    expect('slippage_bps' in body).toBe(false);
    expect('fees_bps' in body).toBe(false);
  });

  it('omits both when explicitly 0', async () => {
    await computePortfolio({ ...base, slippageBps: 0, feesBps: 0 });
    const body = JSON.parse(fetchApi.mock.calls[0][1].body);
    expect('slippage_bps' in body).toBe(false);
    expect('fees_bps' in body).toBe(false);
  });

  it('sends slippage_bps/fees_bps in bps when > 0', async () => {
    await computePortfolio({ ...base, slippageBps: 5, feesBps: 2.5 });
    const body = JSON.parse(fetchApi.mock.calls[0][1].body);
    expect(body.slippage_bps).toBe(5);
    expect(body.fees_bps).toBe(2.5);
  });
});

describe('clearPortfolioCache', () => {
  it('POSTs to /portfolio/cache/clear', async () => {
    await clearPortfolioCache();
    expect(fetchApi).toHaveBeenCalledWith('/portfolio/cache/clear', { method: 'POST' });
  });
});

describe('getPortfolioCacheStatus', () => {
  it('POSTs { queries } to /portfolio/cache/status', async () => {
    const bodies = [{ legs: {}, weights: {} }, { legs: { A: {} }, weights: { A: 1 } }];
    await getPortfolioCacheStatus(bodies);
    const [path, opts] = fetchApi.mock.calls[0];
    expect(path).toBe('/portfolio/cache/status');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ queries: bodies });
  });
});
