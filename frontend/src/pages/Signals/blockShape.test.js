import { describe, it, expect } from 'vitest';
import {
  defaultBlock,
  isInputConfigured,
  isOperandComplete,
  isConditionComplete,
  indexInputs,
  collectEntryIds,
  isBlockRunnable,
} from './blockShape';

// Inputs fixtures (v5 — unchanged from v3).
const SPOT_INPUT = { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } };
const SPOT_INPUT_BAD = { id: 'Y', instrument: { type: 'spot', collection: '', instrument_id: 'SPX' } };
const CONT_INPUT = {
  id: 'Z',
  instrument: {
    type: 'continuous',
    collection: 'FUT_ES',
    adjustment: 'none',
    cycle: 'all',
    rollOffset: 2,
    strategy: 'front_month',
  },
};
const OPT_STREAM_INPUT = {
  id: 'O',
  instrument: {
    type: 'option_stream',
    collection: 'OPT_SPX',
    option_type: 'C',
    cycle: 'W3_FRI',
    maturity: { kind: 'fixed', value: '2025-06-20' },
    selection: { kind: 'delta', value: 0.3 },
    stream: 'iv',
  },
};
const INPUTS = [SPOT_INPUT, CONT_INPUT, OPT_STREAM_INPUT];
const INPUTS_BY_ID = indexInputs(INPUTS);

