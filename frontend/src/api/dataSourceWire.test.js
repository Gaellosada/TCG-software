// The api/ wrappers must NEVER emit a top-level ``data_source`` field. Under the
// immutable per-instrument model the source rides each leg / input / series ref
// (present only for v2); an all-v1 body is byte-identical to a pre-feature
// payload with no ``data_source`` key anywhere.

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

describe('api wire — no top-level data_source', () => {
  it('computePortfolio never emits a top-level data_source', async () => {
    await computePortfolio({ ...baseArgs });
    expect('data_source' in lastBody()).toBe(false);
  });

  it('computePortfolio carries a v2 leg source on the leg, not the top level', async () => {
    await computePortfolio({
      ...baseArgs,
      legs: { A: { type: 'instrument', collection: 'INDEX', symbol: 'SPX', data_source: 'v2' } },
      weights: { A: 100 },
    });
    const body = lastBody();
    expect('data_source' in body).toBe(false);
    expect(body.legs.A.data_source).toBe('v2');
  });

  it('getPortfolioCachedResult never emits a top-level data_source', async () => {
    await getPortfolioCachedResult({ ...baseArgs });
    expect('data_source' in lastBody()).toBe(false);
  });

  it('computeSignal never emits a top-level data_source', async () => {
    await computeSignal({ id: 's' }, []);
    expect('data_source' in lastBody()).toBe(false);
  });
});

describe('buildComputeRequestBody — per-instrument data_source', () => {
  // PER-INSTRUMENT: there is NO top-level ``data_source`` body field — the
  // source rides each input's instrument ref (see requestBuilder.dataSource.test).
  const specNoInputs = { id: 's1', name: 'S', inputs: [], rules: { entries: [], exits: [] } };
  const specWithV2Input = {
    id: 's1', name: 'S',
    inputs: [{ id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX', data_source: 'v2' } }],
    rules: { entries: [], exits: [] },
  };

  it('never emits a top-level data_source field — byte-identical to pre-feature', () => {
    const noSrc = buildComputeRequestBody(specNoInputs, []).body;
    expect('data_source' in noSrc).toBe(false);
    expect(JSON.stringify(noSrc).includes('data_source')).toBe(false);
  });

  it('carries a v2 input source on the input ref, not the top level', () => {
    const v2 = buildComputeRequestBody(specWithV2Input, []).body;
    expect('data_source' in v2).toBe(false);
    expect(v2.spec.inputs[0].instrument.data_source).toBe('v2');
  });
});
