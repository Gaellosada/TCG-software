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
  it('top level has exactly {spec, indicators}; rules carry entries/exits/resets', () => {
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
    // Rules keys are exactly entries+exits+resets — no legacy direction keys.
    expect(Object.keys(body.spec.rules).sort()).toEqual(['entries', 'exits', 'resets']);
    // Settings flow through.
    expect(body.spec.settings).toEqual({ dont_repeat: true });
  });

  it('block id, name, input_id, signed weight and target_entry_block_names flow through verbatim', () => {
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
          // v6: plural target-names array on the wire.
          target_entry_block_names: ['Alpha'],
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
    // Entry blocks do NOT carry target_entry_block_names or the legacy keys.
    expect('target_entry_block_names' in entry).toBe(false);
    expect('target_entry_block_name' in entry).toBe(false);
    expect('target_entry_block_id' in entry).toBe(false);
    const exit = body.spec.rules.exits[0];
    expect(exit.id).toBe('exit-9');
    expect(exit.name).toBe('Exit1');
    expect(exit.target_entry_block_names).toEqual(['Alpha']);
    // The singular legacy key must NOT ride the wire.
    expect('target_entry_block_name' in exit).toBe(false);
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

  it('cleans seriesMap: null entries become placeholders, filled entries lose `type`', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 10,
          conditions: [
            { op: 'gt',
              lhs: { kind: 'indicator', indicator_id: 'ind-1', input_id: 'X', output: 'default' },
              rhs: { kind: 'constant', value: 0 } },
          ],
        }],
        exits: [],
      },
    };
    const indicators = [
      {
        id: 'ind-1', name: 'Ind', code: 'CODE', params: {},
        seriesMap: {
          price: null,
          close: { collection: 'INDEX', instrument_id: 'SPX', type: 'spot' },
        },
      },
    ];
    const { body } = buildComputeRequestBody(signal, indicators);
    const ind = body.indicators.find((i) => i.id === 'ind-1');
    // Null entry gets placeholder so backend sees the key in series_labels.
    expect(ind.seriesMap.price).toEqual({ collection: '_', instrument_id: '_' });
    // Filled entry keeps collection + instrument_id but `type` is stripped.
    expect(ind.seriesMap.close).toEqual({ collection: 'INDEX', instrument_id: 'SPX' });
    expect(ind.seriesMap.close.type).toBeUndefined();
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
          { id: 'x1', input_id: 'X', weight: 0, target_entry_block_names: ['e1'], conditions: [
            { op: 'in_range',
              operand: { kind: 'indicator', indicator_id: 'b', input_id: 'X', output: 'default' },
              min:     { kind: 'indicator', indicator_id: 'c', input_id: 'X', output: 'default' },
              max:     { kind: 'constant', value: 1 } },
          ] },
          { id: 'x2', input_id: 'X', weight: 0, target_entry_block_names: ['e2'], conditions: [
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

describe('enabled and description round-trip through normaliseBlock (B1 regression)', () => {
  const baseEntry = {
    id: 'e1', name: 'E1', input_id: 'X', weight: 50,
    conditions: [],
  };
  const baseExit = {
    id: 'x1', name: 'X1', target_entry_block_names: ['E1'],
    conditions: [],
  };

  it('entry block with enabled:false reaches the wire body as enabled:false', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: { entries: [{ ...baseEntry, enabled: false }], exits: [] },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].enabled).toBe(false);
  });

  it('entry block with description:"hello world" reaches the wire body intact', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: { entries: [{ ...baseEntry, description: 'hello world' }], exits: [] },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].description).toBe('hello world');
  });

  it('exit block with enabled:false reaches the wire body as enabled:false', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: { entries: [baseEntry], exits: [{ ...baseExit, enabled: false }] },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.exits[0].enabled).toBe(false);
  });

  it('exit block with description:"hello world" reaches the wire body intact', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: { entries: [baseEntry], exits: [{ ...baseExit, description: 'hello world' }] },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.exits[0].description).toBe('hello world');
  });

  it('block with neither field set defaults to enabled:true and description:""', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [{ ...baseEntry }],
        exits: [{ ...baseExit }],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].enabled).toBe(true);
    expect(body.spec.rules.entries[0].description).toBe('');
    expect(body.spec.rules.exits[0].enabled).toBe(true);
    expect(body.spec.rules.exits[0].description).toBe('');
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

  // T16
  it('normaliseBlock for resets emits the whitelist + POST body has rules.resets', () => {
    const signal = {
      id: 's1', name: 'S1', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [],
        resets: [
          {
            id: 'r1',
            name: 'Arm',
            // Smuggled fields — must NOT appear on the wire.
            input_id: 'X',
            weight: 42,
            target_entry_block_name: 'Alpha',
            conditions: [
              {
                op: 'gt',
                lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
                rhs: { kind: 'constant', value: 100 },
              },
            ],
            enabled: true,
            description: 'desc',
          },
        ],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(Array.isArray(body.spec.rules.resets)).toBe(true);
    const r = body.spec.rules.resets[0];
    expect(r.id).toBe('r1');
    expect(r.name).toBe('Arm');
    expect(r.enabled).toBe(true);
    expect(r.description).toBe('desc');
    expect(Array.isArray(r.conditions)).toBe(true);
    expect(r.conditions).toHaveLength(1);
    // Whitelist enforcement: forbidden fields must be absent.
    expect('input_id' in r).toBe(false);
    expect('weight' in r).toBe(false);
    expect('target_entry_block_name' in r).toBe(false);
    expect('target_entry_block_names' in r).toBe(false);
  });
});

