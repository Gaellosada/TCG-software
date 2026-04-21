import { describe, it, expect } from 'vitest';
import {
  defaultBlock,
  isInputConfigured,
  isOperandComplete,
  isConditionComplete,
  indexInputs,
  isBlockRunnable,
} from './blockShape';

// Inputs fixtures (v3).
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

// Operands (v3).
const INST_OP_OK = { kind: 'instrument', input_id: 'X', field: 'close' };
const INST_OP_NO_FIELD = { kind: 'instrument', input_id: 'X' };
const INST_OP_UNKNOWN = { kind: 'instrument', input_id: 'NONE', field: 'close' };
const CONST_OK = { kind: 'constant', value: 1 };
const IND_OP_OK = { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' };
const IND_OP_NO_INPUT = { kind: 'indicator', indicator_id: 'sma', input_id: '', output: null };
const IND_OP_UNKNOWN_INPUT = { kind: 'indicator', indicator_id: 'sma', input_id: 'NONE', output: null };

describe('defaultBlock (v3)', () => {
  it('returns {input_id: "", weight: 0, conditions: []}', () => {
    expect(defaultBlock()).toEqual({ input_id: '', weight: 0, conditions: [] });
  });
  it('returns a fresh object each call (no shared references)', () => {
    const a = defaultBlock();
    const b = defaultBlock();
    expect(a).not.toBe(b);
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

describe('isOperandComplete (v3)', () => {
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

describe('isConditionComplete (v3)', () => {
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

describe('isBlockRunnable (v3)', () => {
  const runnableCondition = { op: 'gt', lhs: CONST_OK, rhs: CONST_OK };
  const block = (over = {}) => ({
    input_id: 'X',
    weight: 0.4,
    conditions: [runnableCondition],
    ...over,
  });

  it('happy path: valid input_id + ≥1 complete condition → true', () => {
    expect(isBlockRunnable(block(), 'long_exit', INPUTS)).toBe(true);
  });

  it('rejects bad block shapes', () => {
    expect(isBlockRunnable(null, 'long_exit', INPUTS)).toBe(false);
    expect(isBlockRunnable({}, 'long_exit', INPUTS)).toBe(false);
  });

  it('empty input_id → false', () => {
    expect(isBlockRunnable(block({ input_id: '' }), 'long_exit', INPUTS)).toBe(false);
  });

  it('unknown input_id → false', () => {
    expect(isBlockRunnable(block({ input_id: 'NOPE' }), 'long_exit', INPUTS)).toBe(false);
  });

  it('block referencing an unconfigured input → false', () => {
    const inputsWithBad = [SPOT_INPUT_BAD];
    expect(isBlockRunnable(block({ input_id: 'Y' }), 'long_exit', inputsWithBad)).toBe(false);
  });

  it('zero conditions → false', () => {
    expect(isBlockRunnable(block({ conditions: [] }), 'long_exit', INPUTS)).toBe(false);
  });

  it('any incomplete condition drags the block down', () => {
    const bad = { op: 'gt', lhs: CONST_OK, rhs: null };
    expect(isBlockRunnable(
      block({ conditions: [runnableCondition, bad] }),
      'long_exit',
      INPUTS,
    )).toBe(false);
  });

  it('indicator operand bound to unknown input → false', () => {
    const cond = { op: 'gt', lhs: IND_OP_UNKNOWN_INPUT, rhs: CONST_OK };
    expect(isBlockRunnable(block({ conditions: [cond] }), 'long_exit', INPUTS)).toBe(false);
  });

  it('indicator operand bound to known input → true', () => {
    const cond = { op: 'gt', lhs: IND_OP_OK, rhs: CONST_OK };
    expect(isBlockRunnable(block({ conditions: [cond] }), 'long_exit', INPUTS)).toBe(true);
  });

  describe('direction-aware weight gate (PROB-2)', () => {
    it('long_entry + weight=0 → false', () => {
      expect(isBlockRunnable(block({ weight: 0 }), 'long_entry', INPUTS)).toBe(false);
    });
    it('short_entry + weight=0 → false', () => {
      expect(isBlockRunnable(block({ weight: 0 }), 'short_entry', INPUTS)).toBe(false);
    });
    it('long_entry + weight>0 → true', () => {
      expect(isBlockRunnable(block({ weight: 0.5 }), 'long_entry', INPUTS)).toBe(true);
    });
    it('long_exit + weight=0 → true (exit ignores weight)', () => {
      expect(isBlockRunnable(block({ weight: 0 }), 'long_exit', INPUTS)).toBe(true);
    });
    it('short_exit + weight=0 → true', () => {
      expect(isBlockRunnable(block({ weight: 0 }), 'short_exit', INPUTS)).toBe(true);
    });
    it('entry + NaN weight → false', () => {
      expect(isBlockRunnable(block({ weight: NaN }), 'long_entry', INPUTS)).toBe(false);
    });
  });
});
