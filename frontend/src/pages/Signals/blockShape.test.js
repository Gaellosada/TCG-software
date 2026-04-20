import { describe, it, expect } from 'vitest';
import {
  defaultBlock,
  isOperandComplete,
  isConditionComplete,
  isBlockRunnable,
} from './blockShape';

const INST = { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' };
const INST_NO_FIELD = { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC' };
const CONST_OK = { kind: 'constant', value: 1 };
const IND_OK = { kind: 'indicator', indicator_id: 'sma', output: 'default' };
const IND_EMPTY = { kind: 'indicator', indicator_id: '', output: null };
const IND_NULL_ID = { kind: 'indicator', indicator_id: null, output: null };

describe('defaultBlock', () => {
  it('returns {instrument: null, weight: 0, conditions: []}', () => {
    expect(defaultBlock()).toEqual({ instrument: null, weight: 0, conditions: [] });
  });
  it('returns a fresh object each call (no shared references)', () => {
    const a = defaultBlock();
    const b = defaultBlock();
    expect(a).not.toBe(b);
    expect(a.conditions).not.toBe(b.conditions);
  });
});

describe('isOperandComplete', () => {
  it('null / undefined / non-object → false', () => {
    expect(isOperandComplete(null)).toBe(false);
    expect(isOperandComplete(undefined)).toBe(false);
    expect(isOperandComplete('string')).toBe(false);
    expect(isOperandComplete(42)).toBe(false);
  });

  it('constant: finite value → true; NaN / Infinity / missing → false', () => {
    expect(isOperandComplete({ kind: 'constant', value: 0 })).toBe(true);
    expect(isOperandComplete({ kind: 'constant', value: -3.2 })).toBe(true);
    expect(isOperandComplete({ kind: 'constant', value: NaN })).toBe(false);
    expect(isOperandComplete({ kind: 'constant', value: Infinity })).toBe(false);
    expect(isOperandComplete({ kind: 'constant' })).toBe(false);
  });

  it('indicator: non-empty id → true; empty / null id → false', () => {
    expect(isOperandComplete(IND_OK)).toBe(true);
    expect(isOperandComplete(IND_EMPTY)).toBe(false);
    expect(isOperandComplete(IND_NULL_ID)).toBe(false);
  });

  it('instrument requires collection + instrument_id + field', () => {
    expect(isOperandComplete(INST)).toBe(true);
    expect(isOperandComplete(INST_NO_FIELD)).toBe(false);
    expect(isOperandComplete({ kind: 'instrument', collection: '', instrument_id: '^GSPC', field: 'close' })).toBe(false);
    expect(isOperandComplete({ kind: 'instrument', collection: 'INDEX', instrument_id: '', field: 'close' })).toBe(false);
    expect(isOperandComplete({ kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: '' })).toBe(false);
  });

  it('unknown kind → false', () => {
    expect(isOperandComplete({ kind: 'wat', value: 1 })).toBe(false);
    expect(isOperandComplete({})).toBe(false);
  });
});

describe('isConditionComplete', () => {
  it('rejects bad inputs', () => {
    expect(isConditionComplete(null)).toBe(false);
    expect(isConditionComplete({})).toBe(false);
    expect(isConditionComplete({ op: '' })).toBe(false);
    expect(isConditionComplete({ op: 42 })).toBe(false);
  });

  it('binary: both lhs + rhs must be complete', () => {
    expect(isConditionComplete({ op: 'gt', lhs: CONST_OK, rhs: CONST_OK })).toBe(true);
    expect(isConditionComplete({ op: 'gt', lhs: CONST_OK, rhs: null })).toBe(false);
    expect(isConditionComplete({ op: 'gt', lhs: null, rhs: CONST_OK })).toBe(false);
  });

  it('range: operand + min + max all required', () => {
    expect(isConditionComplete({
      op: 'in_range', operand: CONST_OK, min: CONST_OK, max: CONST_OK,
    })).toBe(true);
    expect(isConditionComplete({
      op: 'in_range', operand: CONST_OK, min: null, max: CONST_OK,
    })).toBe(false);
    expect(isConditionComplete({
      op: 'in_range', operand: CONST_OK, min: CONST_OK, max: null,
    })).toBe(false);
  });

  it('rolling: only operand is an operand slot (lookback is structural)', () => {
    expect(isConditionComplete({
      op: 'rolling_gt', operand: CONST_OK, lookback: 5,
    })).toBe(true);
    expect(isConditionComplete({
      op: 'rolling_gt', operand: null, lookback: 5,
    })).toBe(false);
  });

  it('instrument operand without a ``field`` blocks completion (stricter than iter-2)', () => {
    expect(isConditionComplete({ op: 'gt', lhs: INST_NO_FIELD, rhs: CONST_OK })).toBe(false);
    expect(isConditionComplete({ op: 'gt', lhs: INST, rhs: CONST_OK })).toBe(true);
  });
});

describe('isBlockRunnable', () => {
  const runnableCondition = { op: 'gt', lhs: CONST_OK, rhs: CONST_OK };
  const block = (over = {}) => ({
    instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
    weight: 0.4,
    conditions: [runnableCondition],
    ...over,
  });

  it('happy path: instrument set + ≥1 complete condition → true', () => {
    expect(isBlockRunnable(block())).toBe(true);
  });

  it('rejects bad input shapes', () => {
    expect(isBlockRunnable(null)).toBe(false);
    expect(isBlockRunnable(undefined)).toBe(false);
    expect(isBlockRunnable('nope')).toBe(false);
    expect(isBlockRunnable(42)).toBe(false);
  });

  it('missing instrument → false', () => {
    expect(isBlockRunnable(block({ instrument: null }))).toBe(false);
  });

  it('instrument with empty collection or instrument_id → false', () => {
    expect(isBlockRunnable(block({ instrument: { collection: '', instrument_id: '^GSPC' } }))).toBe(false);
    expect(isBlockRunnable(block({ instrument: { collection: 'INDEX', instrument_id: '' } }))).toBe(false);
  });

  it('zero conditions → false even if instrument is set', () => {
    expect(isBlockRunnable(block({ conditions: [] }))).toBe(false);
  });

  it('non-array conditions → false', () => {
    expect(isBlockRunnable(block({ conditions: null }))).toBe(false);
    expect(isBlockRunnable(block({ conditions: 'nope' }))).toBe(false);
  });

  it('any incomplete condition drags the block down', () => {
    const bad = { op: 'gt', lhs: CONST_OK, rhs: null };
    expect(isBlockRunnable(block({ conditions: [runnableCondition, bad] }))).toBe(false);
  });

  it('indicator operand without indicator_id blocks run', () => {
    const cond = { op: 'gt', lhs: IND_NULL_ID, rhs: CONST_OK };
    expect(isBlockRunnable(block({ conditions: [cond] }))).toBe(false);
  });

  it('indicator operand with indicator_id set allows run', () => {
    const cond = { op: 'gt', lhs: IND_OK, rhs: CONST_OK };
    expect(isBlockRunnable(block({ conditions: [cond] }))).toBe(true);
  });

  it('weight does not gate runnability — zero weight is fine', () => {
    expect(isBlockRunnable(block({ weight: 0 }))).toBe(true);
  });

  it('runnability matrix — all kinds as lhs/rhs in a single block', () => {
    // Each row: (lhs, rhs, expected runnable)
    const cases = [
      [CONST_OK, CONST_OK, true],
      [IND_OK, CONST_OK, true],
      [INST, CONST_OK, true],
      [INST_NO_FIELD, CONST_OK, false],  // stricter: instrument requires field
      [IND_NULL_ID, CONST_OK, false],
      [IND_EMPTY, CONST_OK, false],
      [null, CONST_OK, false],
    ];
    for (const [lhs, rhs, want] of cases) {
      const cond = { op: 'gt', lhs, rhs };
      expect(isBlockRunnable(block({ conditions: [cond] }))).toBe(want);
    }
  });
});
