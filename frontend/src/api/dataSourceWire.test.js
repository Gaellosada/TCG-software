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

describe('buildComputeRequestBody — data_source', () => {
  const spec = { id: 's1', name: 'S', inputs: [], rules: { entries: [], exits: [] } };

  it('omits the key on v1 / unset — byte-identical to a pre-feature body', () => {
    const noSrc = buildComputeRequestBody(spec, []).body;
    const v1 = buildComputeRequestBody(spec, [], undefined, 'v1').body;
    expect('data_source' in noSrc).toBe(false);
    expect(JSON.stringify(v1)).toBe(JSON.stringify(noSrc));
  });

  it('emits data_source:"v2" on v2', () => {
    const v2 = buildComputeRequestBody(spec, [], undefined, 'v2').body;
    expect(v2.data_source).toBe('v2');
  });
});