// Operands (v5 — unchanged from v3).
const INST_OP_OK = { kind: 'instrument', input_id: 'X', field: 'close' };
const INST_OP_NO_FIELD = { kind: 'instrument', input_id: 'X' };
const INST_OP_UNKNOWN = { kind: 'instrument', input_id: 'NONE', field: 'close' };
const CONST_OK = { kind: 'constant', value: 1 };
const IND_OP_OK = { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' };
const IND_OP_NO_INPUT = { kind: 'indicator', indicator_id: 'sma', input_id: '', output: null };
const IND_OP_UNKNOWN_INPUT = { kind: 'indicator', indicator_id: 'sma', input_id: 'NONE', output: null };

describe('defaultBlock (v5)', () => {
  it('entry block defaults: id, input_id: "", weight: 0, conditions: [], enabled: true, description: ""', () => {
    const b = defaultBlock('entries');
    expect(typeof b.id).toBe('string');
    expect(b.id.length).toBeGreaterThan(0);
    expect(b.input_id).toBe('');
    expect(b.weight).toBe(0);
    expect(b.conditions).toEqual([]);
    expect(b.enabled).toBe(true);
    expect(b.description).toBe('');
    expect('target_entry_block_id' in b).toBe(false);
  });

  it('exit block adds target_entry_block_names: [] and omits block-level input_id/weight', () => {
    const b = defaultBlock('exits');
    expect(b.target_entry_block_names).toEqual([]);
    expect('target_entry_block_name' in b).toBe(false);
    expect(b.enabled).toBe(true);
    expect(b.description).toBe('');
    expect('target_entry_block_id' in b).toBe(false);
    expect('input_id' in b).toBe(false);
    expect('weight' in b).toBe(false);
  });

  it('defaults to entries when no section given', () => {
    const b = defaultBlock();
    expect('target_entry_block_names' in b).toBe(false);
    expect('target_entry_block_name' in b).toBe(false);
    expect('target_entry_block_id' in b).toBe(false);
    expect(b.enabled).toBe(true);
    expect(b.description).toBe('');
  });

  it('returns a fresh object each call with a distinct id', () => {
    const a = defaultBlock('entries');
    const b = defaultBlock('entries');
    expect(a).not.toBe(b);
    expect(a.id).not.toBe(b.id);
    expect(a.conditions).not.toBe(b.conditions);
  });

  // Per CONTRACT §6.4 — per-block require-reset binding defaults to null
  // on entries+exits and is ABSENT on resets (a reset cannot gate itself).
  it('entry defaultBlock includes requires_reset_block_id: null', () => {
    const b = defaultBlock('entries');
    expect('requires_reset_block_id' in b).toBe(true);
    expect(b.requires_reset_block_id).toBe(null);
  });

  it('exit defaultBlock includes requires_reset_block_id: null', () => {
    const b = defaultBlock('exits');
    expect('requires_reset_block_id' in b).toBe(true);
    expect(b.requires_reset_block_id).toBe(null);
  });

  it('reset defaultBlock does NOT include requires_reset_block_id', () => {
    const b = defaultBlock('resets');
    expect('requires_reset_block_id' in b).toBe(false);
    expect('input_id' in b).toBe(false);
    expect('weight' in b).toBe(false);
    expect('target_entry_block_name' in b).toBe(false);
    expect('target_entry_block_names' in b).toBe(false);
  });

  // requires_reset_count — per-binding reset countdown. Lives on the
  // entry/exit block (mirrors requires_reset_block_id); default 1 (==
  // current single-fire re-arm). ABSENT on resets (a reset has no count).
  it('entry defaultBlock includes requires_reset_count: 1', () => {
    const b = defaultBlock('entries');
    expect('requires_reset_count' in b).toBe(true);
    expect(b.requires_reset_count).toBe(1);
  });

  it('exit defaultBlock includes requires_reset_count: 1', () => {
    const b = defaultBlock('exits');
    expect('requires_reset_count' in b).toBe(true);
    expect(b.requires_reset_count).toBe(1);
  });

  it('default-section (entries) defaultBlock includes requires_reset_count: 1', () => {
    const b = defaultBlock();
    expect(b.requires_reset_count).toBe(1);
  });

  it('reset defaultBlock does NOT include requires_reset_count', () => {
    const b = defaultBlock('resets');
    expect('requires_reset_count' in b).toBe(false);
  });
});

describe('isInputConfigured', () => {
  it('rejects non-objects and inputs with no id', () => {
    expect(isInputConfigured(null)).toBe(false);
    expect(isInputConfigured({})).toBe(false);
    expect(isInputConfigured({ id: '', instrument: SPOT_INPUT.instrument })).toBe(false);
  });

  it('accepts a fully-configured spot input', () => {
    expect(isInputConfigured(SPOT_INPUT)).toBe(true);
  });

  it('rejects a spot input with missing collection or instrument_id', () => {
    expect(isInputConfigured(SPOT_INPUT_BAD)).toBe(false);
    expect(isInputConfigured({
      id: 'X',
      instrument: { type: 'spot', collection: 'INDEX', instrument_id: '' },
    })).toBe(false);
  });

  it('accepts a fully-configured continuous input', () => {
    expect(isInputConfigured(CONT_INPUT)).toBe(true);
  });

  it('accepts a continuous input with null cycle (all months)', () => {
    expect(isInputConfigured({
      id: 'Z',
      instrument: {
        type: 'continuous', collection: 'FUT_SP_500', adjustment: 'none',
        cycle: null, rollOffset: 0, strategy: 'front_month',
      },
    })).toBe(true);
  });

  it('rejects a continuous input missing required fields', () => {
    expect(isInputConfigured({
      id: 'Z',
      instrument: {
        type: 'continuous', collection: '', adjustment: 'none',
        cycle: 'all', rollOffset: 2, strategy: 'front_month',
      },
    })).toBe(false);
    expect(isInputConfigured({
      id: 'Z',
      instrument: {
        type: 'continuous', collection: 'FUT_ES', adjustment: 'bogus',
        cycle: 'all', rollOffset: 2, strategy: 'front_month',
      },
    })).toBe(false);
  });

  it('accepts a fully-configured option_stream input', () => {
    expect(isInputConfigured(OPT_STREAM_INPUT)).toBe(true);
  });

  it('rejects an option_stream input missing collection', () => {
    expect(isInputConfigured({
      id: 'O',
      instrument: {
        type: 'option_stream', collection: '', option_type: 'C',
        cycle: 'W3_FRI',
        maturity: { kind: 'fixed', value: '2025-06-20' },
        selection: { kind: 'delta', value: 0.3 },
        stream: 'iv',
      },
    })).toBe(false);
  });

  it('rejects an option_stream input with invalid option_type', () => {
    expect(isInputConfigured({
      id: 'O',
      instrument: {
        type: 'option_stream', collection: 'OPT_SPX', option_type: 'X',
        cycle: 'W3_FRI',
        maturity: { kind: 'fixed', value: '2025-06-20' },
        selection: { kind: 'delta', value: 0.3 },
        stream: 'iv',
      },
    })).toBe(false);
  });

  it('rejects an option_stream input missing maturity', () => {
    expect(isInputConfigured({
      id: 'O',
      instrument: {
        type: 'option_stream', collection: 'OPT_SPX', option_type: 'C',
        cycle: 'W3_FRI',
        maturity: null,
        selection: { kind: 'delta', value: 0.3 },
        stream: 'iv',
      },
    })).toBe(false);
  });

  it('rejects an option_stream input missing selection', () => {
    expect(isInputConfigured({
      id: 'O',
      instrument: {
        type: 'option_stream', collection: 'OPT_SPX', option_type: 'C',
        cycle: 'W3_FRI',
        maturity: { kind: 'fixed', value: '2025-06-20' },
        selection: null,
        stream: 'iv',
      },
    })).toBe(false);
  });

  it('rejects an option_stream input with empty stream', () => {
    expect(isInputConfigured({
      id: 'O',
      instrument: {
        type: 'option_stream', collection: 'OPT_SPX', option_type: 'C',
        cycle: 'W3_FRI',
        maturity: { kind: 'fixed', value: '2025-06-20' },
        selection: { kind: 'delta', value: 0.3 },
        stream: '',
      },
    })).toBe(false);
  });

  it('accepts an option_stream input with null cycle', () => {
    expect(isInputConfigured({
      id: 'O',
      instrument: {
        type: 'option_stream', collection: 'OPT_SPX', option_type: 'P',
        cycle: null,
        maturity: { kind: 'dte', value: 30 },
        selection: { kind: 'strike', value: 5000 },
        stream: 'delta',
      },
    })).toBe(true);
  });

  // Basket — two-shape discriminated union (saved | inline).
  it('accepts a saved basket input with non-empty basket_id', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: { type: 'basket', kind: 'saved', basket_id: 'BSK_ABC' },
    })).toBe(true);
  });

  it('rejects a saved basket input with empty / missing basket_id', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: { type: 'basket', kind: 'saved', basket_id: '' },
    })).toBe(false);
    expect(isInputConfigured({
      id: 'B',
      instrument: { type: 'basket', kind: 'saved' },
    })).toBe(false);
  });

  it('accepts an inline future basket with >=1 continuous legs', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'future',
        legs: [
          {
            instrument: {
              type: 'continuous', collection: 'FUT_ES',
              adjustment: 'none', cycle: null, rollOffset: 0,
              strategy: 'front_month',
            },
            weight: 1.0,
          },
          {
            instrument: {
              type: 'continuous', collection: 'FUT_NQ',
              adjustment: 'ratio', cycle: 'M', rollOffset: 3,
              strategy: 'front_month',
            },
            weight: -0.5,
          },
        ],
      },
    })).toBe(true);
  });

  it('accepts an inline equity basket with a single spot leg', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'equity',
        legs: [{
          instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
          weight: 1.0,
        }],
      },
    })).toBe(true);
  });

  it('accepts an inline option basket with a single option_stream leg', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'option',
        legs: [{
          instrument: {
            type: 'option_stream', collection: 'OPT_SP_500',
            option_type: 'C', cycle: 'M',
            maturity: { kind: 'next_third_friday', offset_months: 0 },
            selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
            stream: 'mid',
          },
          weight: 1.0,
        }],
      },
    })).toBe(true);
  });

  it('rejects an inline basket input with 0 legs', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'future', legs: [],
      },
    })).toBe(false);
  });

  it('rejects an inline basket input with missing asset_class', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline',
        legs: [{
          instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
          weight: 1.0,
        }],
      },
    })).toBe(false);
  });

  it('rejects an inline basket with unknown asset_class', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'commodity',
        legs: [{
          instrument: { type: 'spot', collection: 'ETF', instrument_id: 'CL' },
          weight: 1.0,
        }],
      },
    })).toBe(false);
  });

  it('rejects an inline equity basket whose leg has empty instrument_id', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'equity',
        legs: [{
          instrument: { type: 'spot', collection: 'ETF', instrument_id: '' },
          weight: 1.0,
        }],
      },
    })).toBe(false);
  });

  it('rejects an inline future basket whose leg has empty collection', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'future',
        legs: [{
          instrument: {
            type: 'continuous', collection: '',
            adjustment: 'none', cycle: null, rollOffset: 0,
            strategy: 'front_month',
          },
          weight: 1.0,
        }],
      },
    })).toBe(false);
  });

  it('rejects an inline basket leg with zero weight', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'equity',
        legs: [{
          instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
          weight: 0,
        }],
      },
    })).toBe(false);
  });

  it('rejects an inline basket leg with NaN weight', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'equity',
        legs: [{
          instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
          weight: NaN,
        }],
      },
    })).toBe(false);
  });

  it('accepts an inline basket with negative leg weight (short)', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'future',
        legs: [{
          instrument: {
            type: 'continuous', collection: 'FUT_ES',
            adjustment: 'none', cycle: null, rollOffset: 0,
            strategy: 'front_month',
          },
          weight: -1.0,
        }],
      },
    })).toBe(true);
  });

  // Strict per-class mapping — the FE refuses to call a basket
  // configured when a leg's instrument.type doesn't match asset_class.
  // The BE rejects with 422 in the same case (BasketRefInline
  // _check_strict_per_class_mapping validator).
  it('rejects strict-mismatch: future asset_class with a spot leg', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'future',
        legs: [{
          instrument: { type: 'spot', collection: 'FUT_ES', instrument_id: 'ES_MAR26' },
          weight: 1.0,
        }],
      },
    })).toBe(false);
  });

  it('rejects strict-mismatch: equity asset_class with a continuous leg', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'equity',
        legs: [{
          instrument: {
            type: 'continuous', collection: 'FUT_ES',
            adjustment: 'none', cycle: null, rollOffset: 0,
            strategy: 'front_month',
          },
          weight: 1.0,
        }],
      },
    })).toBe(false);
  });

  it('rejects strict-mismatch: option asset_class with a spot leg', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: {
        type: 'basket', kind: 'inline', asset_class: 'option',
        legs: [{
          instrument: { type: 'spot', collection: 'OPT_SP_500', instrument_id: 'SPX' },
          weight: 1.0,
        }],
      },
    })).toBe(false);
  });

  it('rejects a basket input with unknown kind', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: { type: 'basket', kind: 'mystery', basket_id: 'X' },
    })).toBe(false);
  });

  it('rejects a basket input with missing kind discriminator', () => {
    expect(isInputConfigured({
      id: 'B',
      instrument: { type: 'basket', basket_id: 'X' },
    })).toBe(false);
  });
});

