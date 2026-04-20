import { describe, it, expect } from 'vitest';
import {
  conditionShape,
  defaultCondition,
  defaultIndicatorOperand,
  isOperandComplete,
  isConditionComplete,
  operandSlots,
  migrateCondition,
} from './conditionOps';

describe('conditionShape', () => {
  it.each(['gt', 'lt', 'ge', 'le', 'eq'])('maps %s → binary', (op) => {
    expect(conditionShape(op)).toBe('binary');
  });
  it.each(['cross_above', 'cross_below'])('maps %s → binary', (op) => {
    expect(conditionShape(op)).toBe('binary');
  });
  it('maps in_range → range', () => {
    expect(conditionShape('in_range')).toBe('range');
  });
  it.each(['rolling_gt', 'rolling_lt'])('maps %s → rolling', (op) => {
    expect(conditionShape(op)).toBe('rolling');
  });
});

describe('defaultCondition — iter-2: no default operand injection', () => {
  it('binary op returns null lhs / rhs', () => {
    const c = defaultCondition('gt');
    expect(c).toEqual({ op: 'gt', lhs: null, rhs: null });
  });
  it('range op returns null operand / min / max', () => {
    const c = defaultCondition('in_range');
    expect(c).toEqual({ op: 'in_range', operand: null, min: null, max: null });
  });
  it('rolling op returns null operand and integer lookback', () => {
    const c = defaultCondition('rolling_gt');
    expect(c).toEqual({ op: 'rolling_gt', operand: null, lookback: 1 });
  });
  it('defaults to gt when op is omitted', () => {
    expect(defaultCondition().op).toBe('gt');
  });
});

describe('operandSlots', () => {
  it('binary → [lhs, rhs]', () => {
    expect(operandSlots('gt')).toEqual(['lhs', 'rhs']);
  });
  it('range → [operand, min, max]', () => {
    expect(operandSlots('in_range')).toEqual(['operand', 'min', 'max']);
  });
  it('rolling → [operand]', () => {
    expect(operandSlots('rolling_gt')).toEqual(['operand']);
  });
});

