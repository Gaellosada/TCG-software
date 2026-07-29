// The api/ wrappers must encode ``data_source`` exactly like the body builders:
// omitted on v1 (byte-identical to a pre-feature payload), present on v2.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { computePortfolio, getPortfolioCachedResult } from './portfolio';
import { computeSignal } from './signals';
import { buildComputeRequestBody } from '../pages/Signals/requestBuilder';

function okResponse() {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
}

function lastBody() {
  return JSON.parse(globalThis.fetch.mock.calls.at(-1)[1].body);
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(okResponse));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const baseArgs = {
  legs: {}, weights: {}, rebalance: 'none', returnType: 'normal',
  start: '2020-01-01', end: '2021-01-01',
};

describe('api wire — data_source', () => {
  it('computePortfolio omits data_source on v1 and when unset', async () => {
    await computePortfolio({ ...baseArgs });
    expect('data_source' in lastBody()).toBe(false);
    await computePortfolio({ ...baseArgs, dataSource: 'v1' });
    expect('data_source' in lastBody()).toBe(false);
  });

  it('computePortfolio emits data_source on v2', async () => {
    await computePortfolio({ ...baseArgs, dataSource: 'v2' });
    expect(lastBody().data_source).toBe('v2');
  });

  it('getPortfolioCachedResult keys on the same field', async () => {
    await getPortfolioCachedResult({ ...baseArgs });
    expect('data_source' in lastBody()).toBe(false);
    await getPortfolioCachedResult({ ...baseArgs, dataSource: 'v2' });
    expect(lastBody().data_source).toBe('v2');
  });

  it('computeSignal omits on v1, emits on v2', async () => {
    await computeSignal({ id: 's' }, []);
    expect('data_source' in lastBody()).toBe(false);
    await computeSignal({ id: 's' }, [], { dataSource: 'v2' });
    expect(lastBody().data_source).toBe('v2');
  });
});

describe('buildComputeRequestBody — per-instrument data_source', () => {
  // PER-INSTRUMENT: there is NO top-level ``data_source`` body field — the
  // source rides each input's instrument ref (see requestBuilder.dataSource.test).
  const specNoInputs = { id: 's1', name: 'S', inputs: [], rules: { entries: [], exits: [] } };
  const specWithInput = {
    id: 's1', name: 'S',
    inputs: [{ id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } }],
    rules: { entries: [], exits: [] },
  };

  it('never emits a top-level data_source field — byte-identical to pre-feature', () => {
    const noSrc = buildComputeRequestBody(specNoInputs, []).body;
    const v1 = buildComputeRequestBody(specNoInputs, [], undefined, 'v1').body;
    const v2 = buildComputeRequestBody(specNoInputs, [], undefined, 'v2').body;
    expect('data_source' in noSrc).toBe(false);
    expect('data_source' in v2).toBe(false);
    expect(JSON.stringify(v1)).toBe(JSON.stringify(noSrc));
  });

  it('folds the v2 default onto an input instrument ref', () => {
    const v2 = buildComputeRequestBody(specWithInput, [], undefined, 'v2').body;
    expect('data_source' in v2).toBe(false);
    expect(v2.spec.inputs[0].instrument.data_source).toBe('v2');
  });
});
