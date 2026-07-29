// The CATALOG wrappers (used by the instrument picker) must emit
// ``data_source=v2`` ONLY for v2. A v1 / omitted source keeps the URL
// byte-identical to the pre-feature request, so v1 callers are unaffected while
// v2 fetches only what v2 serves.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { listCollections, listInstruments, getAvailableCycles } from './data';
import { getOptionRoots, getOptionExpirations } from './options';

function okResponse(payload) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
}

function lastUrl() {
  return String(globalThis.fetch.mock.calls.at(-1)[0]);
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => okResponse({ collections: [], items: [], cycles: [], roots: [], expirations: [] })));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('catalog wire — data_source only for v2', () => {
  it('listCollections: v1/omitted emits no data_source, v2 does', async () => {
    await listCollections();
    expect(lastUrl()).toContain('/data/collections');
    expect(lastUrl()).not.toContain('data_source');

    await listCollections(null, { source: 'v1' });
    expect(lastUrl()).not.toContain('data_source');

    await listCollections(null, { source: 'v2' });
    expect(lastUrl()).toContain('data_source=v2');
  });

  it('listCollections: asset_class and data_source coexist for v2', async () => {
    await listCollections('index', { source: 'v2' });
    expect(lastUrl()).toContain('asset_class=index');
    expect(lastUrl()).toContain('data_source=v2');
  });

  it('listInstruments: v1/omitted emits no data_source, v2 does', async () => {
    await listInstruments('INDEX');
    expect(lastUrl()).toContain('/data/INDEX?skip=0&limit=50');
    expect(lastUrl()).not.toContain('data_source');

    await listInstruments('INDEX', { source: 'v2' });
    expect(lastUrl()).toContain('data_source=v2');
  });

  it('getAvailableCycles: v1/omitted emits no data_source, v2 does', async () => {
    await getAvailableCycles('FUT_SP_500');
    expect(lastUrl()).toContain('/data/continuous/FUT_SP_500/cycles');
    expect(lastUrl()).not.toContain('data_source');

    await getAvailableCycles('FUT_SP_500', { source: 'v2' });
    expect(lastUrl()).toContain('data_source=v2');
  });

  it('getOptionRoots: v1/omitted emits no data_source, v2 does', async () => {
    await getOptionRoots();
    expect(lastUrl()).toContain('/options/roots');
    expect(lastUrl()).not.toContain('data_source');

    await getOptionRoots({ source: 'v2' });
    expect(lastUrl()).toContain('data_source=v2');
  });

  it('getOptionExpirations: v1/omitted emits no data_source, v2 does', async () => {
    await getOptionExpirations('OPT_SP_500');
    expect(lastUrl()).not.toContain('data_source');

    await getOptionExpirations('OPT_SP_500', 'v2');
    expect(lastUrl()).toContain('data_source=v2');
  });
});
