import { describe, it, expect } from 'vitest';
import { buildComputeRequestBody, normaliseSpecForRequest } from './requestBuilder';
import { collectIndicatorIds } from '../../api/signals';

// Request body shape pinned by PLAN.md § Wire contract (v4).
// Guards against drift between frontend producer and backend consumer.

const V4_INPUTS = [
  { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
  { id: 'Y', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'NDX' } },
];

describe('computeSignal request body shape (v4)', () => {
  it('top level has exactly {spec, indicators}; rules carry entries/exits only', () => {
    const signal = {
      id: 's1',
      name: 'S1',
      inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1',
          input_id: 'X',
          weight: 50,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'sma-20', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        exits: [],
      },
      settings: { dont_repeat: true },
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
    expect(Array.isArray(body.spec.inputs)).toBe(true);
    expect(body.spec.inputs).toEqual(V4_INPUTS);
    // Rules keys are exactly entries+exits — no legacy direction keys.
    expect(Object.keys(body.spec.rules).sort()).toEqual(['entries', 'exits']);
    // Settings flow through.
    expect(body.spec.settings).toEqual({ dont_repeat: true });
  });

  it('block id, name, input_id, signed weight and target_entry_block_name flow through verbatim', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'entry-42',
          name: 'Alpha',
          input_id: 'X',
          weight: -30,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'constant', value: 1 },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        exits: [{
          id: 'exit-9',
          name: 'Exit1',
          // Legacy stored values that the request builder must drop so
          // the wire payload never carries block-level input_id on exits.
          input_id: 'X',
          weight: 0,
          target_entry_block_name: 'Alpha',
          conditions: [
            { op: 'gt',
              lhs: { kind: 'constant', value: 1 },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const entry = body.spec.rules.entries[0];
    expect(entry.id).toBe('entry-42');
    expect(entry.name).toBe('Alpha');
    expect(entry.input_id).toBe('X');
    expect(entry.weight).toBe(-30);
    // Entry blocks do NOT carry target_entry_block_name or target_entry_block_id.
    expect('target_entry_block_name' in entry).toBe(false);
    expect('target_entry_block_id' in entry).toBe(false);
    const exit = body.spec.rules.exits[0];
    expect(exit.id).toBe('exit-9');
    expect(exit.name).toBe('Exit1');
    expect(exit.target_entry_block_name).toBe('Alpha');
    // Exit blocks must NOT carry legacy target_entry_block_id.
    expect('target_entry_block_id' in exit).toBe(false);
    // Exit blocks must NOT carry block-level input_id or weight on the
    // wire — the backend rejects payloads with non-empty input_id.
    expect('input_id' in exit).toBe(false);
    expect('weight' in exit).toBe(false);
    // No more instrument key on blocks.
    expect('instrument' in entry).toBe(false);
  });

  it('clamps |weight| > 100 at normalisation (no leverage escapes the wire)', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [
          { id: 'e1', input_id: 'X', weight: 250, conditions: [] },
          { id: 'e2', input_id: 'X', weight: -250, conditions: [] },
        ],
        exits: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const weights = body.spec.rules.entries.map((b) => b.weight);
    expect(weights).toEqual([100, -100]);
  });

  it('ships indicator specs as an array with {id,name,code,params,seriesMap}', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 10,
          conditions: [
            { op: 'cross_above',
              lhs: { kind: 'indicator', indicator_id: 'sma-20', input_id: 'X', output: 'default' },
              rhs: { kind: 'indicator', indicator_id: 'rsi-14', input_id: 'X', output: 'default' } },
          ],
        }],
        exits: [],
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
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 10,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        exits: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, [
      { id: 'sma', name: 'sma', code: 'X', params: {}, seriesMap: {} },
    ]);
    const lhs = body.spec.rules.entries[0].conditions[0].lhs;
    expect('params_override' in lhs).toBe(true);
    expect('series_override' in lhs).toBe(true);
    expect(lhs.params_override).toBe(null);
    expect(lhs.series_override).toBe(null);
    const rt = JSON.parse(JSON.stringify(body));
    const rtLhs = rt.spec.rules.entries[0].conditions[0].lhs;
    expect('params_override' in rtLhs).toBe(true);
    expect('series_override' in rtLhs).toBe(true);
  });

  it('passes non-null override payloads through verbatim (series_override maps label → input_id)', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 10,
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
        exits: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, [
      { id: 'sma', name: 'sma', code: 'X', params: { window: 20 }, seriesMap: { price: null } },
    ]);
    const lhs = body.spec.rules.entries[0].conditions[0].lhs;
    expect(lhs.params_override).toEqual({ window: 50 });
    expect(lhs.series_override).toEqual({ secondary: 'Y' });
  });

  it('does NOT add override keys to non-indicator operands', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 10,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
              rhs: { kind: 'constant', value: 100 } },
          ],
        }],
        exits: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const cond = body.spec.rules.entries[0].conditions[0];
    expect(cond.lhs.params_override).toBeUndefined();
    expect(cond.rhs.params_override).toBeUndefined();
  });

  it('returns missing indicator ids if any reference is unresolved', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 10,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'does-not-exist', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        exits: [],
      },
    };
    const { body, missing } = buildComputeRequestBody(signal, []);
    expect(missing).toEqual(['does-not-exist']);
    expect(body.indicators).toEqual([]);
  });

  it('collects indicator ids across both sections and every condition variant', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [
          { id: 'e1', input_id: 'X', weight: 10, conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'a', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ] },
          { id: 'e2', input_id: 'X', weight: -5, conditions: [
            { op: 'rolling_lt',
              operand: { kind: 'indicator', indicator_id: 'd', input_id: 'X', output: 'default' },
              lookback: 5 },
          ] },
        ],
        exits: [
          { id: 'x1', input_id: 'X', weight: 0, target_entry_block_name: 'e1', conditions: [
            { op: 'in_range',
              operand: { kind: 'indicator', indicator_id: 'b', input_id: 'X', output: 'default' },
              min:     { kind: 'indicator', indicator_id: 'c', input_id: 'X', output: 'default' },
              max:     { kind: 'constant', value: 1 } },
          ] },
          { id: 'x2', input_id: 'X', weight: 0, target_entry_block_name: 'e2', conditions: [
            { op: 'cross_below',
              lhs: { kind: 'indicator', indicator_id: 'e', input_id: 'X', output: 'default' },
              rhs: { kind: 'instrument', input_id: 'X', field: 'close' } },
          ] },
        ],
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
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 10,
          conditions: [{ op: 'gt', lhs: operand, rhs: { kind: 'constant', value: 0 } }],
        }],
        exits: [],
      },
    };
    const normalised = normaliseSpecForRequest(signal);
    expect(operand.params_override).toBeUndefined();
    expect(operand.series_override).toBeUndefined();
    const normLhs = normalised.rules.entries[0].conditions[0].lhs;
    expect(normLhs.params_override).toBe(null);
    expect(normLhs.series_override).toBe(null);
  });
});