// Per CONTRACT §6.2 — requires_reset_block_id is whitelisted on
// entries+exits and OMITTED from resets (backend rejects payloads
// where a reset block carries the field).
describe('normaliseBlock — requires_reset_block_id whitelist', () => {
  const RESET_ID = 'reset-uuid-42';

  it('emits requires_reset_block_id on entries (verbatim id)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: '',
          conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } }],
          requires_reset_block_id: RESET_ID,
        }],
        exits: [],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].requires_reset_block_id).toBe(RESET_ID);
  });

  it('emits requires_reset_block_id on exits (verbatim id)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [{
          id: 'x1', name: '', target_entry_block_names: ['Alpha'],
          conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } }],
          requires_reset_block_id: RESET_ID,
        }],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.exits[0].requires_reset_block_id).toBe(RESET_ID);
  });

  it('emits explicit null when missing/empty/non-string on entries+exits', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: '', conditions: [],
          // field absent
        }],
        exits: [{
          id: 'x1', name: '', target_entry_block_names: ['Alpha'], conditions: [],
          requires_reset_block_id: '',  // empty string → null
        }],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].requires_reset_block_id).toBe(null);
    expect(body.spec.rules.exits[0].requires_reset_block_id).toBe(null);
  });

  it('OMITS requires_reset_block_id from reset blocks (Sign 4)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [],
        resets: [{
          id: 'r1', name: 'Arm', conditions: [],
          // Tampered upstream payload — must not leak to the wire.
          requires_reset_block_id: 'tampered',
        }],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect('requires_reset_block_id' in body.spec.rules.resets[0]).toBe(false);
  });
});

// requires_reset_count is whitelisted on entries+exits (the count of
// reset fires required before the block re-arms) and OMITTED from resets
// (the count lives on the binder, never on the reset itself). Whitelist
// note in requestBuilder.js mandates this matching round-trip test.
describe('normaliseBlock — requires_reset_count whitelist', () => {
  it('emits requires_reset_count on entries (verbatim integer)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: '',
          conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } }],
          requires_reset_block_id: 'reset-uuid-42',
          requires_reset_count: 3,
        }],
        exits: [],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].requires_reset_count).toBe(3);
  });

  it('emits requires_reset_count on exits (verbatim integer)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [{
          id: 'x1', name: '', target_entry_block_names: ['Alpha'],
          conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } }],
          requires_reset_block_id: 'reset-uuid-42',
          requires_reset_count: 5,
        }],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.exits[0].requires_reset_count).toBe(5);
  });

  it('defaults requires_reset_count to 1 when missing/invalid on entries+exits', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: '', conditions: [],
          // requires_reset_count absent
        }],
        exits: [{
          id: 'x1', name: '', target_entry_block_names: ['Alpha'], conditions: [],
          requires_reset_count: 0, // invalid (<1) → clamp to 1
        }],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].requires_reset_count).toBe(1);
    expect(body.spec.rules.exits[0].requires_reset_count).toBe(1);
  });

  it('coerces a non-integer requires_reset_count to an integer on the wire', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: '', conditions: [],
          // Bind a reset so the COERCION (floor) path is exercised — an
          // unbound block would be forced to 1 by the orphan-kill rule.
          requires_reset_block_id: 'reset-uuid-42',
          requires_reset_count: 2.9,
        }],
        exits: [],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const v = body.spec.rules.entries[0].requires_reset_count;
    expect(Number.isInteger(v)).toBe(true);
    expect(v).toBe(2);
  });

  it('OMITS requires_reset_count from reset blocks', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [],
        resets: [{
          id: 'r1', name: 'Arm', conditions: [],
          // Tampered upstream payload — must not leak to the wire.
          requires_reset_count: 7,
        }],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect('requires_reset_count' in body.spec.rules.resets[0]).toBe(false);
  });
});

// Orphan-count-on-wire: a block with NO reset bound must emit
// requires_reset_count = 1, regardless of any stale stored count. The wire
// binding (requires_reset_block_id) and the wire count must agree — a count
// only has meaning when a reset is bound.
describe('normaliseBlock — orphan requires_reset_count is forced to 1 on the wire', () => {
  it('forces the count to 1 on an unbound entry block (stored count 5)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: '', conditions: [],
          requires_reset_block_id: null, // no binding
          requires_reset_count: 5, // orphan — must not ride the wire
        }],
        exits: [],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.entries[0].requires_reset_block_id).toBe(null);
    expect(body.spec.rules.entries[0].requires_reset_count).toBe(1);
  });

  it('forces the count to 1 on an unbound exit block (stored count 5)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [{
          id: 'x1', name: '', target_entry_block_names: ['Alpha'], conditions: [],
          requires_reset_block_id: null, // no binding
          requires_reset_count: 5, // orphan — must not ride the wire
        }],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect(body.spec.rules.exits[0].requires_reset_block_id).toBe(null);
    expect(body.spec.rules.exits[0].requires_reset_count).toBe(1);
  });
});

