import { describe, it, expect } from 'vitest';
import { buildComputeRequestBody } from './requestBuilder';
import { collectIndicatorIds } from '../../api/signals';

// The request body shape is pinned by PLAN.md § Contract. This test
// protects against accidental drift between the frontend producer and
// the backend consumer.

describe('computeSignal request body shape', () => {
  it('wraps spec, indicators, and instruments at the top level', () => {
    const signal = {
      id: 's1',
      name: 'S1',
      rules: {
        long_entry: [{ conditions: [
          { op: 'gt',
            lhs: { kind: 'indicator', indicator_id: 'sma-20', output: 'default' },
            rhs: { kind: 'constant', value: 0 } },
        ] }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const indicators = [
      { id: 'sma-20', name: '20-day SMA', code: 'def compute(series, window=20): return series["price"]',
        params: { window: 20 },
        seriesMap: { price: { collection: 'INDEX', instrument_id: '^GSPC' } } },
    ];
    const { body, missing } = buildComputeRequestBody(signal, indicators);
    expect(missing).toEqual([]);
    expect(Object.keys(body).sort()).toEqual(['indicators', 'instruments', 'spec']);
    expect(body.spec).toBe(signal);
    expect(body.instruments).toEqual({});
  });

  it('ships the indicator spec keyed by indicator_id', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{ conditions: [
          { op: 'cross_above',
            lhs: { kind: 'indicator', indicator_id: 'sma-20', output: 'default' },
            rhs: { kind: 'indicator', indicator_id: 'rsi-14', output: 'default' } },
        ] }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const indicators = [
      { id: 'sma-20', name: 'SMA-20', code: 'SMA_CODE', params: { w: 20 }, seriesMap: { price: null } },
      { id: 'rsi-14', name: 'RSI-14', code: 'RSI_CODE', params: { w: 14 }, seriesMap: { price: null } },
      { id: 'unused', name: 'unused', code: 'X', params: {}, seriesMap: {} },
    ];
    const { body } = buildComputeRequestBody(signal, indicators);
    expect(Object.keys(body.indicators).sort()).toEqual(['rsi-14', 'sma-20']);
    expect(body.indicators['sma-20']).toEqual({
      code: 'SMA_CODE', params: { w: 20 }, seriesMap: { price: null },
    });
    expect(body.indicators['rsi-14']).toEqual({
      code: 'RSI_CODE', params: { w: 14 }, seriesMap: { price: null },
    });
    // Unreferenced indicators are omitted — the request is minimal.
    expect(body.indicators.unused).toBeUndefined();
  });

  it('returns missing indicator ids if any reference is unresolved', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{ conditions: [
          { op: 'gt',
            lhs: { kind: 'indicator', indicator_id: 'does-not-exist', output: 'default' },
            rhs: { kind: 'constant', value: 0 } },
        ] }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const { body, missing } = buildComputeRequestBody(signal, []);
    expect(missing).toEqual(['does-not-exist']);
    expect(body.indicators).toEqual({});
  });

  it('ignores non-indicator operands when collecting indicator ids', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{ conditions: [
          { op: 'gt',
            lhs: { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' },
            rhs: { kind: 'constant', value: 100 } },
        ] }],
        long_exit: [], short_entry: [], short_exit: [],
      },
    };
    const ids = collectIndicatorIds(signal);
    expect(Array.from(ids)).toEqual([]);
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.indicators).toEqual({});
  });

  it('collects indicator ids across ALL four directions and condition variants', () => {
    const signal = {
      id: 's1', name: 'S1',
      rules: {
        long_entry: [{ conditions: [
          { op: 'gt',
            lhs: { kind: 'indicator', indicator_id: 'a', output: 'default' },
            rhs: { kind: 'constant', value: 0 } },
        ] }],
        long_exit: [{ conditions: [
          { op: 'in_range',
            operand: { kind: 'indicator', indicator_id: 'b', output: 'default' },
            min:     { kind: 'indicator', indicator_id: 'c', output: 'default' },
            max:     { kind: 'constant', value: 1 } },
        ] }],
        short_entry: [{ conditions: [
          { op: 'rolling_lt',
            operand: { kind: 'indicator', indicator_id: 'd', output: 'default' },
            lookback: 5 },
        ] }],
        short_exit: [{ conditions: [
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
