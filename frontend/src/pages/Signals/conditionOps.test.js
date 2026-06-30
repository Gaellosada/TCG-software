import { describe, it, expect } from 'vitest';
import {
  ALL_OPS,
  ROLLING_OPS,
  CROSS_OPS,
  isLegacyOp,
  conditionShape,
  defaultCondition,
  defaultIndicatorOperand,
  defaultInstrumentOperand,
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

describe('defaultCondition — no default operand injection', () => {
  it('binary op returns null lhs / rhs', () => {
    const c = defaultCondition('gt');
    expect(c).toEqual({ op: 'gt', lhs: null, rhs: null });
  });
  it('range op returns null operand / min / max', () => {
    const c = defaultCondition('in_range');
    expect(c).toEqual({ op: 'in_range', operand: null, min: null, max: null });
  });
  it('rolling op (still constructible for legacy) returns null operand + lookback', () => {
    const c = defaultCondition('rolling_gt');
    expect(c).toEqual({ op: 'rolling_gt', operand: null, lookback: 1 });
  });
  it('cross op returns null lhs / rhs PLUS count/window defaulted to 1', () => {
    expect(defaultCondition('cross_above')).toEqual({
      op: 'cross_above', lhs: null, rhs: null, count: 1, window: 1,
    });
    expect(defaultCondition('cross_below')).toEqual({
      op: 'cross_below', lhs: null, rhs: null, count: 1, window: 1,
    });
  });
  it('defaults to gt when op is omitted', () => {
    expect(defaultCondition().op).toBe('gt');
  });
});

describe('ALL_OPS — rolling retired from authoring', () => {
  it('does NOT list rolling ops (no new rolling condition can be authored)', () => {
    expect(ALL_OPS).not.toContain('rolling_gt');
    expect(ALL_OPS).not.toContain('rolling_lt');
  });
  it('still lists comparators, cross and range ops', () => {
    expect(ALL_OPS).toContain('gt');
    expect(ALL_OPS).toContain('cross_above');
    expect(ALL_OPS).toContain('cross_below');
    expect(ALL_OPS).toContain('in_range');
  });
});

describe('isLegacyOp — retired operators', () => {
  it.each(ROLLING_OPS)('%s is legacy (retired but evaluable)', (op) => {
    expect(isLegacyOp(op)).toBe(true);
  });
  it.each(['gt', 'lt', 'cross_above', 'cross_below', 'in_range'])('%s is NOT legacy', (op) => {
    expect(isLegacyOp(op)).toBe(false);
  });
  it('rolling still maps to the rolling shape (so it renders + evaluates)', () => {
    expect(conditionShape('rolling_gt')).toBe('rolling');
    expect(operandSlots('rolling_gt')).toEqual(['operand']);
  });
  it('cross ops are not legacy', () => {
    for (const op of CROSS_OPS) expect(isLegacyOp(op)).toBe(false);
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

describe('isOperandComplete (conditionOps — input-naive)', () => {
  // Note: this is the backwards-compatible operand-level check. The
  // input_id → declared-input resolution check lives in blockShape.
  it('null → false', () => {
    expect(isOperandComplete(null)).toBe(false);
  });
  it('constant with finite value → true', () => {
    expect(isOperandComplete({ kind: 'constant', value: 0 })).toBe(true);
  });
  it('constant with NaN → false', () => {
    expect(isOperandComplete({ kind: 'constant', value: NaN })).toBe(false);
  });
  it('indicator requires indicator_id AND input_id', () => {
    expect(isOperandComplete({
      kind: 'indicator', indicator_id: 'sma', input_id: 'X',
    })).toBe(true);
    expect(isOperandComplete({
      kind: 'indicator', indicator_id: 'sma', input_id: '',
    })).toBe(false);
    expect(isOperandComplete({
      kind: 'indicator', indicator_id: '', input_id: 'X',
    })).toBe(false);
  });
  it('instrument requires a non-empty input_id', () => {
    expect(isOperandComplete({ kind: 'instrument', input_id: '' })).toBe(false);
    expect(isOperandComplete({ kind: 'instrument', input_id: 'X' })).toBe(true);
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
  it('rolling with instrument operand → true', () => {
    expect(isConditionComplete({
      op: 'rolling_gt',
      operand: { kind: 'instrument', input_id: 'X', field: 'close' },
      lookback: 5,
    })).toBe(true);
  });
});

describe('defaultIndicatorOperand — v3 shape', () => {
  it('returns shape with all override fields explicit', () => {
    expect(defaultIndicatorOperand()).toEqual({
      kind: 'indicator',
      indicator_id: null,
      input_id: '',
      output: null,
      params_override: null,
      series_override: null,
    });
  });
  it('is not complete (no defaults policy)', () => {
    expect(isOperandComplete(defaultIndicatorOperand())).toBe(false);
  });
});

describe('defaultInstrumentOperand — v3 shape', () => {
  it('returns {kind, input_id: "", field: "close"}', () => {
    expect(defaultInstrumentOperand()).toEqual({
      kind: 'instrument', input_id: '', field: 'close',
    });
  });
  it('is not complete before input_id is picked', () => {
    expect(isOperandComplete(defaultInstrumentOperand())).toBe(false);
  });
});

describe('migrateCondition — preserves compatible slots', () => {
  it('same shape preserves operands, only op changes', () => {
    const current = {
      op: 'gt',
      lhs: { kind: 'constant', value: 1 },
      rhs: { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' },
    };
    const next = migrateCondition(current, 'lt');
    expect(next).toEqual({ ...current, op: 'lt' });
  });
  it('binary → range preserves lhs into operand', () => {
    const current = {
      op: 'gt',
      lhs: { kind: 'indicator', indicator_id: 'rsi', input_id: 'X', output: 'default' },
      rhs: { kind: 'constant', value: 30 },
    };
    const next = migrateCondition(current, 'in_range');
    expect(next.op).toBe('in_range');
    expect(next.operand).toEqual(current.lhs);
    expect(next.max).toEqual(current.rhs);
    expect(next.min).toBeNull();
  });
  it('range → rolling preserves operand', () => {
    const current = {
      op: 'in_range',
      operand: { kind: 'indicator', indicator_id: 'x', input_id: 'X', output: 'default' },
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
    expect(migrateCondition(null, 'gt')).toEqual({ op: 'gt', lhs: null, rhs: null });
  });
  it('preserves indicator override fields when operator stays in the same shape', () => {
    const current = {
      op: 'gt',
      lhs: {
        kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default',
        params_override: { window: 50 }, series_override: { close: 'X' },
      },
      rhs: { kind: 'constant', value: 0 },
    };
    const next = migrateCondition(current, 'lt');
    expect(next.lhs).toEqual(current.lhs);
    expect(next.lhs.params_override).toEqual({ window: 50 });
    expect(next.lhs.series_override).toEqual({ close: 'X' });
  });

  it('gt → cross_above (both binary) adds count/window defaults', () => {
    const current = {
      op: 'gt',
      lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
      rhs: { kind: 'constant', value: 0 },
    };
    const next = migrateCondition(current, 'cross_above');
    expect(next.op).toBe('cross_above');
    expect(next.lhs).toEqual(current.lhs);
    expect(next.rhs).toEqual(current.rhs);
    expect(next.count).toBe(1);
    expect(next.window).toBe(1);
  });

  it('cross_above → gt drops the count/window scalars', () => {
    const current = {
      op: 'cross_above',
      lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
      rhs: { kind: 'constant', value: 0 },
      count: 3, window: 10,
    };
    const next = migrateCondition(current, 'gt');
    expect(next.op).toBe('gt');
    expect('count' in next).toBe(false);
    expect('window' in next).toBe(false);
    expect(next.lhs).toEqual(current.lhs);
  });

  it('cross_above → cross_below (cross↔cross) preserves count/window', () => {
    const current = {
      op: 'cross_above',
      lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
      rhs: { kind: 'constant', value: 0 },
      count: 4, window: 12,
    };
    const next = migrateCondition(current, 'cross_below');
    expect(next.op).toBe('cross_below');
    expect(next.count).toBe(4);
    expect(next.window).toBe(12);
  });

  it('in_range → cross_above (shape change) lands with count/window defaults', () => {
    const current = {
      op: 'in_range',
      operand: { kind: 'instrument', input_id: 'X', field: 'close' },
      min: { kind: 'constant', value: 0 },
      max: { kind: 'constant', value: 10 },
    };
    const next = migrateCondition(current, 'cross_above');
    expect(next.op).toBe('cross_above');
    expect(next.lhs).toEqual(current.operand); // operand → lhs preserved
    expect(next.count).toBe(1);
    expect(next.window).toBe(1);
  });
});