describe('isOperandComplete (v5)', () => {
  it('null / undefined / non-object → false', () => {
    expect(isOperandComplete(null, INPUTS_BY_ID)).toBe(false);
    expect(isOperandComplete(undefined, INPUTS_BY_ID)).toBe(false);
    expect(isOperandComplete(42, INPUTS_BY_ID)).toBe(false);
  });

  it('constant: finite value → true', () => {
    expect(isOperandComplete({ kind: 'constant', value: 0 }, INPUTS_BY_ID)).toBe(true);
    expect(isOperandComplete({ kind: 'constant', value: NaN }, INPUTS_BY_ID)).toBe(false);
    expect(isOperandComplete({ kind: 'constant' }, INPUTS_BY_ID)).toBe(false);
  });

  it('indicator: requires indicator_id + input_id that resolves', () => {
    expect(isOperandComplete(IND_OP_OK, INPUTS_BY_ID)).toBe(true);
    expect(isOperandComplete(IND_OP_NO_INPUT, INPUTS_BY_ID)).toBe(false);
    expect(isOperandComplete(IND_OP_UNKNOWN_INPUT, INPUTS_BY_ID)).toBe(false);
  });

  it('indicator series_override values must resolve to declared inputs', () => {
    const op = { ...IND_OP_OK, series_override: { secondary: 'Z' } };
    expect(isOperandComplete(op, INPUTS_BY_ID)).toBe(true);
    const bad = { ...IND_OP_OK, series_override: { secondary: 'NONE' } };
    expect(isOperandComplete(bad, INPUTS_BY_ID)).toBe(false);
  });

  it('instrument: requires input_id that resolves + a field', () => {
    expect(isOperandComplete(INST_OP_OK, INPUTS_BY_ID)).toBe(true);
    expect(isOperandComplete(INST_OP_NO_FIELD, INPUTS_BY_ID)).toBe(false);
    expect(isOperandComplete(INST_OP_UNKNOWN, INPUTS_BY_ID)).toBe(false);
  });

  it('unknown kind → false', () => {
    expect(isOperandComplete({ kind: 'wat', value: 1 }, INPUTS_BY_ID)).toBe(false);
    expect(isOperandComplete({}, INPUTS_BY_ID)).toBe(false);
  });
});

