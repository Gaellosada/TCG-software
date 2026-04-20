import { describe, it, expect } from 'vitest';
import { buildComputeRequestBody, normaliseSpecForRequest } from './requestBuilder';
import { collectIndicatorIds } from '../../api/signals';

// The request body shape is pinned by PLAN.md § Authoritative v2 contract.
// This test protects against accidental drift between the frontend
// producer and the backend consumer.

describe('computeSignal request body shape (v2)', () => {
  it('top level has exactly {spec, indicators}', () => {
    const signal = {
      id: 's1',
      name: 'S1',
      rules: {
        long_entry: [{
          instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
          weight: 0.5,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'sma-20', output: 'default' },
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
        seriesMap: { price: { collection: 'INDEX', instrument_id: '^GSPC' } } },
    ];
    const { body, missing } = buildComputeRequestBody(signal, indicators);
    expect(missing).toEqual([]);
    expect(Object.keys(body).sort()).toEqual(['indicators', 'spec']);
    expect(body.spec.id).toBe('s1');
    expect(body.spec.name).toBe('S1');
    // indicators is an ARRAY in v2 (was a map in v1).
    expect(Array.isArray(body.indicators)).toBe(true);
  });

  it('block instrument and weight flow through verbatim', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
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
    expect(block.instrument).toEqual({ collection: 'INDEX', instrument_id: '^GSPC' });
    expect(block.weight).toBe(0.4);
  });

  it('ships indicator specs as an array with {id,name,code,params,seriesMap}', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: null, weight: 0,
          conditions: [
            { op: 'cross_above',
              lhs: { kind: 'indicator', indicator_id: 'sma-20', output: 'default' },
              rhs: { kind: 'indicator', indicator_id: 'rsi-14', output: 'default' } },
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
    const smaSpec = body.indicators.find((i) => i.id === 'sma-20');
    expect(smaSpec).toEqual({
      id: 'sma-20', name: 'SMA-20', code: 'SMA_CODE', params: { w: 20 }, seriesMap: { price: null },
    });
    // Unreferenced indicators are omitted — the request is minimal.
    expect(body.indicators.find((i) => i.id === 'unused')).toBeUndefined();
  });

  it('always emits params_override + series_override keys on indicator operands (null ⇒ null, not omitted)', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: null, weight: 0,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'sma', output: 'default' },
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
    // Roundtrip through JSON to confirm keys actually serialise.
    const roundtripped = JSON.parse(JSON.stringify(body));
    const rtLhs = roundtripped.spec.rules.long_entry[0].conditions[0].lhs;
    expect('params_override' in rtLhs).toBe(true);
    expect('series_override' in rtLhs).toBe(true);
  });

  it('passes non-null override payloads through verbatim', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: null, weight: 0,
          conditions: [
            { op: 'gt',
              lhs: {
                kind: 'indicator', indicator_id: 'sma', output: 'default',
                params_override: { window: 50 },
                series_override: { price: { collection: 'INDEX', instrument_id: '^NDX' } },
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
    expect(lhs.series_override).toEqual({ price: { collection: 'INDEX', instrument_id: '^NDX' } });
  });

  it('does NOT add override keys to non-indicator operands', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: null, weight: 0,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' },
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
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: null, weight: 0,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'does-not-exist', output: 'default' },
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

  it('ignores non-indicator operands when collecting indicator ids', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: null, weight: 0,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' },
              rhs: { kind: 'constant', value: 100 } },
          ],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const ids = collectIndicatorIds(signal);
    expect(Array.from(ids)).toEqual([]);
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.indicators).toEqual([]);
  });

  it('collects indicator ids across all four directions and condition variants', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{ instrument: null, weight: 0, conditions: [
          { op: 'gt',
            lhs: { kind: 'indicator', indicator_id: 'a', output: 'default' },
            rhs: { kind: 'constant', value: 0 } },
        ] }],
        long_exit: [{ instrument: null, weight: 0, conditions: [
          { op: 'in_range',
            operand: { kind: 'indicator', indicator_id: 'b', output: 'default' },
            min:     { kind: 'indicator', indicator_id: 'c', output: 'default' },
            max:     { kind: 'constant', value: 1 } },
        ] }],
        short_entry: [{ instrument: null, weight: 0, conditions: [
          { op: 'rolling_lt',
            operand: { kind: 'indicator', indicator_id: 'd', output: 'default' },
            lookback: 5 },
        ] }],
        short_exit: [{ instrument: null, weight: 0, conditions: [
          { op: 'cross_below',
            lhs: { kind: 'indicator', indicator_id: 'e', output: 'default' },
            rhs: { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' } },
        ] }],
      },
    };
    const ids = collectIndicatorIds(signal);
    expect(Array.from(ids).sort()).toEqual(['a', 'b', 'c', 'd', 'e']);
  });
});

describe('normaliseSpecForRequest does not mutate caller data', () => {
  it('produces a new rules object without touching the original operand shape', () => {
    const operand = { kind: 'indicator', indicator_id: 'sma', output: 'default' };
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{
          instrument: null, weight: 0,
          conditions: [{ op: 'gt', lhs: operand, rhs: { kind: 'constant', value: 0 } }],
        }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const normalised = normaliseSpecForRequest(signal);
    // Original operand is untouched — override keys exist only on the
    // normalised copy.
    expect(operand.params_override).toBeUndefined();
    expect(operand.series_override).toBeUndefined();
    const normLhs = normalised.rules.long_entry[0].conditions[0].lhs;
    expect(normLhs.params_override).toBe(null);
    expect(normLhs.series_override).toBe(null);
  });
});
