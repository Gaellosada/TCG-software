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

// Inputs fixtures (v4 — unchanged from v3).
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
const INPUTS = [SPOT_INPUT, CONT_INPUT];
const INPUTS_BY_ID = indexInputs(INPUTS);

// Operands (v4 — unchanged from v3).
const INST_OP_OK = { kind: 'instrument', input_id: 'X', field: 'close' };
const INST_OP_NO_FIELD = { kind: 'instrument', input_id: 'X' };
const INST_OP_UNKNOWN = { kind: 'instrument', input_id: 'NONE', field: 'close' };
const CONST_OK = { kind: 'constant', value: 1 };
const IND_OP_OK = { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' };
const IND_OP_NO_INPUT = { kind: 'indicator', indicator_id: 'sma', input_id: '', output: null };
const IND_OP_UNKNOWN_INPUT = { kind: 'indicator', indicator_id: 'sma', input_id: 'NONE', output: null };

describe('defaultBlock (v4)', () => {
  it('entry block defaults: id, input_id: "", weight: 0, conditions: []', () => {
    const b = defaultBlock('entries');
    expect(typeof b.id).toBe('string');
    expect(b.id.length).toBeGreaterThan(0);
    expect(b.input_id).toBe('');
    expect(b.weight).toBe(0);
    expect(b.conditions).toEqual([]);
    expect('target_entry_block_id' in b).toBe(false);
  });

  it('exit block adds target_entry_block_id: ""', () => {
    const b = defaultBlock('exits');
    expect(b.target_entry_block_id).toBe('');
  });

  it('defaults to entries when no section given', () => {
    const b = defaultBlock();
    expect('target_entry_block_id' in b).toBe(false);
  });

  it('returns a fresh object each call with a distinct id', () => {
    const a = defaultBlock('entries');
    const b = defaultBlock('entries');
    expect(a).not.toBe(b);
    expect(a.id).not.toBe(b.id);
    expect(a.conditions).not.toBe(b.conditions);
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
});

describe('isOperandComplete (v4)', () => {
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

describe('isConditionComplete (v4)', () => {
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

describe('isBlockRunnable (v4 — entries)', () => {
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

describe('isBlockRunnable (v4 — exits)', () => {
  const runnableCondition = { op: 'gt', lhs: CONST_OK, rhs: CONST_OK };
  const exitBlock = (over = {}) => ({
    id: 'x1',
    input_id: 'X',
    weight: 0,
    conditions: [runnableCondition],
    target_entry_block_id: 'entry-1',
    ...over,
  });
  const entryIds = new Set(['entry-1']);

  it('happy path: exit with valid target → true (weight ignored)', () => {
    expect(isBlockRunnable(exitBlock(), 'exits', INPUTS, entryIds)).toBe(true);
  });

  it('missing target_entry_block_id → false', () => {
    expect(isBlockRunnable(exitBlock({ target_entry_block_id: '' }), 'exits', INPUTS, entryIds)).toBe(false);
  });

  it('target does not match any entry → false', () => {
    expect(isBlockRunnable(exitBlock({ target_entry_block_id: 'orphan' }), 'exits', INPUTS, entryIds)).toBe(false);
  });

  it('accepts entryIds as a plain array (tolerant signature)', () => {
    expect(isBlockRunnable(exitBlock(), 'exits', INPUTS, ['entry-1'])).toBe(true);
  });

  it('exit + weight=0 → true (exit blocks don\'t participate in position sizing)', () => {
    expect(isBlockRunnable(exitBlock({ weight: 0 }), 'exits', INPUTS, entryIds)).toBe(true);
  });

  it('exit + nonzero weight → still runnable (weight unused on exits)', () => {
    expect(isBlockRunnable(exitBlock({ weight: 999 }), 'exits', INPUTS, entryIds)).toBe(true);
  });
});