describe('isConditionComplete (v5)', () => {
  it('rejects bad inputs', () => {
    expect(isConditionComplete(null, INPUTS_BY_ID)).toBe(false);
    expect(isConditionComplete({}, INPUTS_BY_ID)).toBe(false);
    expect(isConditionComplete({ op: '' }, INPUTS_BY_ID)).toBe(false);
  });

  it('binary: both lhs + rhs must be complete', () => {
    expect(isConditionComplete({
      op: 'gt', lhs: CONST_OK, rhs: CONST_OK,
    }, INPUTS_BY_ID)).toBe(true);
    expect(isConditionComplete({
      op: 'gt', lhs: CONST_OK, rhs: null,
    }, INPUTS_BY_ID)).toBe(false);
  });

  it('range: operand + min + max all required', () => {
    expect(isConditionComplete({
      op: 'in_range', operand: CONST_OK, min: CONST_OK, max: CONST_OK,
    }, INPUTS_BY_ID)).toBe(true);
    expect(isConditionComplete({
      op: 'in_range', operand: CONST_OK, min: null, max: CONST_OK,
    }, INPUTS_BY_ID)).toBe(false);
  });

  it('rolling: only operand is an operand slot', () => {
    expect(isConditionComplete({
      op: 'rolling_gt', operand: CONST_OK, lookback: 5,
    }, INPUTS_BY_ID)).toBe(true);
    expect(isConditionComplete({
      op: 'rolling_gt', operand: null, lookback: 5,
    }, INPUTS_BY_ID)).toBe(false);
  });

  it('instrument operand without a field blocks completion', () => {
    expect(isConditionComplete({
      op: 'gt', lhs: INST_OP_NO_FIELD, rhs: CONST_OK,
    }, INPUTS_BY_ID)).toBe(false);
    expect(isConditionComplete({
      op: 'gt', lhs: INST_OP_OK, rhs: CONST_OK,
    }, INPUTS_BY_ID)).toBe(true);
  });
});

