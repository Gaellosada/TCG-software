// Unit tests for the intraday-backtest cache API client functions:
//   - getIntradayBacktestCachedResult POSTs to /intraday-backtest/cache/get.
//   - getIntradayBacktestCacheStatus POSTs to /intraday-backtest/cache/status.
// Mirrors the mocking style of portfolio.test.js — ``fetchApi`` (from
// ``./client``) is mocked directly rather than the global ``fetch``.

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client', () => ({
  fetchApi: vi.fn(() => Promise.resolve({ ok: true })),
}));

import { fetchApi } from './client';
import {
  getIntradayBacktestCachedResult,
  getIntradayBacktestCacheStatus,
} from './intradayBacktest';

beforeEach(() => {
  fetchApi.mockClear();
});

const _body = (over = {}) => ({
  start_date: '2025-02-03',
  end_date: '2025-02-14',
  ...over,
});

describe('getIntradayBacktestCachedResult', () => {
  it('POSTs the given payload as JSON body to /intraday-backtest/cache/get', async () => {
    const params = _body();
    await getIntradayBacktestCachedResult(params);
    const [path, opts] = fetchApi.mock.calls[0];
    expect(path).toBe('/intraday-backtest/cache/get');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual(params);
  });

  it('returns the parsed result on a HIT (from_cache:true)', async () => {
    const hit = {
      params_echo: { start_date: '2025-02-03', end_date: '2025-02-14' },
      window: { min_date: '2025-01-01', max_date: '2026-07-31' },
      days: [{ date: '2025-02-03', status: 'ok', strike: 5850.0 }],
      aggregate: { n_days: 10, n_traded: 1, total_pnl_usd: 123.5 },
      warnings: [],
      from_cache: true,
    };
    fetchApi.mockResolvedValueOnce(hit);
    const result = await getIntradayBacktestCachedResult(_body());
    expect(result).toEqual(hit);
  });

  it('passes through a MISS response shape ({cached:false}) unchanged', async () => {
    fetchApi.mockResolvedValueOnce({ cached: false });
    const result = await getIntradayBacktestCachedResult(_body());
    expect(result).toEqual({ cached: false });
  });
});

describe('getIntradayBacktestCacheStatus', () => {
  it('POSTs the given payload as JSON body to /intraday-backtest/cache/status', async () => {
    const params = _body();
    await getIntradayBacktestCacheStatus(params);
    const [path, opts] = fetchApi.mock.calls[0];
    expect(path).toBe('/intraday-backtest/cache/status');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual(params);
  });

  it('returns {cached:true} for a HIT', async () => {
    fetchApi.mockResolvedValueOnce({ cached: true });
    const result = await getIntradayBacktestCacheStatus(_body());
    expect(result).toEqual({ cached: true });
  });

  it('returns {cached:false} for a MISS', async () => {
    fetchApi.mockResolvedValueOnce({ cached: false });
    const result = await getIntradayBacktestCacheStatus(_body());
    expect(result).toEqual({ cached: false });
  });
});