// Block-level temporal ``links`` (block-temporal-composition v1).
//
// ``links`` is a flat map { "<successor_condition_index>": <within_bars_int> }
// keyed by the SUCCESSOR condition's index within the block. It MUST be added
// to the entry+exit whitelist in normaliseBlock and OMITTED from resets
// (a reset block carrying links is rejected by the backend with HTTP 400 —
// it must never reach the wire). This is the G4 gate: without the whitelist
// line, a block authored as a temporal chain silently runs as plain CNF on
// the backend because normaliseBlock rebuilds each section literal with NO
// spread, dropping any field not explicitly copied.
describe('normaliseBlock — block-level temporal links whitelist (G4)', () => {
  it('emits links on ENTRY blocks (flat {successor_index: within_bars} verbatim)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: 'Chain',
          conditions: [
            { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
            { op: 'gt', lhs: { kind: 'constant', value: 2 }, rhs: { kind: 'constant', value: 0 } },
            { op: 'gt', lhs: { kind: 'constant', value: 3 }, rhs: { kind: 'constant', value: 0 } },
          ],
          links: { 1: 5, 2: 3 },
        }],
        exits: [],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const entry = body.spec.rules.entries[0];
    expect('links' in entry).toBe(true);
    // JSON object keys are strings; values are the within-bar windows.
    expect(entry.links).toEqual({ 1: 5, 2: 3 });
    // The chain survives a JSON round-trip (the actual wire serialisation).
    const rt = JSON.parse(JSON.stringify(body)).spec.rules.entries[0];
    expect(rt.links).toEqual({ 1: 5, 2: 3 });
  });

  it('emits links on EXIT blocks (sequence exits are allowed)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [{
          id: 'x1', name: 'Exit', target_entry_block_names: ['Alpha'],
          conditions: [
            { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
            { op: 'gt', lhs: { kind: 'constant', value: 2 }, rhs: { kind: 'constant', value: 0 } },
          ],
          links: { 1: 7 },
        }],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const exit = body.spec.rules.exits[0];
    expect('links' in exit).toBe(true);
    expect(exit.links).toEqual({ 1: 7 });
  });

  it('OMITS links from the wire when a block has no temporal chain (zero-link == CNF, byte-identical)', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: 'Plain',
          conditions: [
            { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
          ],
          // no links key at all
        }],
        exits: [{
          id: 'x1', name: 'Exit', target_entry_block_names: ['Plain'],
          conditions: [
            { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
          ],
          links: {}, // empty map also folds to CNF — must not ride the wire
        }],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    // A plain CNF block must NOT grow a links key — the payload stays
    // byte-identical to a pre-feature signal so G1 (CNF byte-identical) holds.
    expect('links' in body.spec.rules.entries[0]).toBe(false);
    expect('links' in body.spec.rules.exits[0]).toBe(false);
  });

  it('OMITS links from RESET blocks even when an upstream payload smuggles it in', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [],
        exits: [],
        resets: [{
          id: 'r1', name: 'Arm',
          conditions: [
            { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 0 } },
            { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 1 } },
          ],
          // Tampered payload — the backend rejects links on resets with HTTP
          // 400, so the wire builder must strip it before it ever gets there.
          links: { 1: 4 },
        }],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    expect('links' in body.spec.rules.resets[0]).toBe(false);
  });

  it('drops a malformed links map (non-object / NaN windows) rather than shipping garbage', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50, name: 'Garbled',
          conditions: [
            { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
            { op: 'gt', lhs: { kind: 'constant', value: 2 }, rhs: { kind: 'constant', value: 0 } },
          ],
          links: { 1: 'oops', 2: 0, bogus: 4 }, // all entries invalid → drop
        }],
        exits: [],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    // No salvageable links → omit entirely (folds to CNF) rather than ship a
    // garbled map the backend would 400 on.
    expect('links' in body.spec.rules.entries[0]).toBe(false);
  });
});

// cross_count fields (count/window) ride the wire on a CrossCondition.
// normaliseCondition spreads ``...condition`` so scalar fields flow through,
// but a round-trip test pins the contract against silent regressions.
describe('normaliseCondition — cross count/window flow through (SC5)', () => {
  it('passes count + window verbatim on a cross condition', () => {
    const signal = {
      id: 's', name: 'S', inputs: V4_INPUTS,
      rules: {
        entries: [{
          id: 'e1', input_id: 'X', weight: 50,
          conditions: [
            {
              op: 'cross_above',
              lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
              rhs: { kind: 'constant', value: 0 },
              count: 3,
              window: 10,
            },
          ],
        }],
        exits: [],
        resets: [],
      },
    };
    const { body } = buildComputeRequestBody(signal, []);
    const cond = body.spec.rules.entries[0].conditions[0];
    expect(cond.count).toBe(3);
    expect(cond.window).toBe(10);
  });
});