describe('indexInputs', () => {
  it('builds id -> input map', () => {
    const m = indexInputs(INPUTS);
    expect(m.X).toBe(SPOT_INPUT);
    expect(m.Z).toBe(CONT_INPUT);
    expect(m.NONE).toBe(undefined);
  });
  it('handles non-array input', () => {
    expect(indexInputs(null)).toEqual({});
    expect(indexInputs('nope')).toEqual({});
  });
  it('skips entries with no id', () => {
    const m = indexInputs([{ id: '', instrument: null }, SPOT_INPUT]);
    expect(m.X).toBe(SPOT_INPUT);
    expect(Object.keys(m)).toEqual(['X']);
  });
});

describe('collectEntryIds', () => {
  it('returns a Set of entry block ids', () => {
    const ids = collectEntryIds([{ id: 'a' }, { id: 'b' }]);
    expect(ids instanceof Set).toBe(true);
    expect([...ids].sort()).toEqual(['a', 'b']);
  });
  it('ignores blocks without an id', () => {
    const ids = collectEntryIds([{ id: '' }, null, { id: 'ok' }]);
    expect([...ids]).toEqual(['ok']);
  });
  it('returns empty set on non-array input', () => {
    expect([...collectEntryIds(null)]).toEqual([]);
  });
});