describe('isOperandComplete', () => {
  it('null → false', () => {
    expect(isOperandComplete(null)).toBe(false);
  });
  it('constant with finite value → true', () => {
    expect(isOperandComplete({ kind: 'constant', value: 0 })).toBe(true);
    expect(isOperandComplete({ kind: 'constant', value: -3.2 })).toBe(true);
  });
  it('constant with NaN value → false', () => {
    expect(isOperandComplete({ kind: 'constant', value: NaN })).toBe(false);
  });
  it('indicator with empty id → false', () => {
    expect(isOperandComplete({ kind: 'indicator', indicator_id: '' })).toBe(false);
  });
  it('indicator with id → true', () => {
    expect(isOperandComplete({ kind: 'indicator', indicator_id: 'sma' })).toBe(true);
  });
  it('instrument with missing fields → false', () => {
    expect(isOperandComplete({ kind: 'instrument', collection: '', instrument_id: '' })).toBe(false);
    expect(isOperandComplete({ kind: 'instrument', collection: 'INDEX', instrument_id: '' })).toBe(false);
  });
  it('instrument with both fields → true', () => {
    expect(isOperandComplete({ kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' })).toBe(true);
  });
  it('unknown kind → false', () => {
    expect(isOperandComplete({ kind: 'wat' })).toBe(false);
  });
});

describe('isConditionComplete', () => {
  it('default (all-null) condition → false', () => {
    expect(isConditionComplete(defaultCondition('gt'))).toBe(false);
    expect(isConditionComplete(defaultCondition('in_range'))).toBe(false);
    expect(isConditionComplete(defaultCondition('rolling_gt'))).toBe(false);
  });
  it('binary with two constants → true', () => {
    expect(isConditionComplete({
      op: 'gt',
      lhs: { kind: 'constant', value: 1 },
      rhs: { kind: 'constant', value: 2 },
    })).toBe(true);
  });
  it('binary with one null → false', () => {
    expect(isConditionComplete({
      op: 'gt',
      lhs: { kind: 'constant', value: 1 },
      rhs: null,
    })).toBe(false);
  });
  it('range with 3 complete operands → true', () => {
    expect(isConditionComplete({
      op: 'in_range',
      operand: { kind: 'constant', value: 1 },
      min: { kind: 'constant', value: 0 },
      max: { kind: 'constant', value: 2 },
    })).toBe(true);
  });
  it('rolling with complete operand → true', () => {
    expect(isConditionComplete({
      op: 'rolling_gt',
      operand: { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC' },
      lookback: 5,
    })).toBe(true);
  });
});

describe('defaultIndicatorOperand — iter-3 override fields', () => {
  it('returns an all-null shape with explicit params_override / series_override keys', () => {
    expect(defaultIndicatorOperand()).toEqual({
      kind: 'indicator',
      indicator_id: null,
      output: null,
      params_override: null,
      series_override: null,
    });
  });
  it('keys are own-enumerable (so JSON.stringify emits nulls, not undefineds)', () => {
    const op = defaultIndicatorOperand();
    // If these keys weren't present, they'd be missing from the JSON entirely.
    const encoded = JSON.parse(JSON.stringify(op));
    expect('params_override' in encoded).toBe(true);
    expect('series_override' in encoded).toBe(true);
    expect(encoded.params_override).toBe(null);
    expect(encoded.series_override).toBe(null);
  });
  it('defaultIndicatorOperand is not complete (iter-2 no-defaults policy)', () => {
    expect(isOperandComplete(defaultIndicatorOperand())).toBe(false);
  });
});

describe('migrateCondition — preserves compatible slots, leaves remainder unset', () => {
  it('same shape preserves operands, only op changes', () => {
    const current = {
      op: 'gt',
      lhs: { kind: 'constant', value: 1 },
      rhs: { kind: 'indicator', indicator_id: 'sma', output: 'default' },
    };
    const next = migrateCondition(current, 'lt');
    expect(next).toEqual({ ...current, op: 'lt' });
  });
  it('binary → range preserves lhs into operand', () => {
    const current = {
      op: 'gt',
      lhs: { kind: 'indicator', indicator_id: 'rsi', output: 'default' },
      rhs: { kind: 'constant', value: 30 },
    };
    const next = migrateCondition(current, 'in_range');
    expect(next.op).toBe('in_range');
    expect(next.operand).toEqual(current.lhs);
    // rhs maps to max per the migrate rule.
    expect(next.max).toEqual(current.rhs);
    expect(next.min).toBeNull();
  });
  it('range → rolling preserves operand and defaults lookback to 1', () => {
    const current = {
      op: 'in_range',
      operand: { kind: 'indicator', indicator_id: 'x', output: 'default' },
      min: { kind: 'constant', value: 0 },
      max: { kind: 'constant', value: 10 },
    };
    const next = migrateCondition(current, 'rolling_gt');
    expect(next).toEqual({
      op: 'rolling_gt',
      operand: current.operand,
      lookback: 1,
    });
  });
  it('null current → default (all-null) condition', () => {
    const next = migrateCondition(null, 'gt');
    expect(next).toEqual({ op: 'gt', lhs: null, rhs: null });
  });

  it('preserves indicator override fields when operator stays in the same shape', () => {
    const current = {
      op: 'gt',
      lhs: {
        kind: 'indicator', indicator_id: 'sma', output: 'default',
        params_override: { window: 50 }, series_override: { close: 'close' },
      },
      rhs: { kind: 'constant', value: 0 },
    };
    const next = migrateCondition(current, 'lt');
    expect(next.lhs).toEqual(current.lhs);
    expect(next.lhs.params_override).toEqual({ window: 50 });
    expect(next.lhs.series_override).toEqual({ close: 'close' });
  });

  it('preserves indicator override fields when migrating binary → range (lhs → operand)', () => {
    const current = {
      op: 'gt',
      lhs: {
        kind: 'indicator', indicator_id: 'rsi', output: 'default',
        params_override: { window: 7 }, series_override: null,
      },
      rhs: { kind: 'constant', value: 30 },
    };
    const next = migrateCondition(current, 'in_range');
    expect(next.operand).toEqual(current.lhs);
    expect(next.operand.params_override).toEqual({ window: 7 });
  });

  it('preserves indicator override fields when migrating range → rolling (operand → operand)', () => {
    const current = {
      op: 'in_range',
      operand: {
        kind: 'indicator', indicator_id: 'x', output: 'default',
        params_override: { p: 1 }, series_override: { s: 't' },
      },
      min: { kind: 'constant', value: 0 },
      max: { kind: 'constant', value: 10 },
    };
    const next = migrateCondition(current, 'rolling_gt');
    expect(next.operand).toEqual(current.operand);
    expect(next.operand.params_override).toEqual({ p: 1 });
    expect(next.operand.series_override).toEqual({ s: 't' });
  });
});
