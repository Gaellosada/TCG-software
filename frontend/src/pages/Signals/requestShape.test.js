import { describe, it, expect } from 'vitest';
import { buildComputeRequestBody, normaliseSpecForRequest } from './requestBuilder';
import { collectIndicatorIds } from '../../api/signals';

// Request body shape pinned by PLAN.md § v3 contract. Guards against drift
// between frontend producer and backend consumer.

const V3_INPUTS = [
  { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
  { id: 'Y', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'NDX' } },
];

describe('computeSignal request body shape (v3)', () => {
  it('top level has exactly {spec, indicators}', () => {
    const signal = {
      id: 's1',
      name: 'S1',
      inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X',
          weight: 0.5,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'sma-20', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const indicators = [
      { id: 'sma-20', name: '20-day SMA',
        code: 'def compute(series, window=20): return series["price"]',
        params: { window: 20 },
        seriesMap: { price: { collection: 'INDEX', instrument_id: 'SPX' } } },
    ];
    const { body, missing } = buildComputeRequestBody(signal, indicators);
    expect(missing).toEqual([]);
    expect(Object.keys(body).sort()).toEqual(['indicators', 'spec']);
    expect(body.spec.id).toBe('s1');
    expect(body.spec.name).toBe('S1');
    expect(Array.isArray(body.indicators)).toBe(true);
    // v3: inputs are part of the spec.
    expect(Array.isArray(body.spec.inputs)).toBe(true);
    expect(body.spec.inputs).toEqual(V3_INPUTS);
  });

  it('block input_id and weight flow through verbatim', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X',
          weight: 0.4,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'constant', value: 1 },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const block = body.spec.rules.long_entry[0];
    expect(block.input_id).toBe('X');
    expect(block.weight).toBe(0.4);
    // No more instrument key on blocks.
    expect('instrument' in block).toBe(false);
  });

  it('ships indicator specs as an array with {id,name,code,params,seriesMap}', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X', weight: 0,
          conditions: [
            { op: 'cross_above',
              lhs: { kind: 'indicator', indicator_id: 'sma-20', input_id: 'X', output: 'default' },
              rhs: { kind: 'indicator', indicator_id: 'rsi-14', input_id: 'X', output: 'default' } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const indicators = [
      { id: 'sma-20', name: 'SMA-20', code: 'SMA_CODE', params: { w: 20 }, seriesMap: { price: null } },
      { id: 'rsi-14', name: 'RSI-14', code: 'RSI_CODE', params: { w: 14 }, seriesMap: { price: null } },
      { id: 'unused', name: 'unused', code: 'X', params: {}, seriesMap: {} },
    ];
    const { body } = buildComputeRequestBody(signal, indicators);
    const ids = body.indicators.map((i) => i.id).sort();
    expect(ids).toEqual(['rsi-14', 'sma-20']);
    expect(body.indicators.find((i) => i.id === 'unused')).toBeUndefined();
  });

  it('always emits params_override + series_override keys on indicator operands', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X', weight: 0,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, [
      { id: 'sma', name: 'sma', code: 'X', params: {}, seriesMap: {} },
    ]);
    const lhs = body.spec.rules.long_entry[0].conditions[0].lhs;
    expect('params_override' in lhs).toBe(true);
    expect('series_override' in lhs).toBe(true);
    expect(lhs.params_override).toBe(null);
    expect(lhs.series_override).toBe(null);
    const rt = JSON.parse(JSON.stringify(body));
    const rtLhs = rt.spec.rules.long_entry[0].conditions[0].lhs;
    expect('params_override' in rtLhs).toBe(true);
    expect('series_override' in rtLhs).toBe(true);
  });

  it('passes non-null override payloads through verbatim (series_override maps label → input_id)', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X', weight: 0,
          conditions: [
            { op: 'gt',
              lhs: {
                kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default',
                params_override: { window: 50 },
                series_override: { secondary: 'Y' },
              },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, [
      { id: 'sma', name: 'sma', code: 'X', params: { window: 20 }, seriesMap: { price: null } },
    ]);
    const lhs = body.spec.rules.long_entry[0].conditions[0].lhs;
    expect(lhs.params_override).toEqual({ window: 50 });
    expect(lhs.series_override).toEqual({ secondary: 'Y' });
  });

  it('does NOT add override keys to non-indicator operands', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X', weight: 0,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
              rhs: { kind: 'constant', value: 100 } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const cond = body.spec.rules.long_entry[0].conditions[0];
    expect(cond.lhs.params_override).toBeUndefined();
    expect(cond.rhs.params_override).toBeUndefined();
  });

  it('returns missing indicator ids if any reference is unresolved', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X', weight: 0,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'does-not-exist', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const { body, missing } = buildComputeRequestBody(signal, []);
    expect(missing).toEqual(['does-not-exist']);
    expect(body.indicators).toEqual([]);
  });

  it('collects indicator ids across all four directions and condition variants', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{ input_id: 'X', weight: 0, conditions: [
          { op: 'gt',
            lhs: { kind: 'indicator', indicator_id: 'a', input_id: 'X', output: 'default' },
            rhs: { kind: 'constant', value: 0 } },
        ] }],
        long_exit: [{ input_id: 'X', weight: 0, conditions: [
          { op: 'in_range',
            operand: { kind: 'indicator', indicator_id: 'b', input_id: 'X', output: 'default' },
            min:     { kind: 'indicator', indicator_id: 'c', input_id: 'X', output: 'default' },
            max:     { kind: 'constant', value: 1 } },
        ] }],
        short_entry: [{ input_id: 'X', weight: 0, conditions: [
          { op: 'rolling_lt',
            operand: { kind: 'indicator', indicator_id: 'd', input_id: 'X', output: 'default' },
            lookback: 5 },
        ] }],
        short_exit: [{ input_id: 'X', weight: 0, conditions: [
          { op: 'cross_below',
            lhs: { kind: 'indicator', indicator_id: 'e', input_id: 'X', output: 'default' },
            rhs: { kind: 'instrument', input_id: 'X', field: 'close' } },
        ] }],
      },
    };
    const ids = collectIndicatorIds(signal);
    expect(Array.from(ids).sort()).toEqual(['a', 'b', 'c', 'd', 'e']);
  });
});

describe('normaliseSpecForRequest does not mutate caller data', () => {
  it('produces a new rules object without touching the original operand shape', () => {
    const operand = { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' };
    const signal = {
      id: 's1', name: 'S1', inputs: V3_INPUTS,
      rules: {
        long_entry: [{
          input_id: 'X', weight: 0,
          conditions: [{ op: 'gt', lhs: operand, rhs: { kind: 'constant', value: 0 } }],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const normalised = normaliseSpecForRequest(signal);
    expect(operand.params_override).toBeUndefined();
    expect(operand.series_override).toBeUndefined();
    const normLhs = normalised.rules.long_entry[0].conditions[0].lhs;
    expect(normLhs.params_override).toBe(null);
    expect(normLhs.series_override).toBe(null);
  });
});