describe('isBlockRunnable (v5 — entries)', () => {
  const runnableCondition = { op: 'gt', lhs: CONST_OK, rhs: CONST_OK };
  const entryBlock = (over = {}) => ({
    id: 'e1', input_id: 'X', weight: 40, conditions: [runnableCondition], ...over,
  });

  it('happy path: valid entry with positive weight → true', () => {
    expect(isBlockRunnable(entryBlock(), 'entries', INPUTS)).toBe(true);
  });

  it('happy path: valid entry with negative weight → true (shorts)', () => {
    expect(isBlockRunnable(entryBlock({ weight: -25 }), 'entries', INPUTS)).toBe(true);
  });

  it('rejects bad block shapes', () => {
    expect(isBlockRunnable(null, 'entries', INPUTS)).toBe(false);
    expect(isBlockRunnable({}, 'entries', INPUTS)).toBe(false);
  });

  it('empty input_id → false', () => {
    expect(isBlockRunnable(entryBlock({ input_id: '' }), 'entries', INPUTS)).toBe(false);
  });

  it('unknown input_id → false', () => {
    expect(isBlockRunnable(entryBlock({ input_id: 'NOPE' }), 'entries', INPUTS)).toBe(false);
  });

  it('block referencing an unconfigured input → false', () => {
    const inputsWithBad = [SPOT_INPUT_BAD];
    expect(isBlockRunnable(entryBlock({ input_id: 'Y' }), 'entries', inputsWithBad)).toBe(false);
  });

  it('zero conditions → false', () => {
    expect(isBlockRunnable(entryBlock({ conditions: [] }), 'entries', INPUTS)).toBe(false);
  });

  it('any incomplete condition drags the block down', () => {
    const bad = { op: 'gt', lhs: CONST_OK, rhs: null };
    expect(isBlockRunnable(
      entryBlock({ conditions: [runnableCondition, bad] }),
      'entries',
      INPUTS,
    )).toBe(false);
  });

  it('indicator operand bound to unknown input → false', () => {
    const cond = { op: 'gt', lhs: IND_OP_UNKNOWN_INPUT, rhs: CONST_OK };
    expect(isBlockRunnable(entryBlock({ conditions: [cond] }), 'entries', INPUTS)).toBe(false);
  });

  it('indicator operand bound to known input → true', () => {
    const cond = { op: 'gt', lhs: IND_OP_OK, rhs: CONST_OK };
    expect(isBlockRunnable(entryBlock({ conditions: [cond] }), 'entries', INPUTS)).toBe(true);
  });

  describe('signed-weight gate', () => {
    it('entry + weight=0 → false (zero weight ambiguous)', () => {
      expect(isBlockRunnable(entryBlock({ weight: 0 }), 'entries', INPUTS)).toBe(false);
    });
    it('entry + NaN weight → false', () => {
      expect(isBlockRunnable(entryBlock({ weight: NaN }), 'entries', INPUTS)).toBe(false);
    });
    it('entry + |weight| > 100 → false (no leverage)', () => {
      expect(isBlockRunnable(entryBlock({ weight: 150 }), 'entries', INPUTS)).toBe(false);
      expect(isBlockRunnable(entryBlock({ weight: -150 }), 'entries', INPUTS)).toBe(false);
    });
    it('entry + |weight| = 100 → true (boundary inclusive)', () => {
      expect(isBlockRunnable(entryBlock({ weight: 100 }), 'entries', INPUTS)).toBe(true);
      expect(isBlockRunnable(entryBlock({ weight: -100 }), 'entries', INPUTS)).toBe(true);
    });
  });
});

