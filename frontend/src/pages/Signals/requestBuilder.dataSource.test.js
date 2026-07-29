// Per-instrument data_source tests for the signal compute-request builder.
//
// New model (immutable per-instrument source): a source is chosen ONCE, at add
// time, and rides each INPUT's instrument ref. There is NO page/run default and
// NO fold. Invariants:
//   1. The source rides each INPUT's instrument ref, emitted only for 'v2'.
//   2. There is NO top-level ``data_source`` body field.
//   3. A v1/absent input carries no key → byte-identical to a pre-feature body.

import { describe, it, expect } from 'vitest';
import { buildComputeRequestBody } from './requestBuilder';

function specWith(inputs) {
  return {
    id: 's1',
    name: 'S',
    inputs,
    rules: { entries: [], exits: [] },
    settings: { dont_repeat: false },
  };
}

const spot = (extra = {}) => ({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX', ...extra });

describe('buildComputeRequestBody — per-instrument data_source', () => {
  it('never emits a top-level data_source field', () => {
    const { body } = buildComputeRequestBody(specWith([{ id: 'X', instrument: spot({ data_source: 'v2' }) }]), []);
    expect('data_source' in body).toBe(false);
  });

  it('emits data_source:"v2" on a v2 input and NO key on a v1 input', () => {
    const spec = specWith([
      { id: 'X', instrument: spot({ data_source: 'v1' }) },
      { id: 'Y', instrument: spot({ data_source: 'v2' }) },
    ]);
    const { body } = buildComputeRequestBody(spec, []);
    expect('data_source' in body.spec.inputs[0].instrument).toBe(false);
    expect(body.spec.inputs[1].instrument.data_source).toBe('v2');
  });

  it('an unset input and an explicit v1 input both emit no key', () => {
    const spec = specWith([
      { id: 'X', instrument: spot() },                          // unset → no key
      { id: 'Y', instrument: spot({ data_source: 'v1' }) },     // explicit v1 → omitted
    ]);
    const { body } = buildComputeRequestBody(spec, []);
    expect('data_source' in body.spec.inputs[0].instrument).toBe(false);
    expect('data_source' in body.spec.inputs[1].instrument).toBe(false);
  });

  it('an all-v1 signal body emits ZERO data_source keys (byte-identity)', () => {
    const spec = specWith([
      { id: 'X', instrument: spot() },
      { id: 'Y', instrument: spot({ data_source: 'v1' }) },
    ]);
    const { body } = buildComputeRequestBody(spec, []);
    expect(JSON.stringify(body).includes('data_source')).toBe(false);
  });

  it('preserves the other instrument fields (v2 rides alongside them)', () => {
    const spec = specWith([{ id: 'X', instrument: spot({ data_source: 'v2' }) }]);
    const { body } = buildComputeRequestBody(spec, []);
    const inst = body.spec.inputs[0].instrument;
    expect(inst.type).toBe('spot');
    expect(inst.collection).toBe('INDEX');
    expect(inst.instrument_id).toBe('SPX');
    expect(inst.data_source).toBe('v2');
  });

  it('strips a stray camelCase dataSource key and re-encodes it as data_source', () => {
    const spec = specWith([{ id: 'X', instrument: spot({ dataSource: 'v2' }) }]);
    const { body } = buildComputeRequestBody(spec, []);
    const inst = body.spec.inputs[0].instrument;
    expect('dataSource' in inst).toBe(false);
    expect(inst.data_source).toBe('v2');
  });

  it('emits data_source on an indicator seriesMap ref only for v2', () => {
    const spec = specWith([{ id: 'X', instrument: spot() }]);
    spec.rules.entries = [{
      id: 'b1', name: 'B', enabled: true, input_id: 'X', weight: 100,
      conditions: [{
        op: 'gt',
        lhs: { kind: 'indicator', indicator_id: 'ind-1' },
        rhs: { kind: 'constant', value: 0 },
      }],
    }];
    const indicators = [{
      id: 'ind-1', name: 'I', code: 'def compute(series):\n return series["a"]', params: {},
      seriesMap: {
        a: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX', data_source: 'v2' },
        b: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX', data_source: 'v1' },
      },
    }];
    const { body } = buildComputeRequestBody(spec, indicators);
    const map = body.indicators[0].seriesMap;
    expect(map.a.data_source).toBe('v2');
    expect('data_source' in map.b).toBe(false);
    expect('type' in map.a).toBe(false); // frontend-only key still stripped
  });
});
