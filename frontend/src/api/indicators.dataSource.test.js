// Per-instrument data_source tests for computeIndicator's series refs.
//
// Invariant: each series ref carries 'v2' on the wire only when its own source
// is v2; v1/absent emits no key (byte-identical to a pre-feature request).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { computeIndicator } from './indicators';

let lastBody;

beforeEach(() => {
  lastBody = null;
  global.fetch = vi.fn(async (_url, opts) => {
    lastBody = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ series: [] }) };
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

const base = {
  code: 'def compute(series):\n return series["price"]',
  params: {},
};

describe('computeIndicator — per-instrument data_source on series refs', () => {
  it('emits data_source:"v2" on a v2 series ref and omits it for v1/absent', async () => {
    await computeIndicator({
      ...base,
      series: {
        price: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX', data_source: 'v2' },
        other: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX', data_source: 'v1' },
        bare: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
      },
    });
    expect(lastBody.series.price.data_source).toBe('v2');
    expect('data_source' in lastBody.series.other).toBe(false);
    expect('data_source' in lastBody.series.bare).toBe(false);
  });

  it('preserves the other ref fields and keeps a v1 request byte-identical', async () => {
    await computeIndicator({
      ...base,
      series: { price: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
    });
    expect(lastBody.series.price).toEqual({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX' });
  });

  it('strips a stray camelCase dataSource key and re-encodes it', async () => {
    await computeIndicator({
      ...base,
      series: { price: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX', dataSource: 'v2' } },
    });
    expect('dataSource' in lastBody.series.price).toBe(false);
    expect(lastBody.series.price.data_source).toBe('v2');
  });
});