describe('isBlockRunnable (v6 — exits, plural targets)', () => {
  const runnableCondition = { op: 'gt', lhs: CONST_OK, rhs: CONST_OK };
  const exitBlock = (over = {}) => ({
    id: 'x1',
    conditions: [runnableCondition],
    target_entry_block_names: ['Alpha'],
    ...over,
  });
  // Entry blocks array with names for name-based resolution
  const entryBlocks = [{ id: 'entry-1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] }];

  it('happy path: exit with one valid target name → true (weight ignored)', () => {
    expect(isBlockRunnable(exitBlock(), 'exits', INPUTS, entryBlocks)).toBe(true);
  });

  it('empty target array → false', () => {
    expect(isBlockRunnable(exitBlock({ target_entry_block_names: [] }), 'exits', INPUTS, entryBlocks)).toBe(false);
  });

  it('missing target array (undefined) → false', () => {
    expect(isBlockRunnable(exitBlock({ target_entry_block_names: undefined }), 'exits', INPUTS, entryBlocks)).toBe(false);
  });

  it('a target that does not match any entry name → false', () => {
    expect(isBlockRunnable(exitBlock({ target_entry_block_names: ['orphan'] }), 'exits', INPUTS, entryBlocks)).toBe(false);
  });

  it('exit + weight=0 → true (exit blocks don\'t participate in position sizing)', () => {
    expect(isBlockRunnable(exitBlock({ weight: 0 }), 'exits', INPUTS, entryBlocks)).toBe(true);
  });

  it('exit + nonzero weight → still runnable (weight unused on exits)', () => {
    expect(isBlockRunnable(exitBlock({ weight: 999 }), 'exits', INPUTS, entryBlocks)).toBe(true);
  });

  // When callers pass an array of entry Block objects (rich check), the
  // exit's runnability additionally requires every target entry's input_id
  // to resolve to a configured input — exits inherit that input.
  it('rich check: target entry has no input_id → false', () => {
    const blocks = [{ id: 'entry-1', name: 'Alpha', input_id: '', weight: 10, conditions: [] }];
    expect(isBlockRunnable(exitBlock(), 'exits', INPUTS, blocks)).toBe(false);
  });

  it('rich check: target entry has unknown input_id → false', () => {
    const blocks = [{ id: 'entry-1', name: 'Alpha', input_id: 'NOPE', weight: 10, conditions: [] }];
    expect(isBlockRunnable(exitBlock(), 'exits', INPUTS, blocks)).toBe(false);
  });

  it('rich check: target entry has configured input → true', () => {
    const blocks = [{ id: 'entry-1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] }];
    expect(isBlockRunnable(exitBlock(), 'exits', INPUTS, blocks)).toBe(true);
  });

  it('ambiguous: two entries share a targeted name → false', () => {
    const blocks = [
      { id: 'entry-1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] },
      { id: 'entry-2', name: 'Alpha', input_id: 'X', weight: 20, conditions: [] },
    ];
    expect(isBlockRunnable(exitBlock(), 'exits', INPUTS, blocks)).toBe(false);
  });

  // --- Multi-target (v6) ---
  it('two valid targets, both configured (cross-input allowed) → true', () => {
    const blocks = [
      { id: 'e1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] },
      { id: 'e2', name: 'Beta', input_id: 'Y', weight: -5, conditions: [] },
    ];
    const ins = [...INPUTS, { id: 'Y', instrument: INPUTS[0].instrument }];
    expect(isBlockRunnable(
      exitBlock({ target_entry_block_names: ['Alpha', 'Beta'] }),
      'exits', ins, blocks,
    )).toBe(true);
  });

  it('one valid + one dangling target → false (every target must resolve)', () => {
    const blocks = [{ id: 'e1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] }];
    expect(isBlockRunnable(
      exitBlock({ target_entry_block_names: ['Alpha', 'ghost'] }),
      'exits', INPUTS, blocks,
    )).toBe(false);
  });

  it('one valid + one targeting an entry with no input → false', () => {
    const blocks = [
      { id: 'e1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] },
      { id: 'e2', name: 'Beta', input_id: '', weight: 5, conditions: [] },
    ];
    expect(isBlockRunnable(
      exitBlock({ target_entry_block_names: ['Alpha', 'Beta'] }),
      'exits', INPUTS, blocks,
    )).toBe(false);
  });

  it('a blank string inside the target array → false', () => {
    const blocks = [{ id: 'e1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] }];
    expect(isBlockRunnable(
      exitBlock({ target_entry_block_names: ['Alpha', ''] }),
      'exits', INPUTS, blocks,
    )).toBe(false);
  });
});

describe('defaultBlock — fire_mode (v8, per-block pulse|sustained)', () => {
  it('stamps fire_mode:"pulse" on NEW entry blocks (pulse-by-default UX)', () => {
    expect(defaultBlock('entries').fire_mode).toBe('pulse');
  });
  it('stamps fire_mode:"pulse" on NEW exit blocks', () => {
    expect(defaultBlock('exits').fire_mode).toBe('pulse');
  });
  it('reset blocks carry NO fire_mode (backend rejects it there)', () => {
    expect('fire_mode' in defaultBlock('resets')).toBe(false);
  });
});
