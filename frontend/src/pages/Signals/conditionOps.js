// Canonical list of the 10 condition operators the backend understands.
// UI flows key off this list so adding a new op here is the only change
// needed on the frontend (assuming the backend also accepts it).

/** Binary comparators: take lhs + rhs. */
export const BINARY_COMPARE_OPS = Object.freeze(['gt', 'lt', 'ge', 'le', 'eq']);

/** Cross operators: take lhs + rhs, evaluated on the transition edge. */
export const CROSS_OPS = Object.freeze(['cross_above', 'cross_below']);

/** Range operator: takes operand + min + max. */
export const RANGE_OPS = Object.freeze(['in_range']);

/** Rolling comparators: take operand + lookback (int). */
export const ROLLING_OPS = Object.freeze(['rolling_gt', 'rolling_lt']);

/** Flat list of every supported op — shown in the op dropdown in order. */
export const ALL_OPS = Object.freeze([
  ...BINARY_COMPARE_OPS,
  ...CROSS_OPS,
  ...RANGE_OPS,
  ...ROLLING_OPS,
]);

/** Human-facing labels (shown in the op dropdown). */
export const OP_LABELS = Object.freeze({
  gt: '>',
  lt: '<',
  ge: '>=',
  le: '<=',
  eq: '==',
  cross_above: 'crosses above',
  cross_below: 'crosses below',
  in_range: 'in range',
  rolling_gt: 'rolling >',
  rolling_lt: 'rolling <',
});

/**
 * Structural shape of a condition given its operator. One of:
 *   - ``binary``   — { lhs, rhs }
 *   - ``range``    — { operand, min, max }
 *   - ``rolling``  — { operand, lookback }
 */
export function conditionShape(op) {
  if (BINARY_COMPARE_OPS.includes(op) || CROSS_OPS.includes(op)) return 'binary';
  if (RANGE_OPS.includes(op)) return 'range';
  if (ROLLING_OPS.includes(op)) return 'rolling';
  return 'binary';
}

/**
 * Build a default condition object for a given op.
 *
 * IMPORTANT — iter-2 policy: a freshly-added condition must NOT inject any
 * default instrument / indicator / constant into its operand slots. Every
 * operand starts as ``null`` ("unset") and the user must explicitly pick
 * something before Run is enabled. Structural (non-operand) fields like
 * ``lookback`` still get a sane numeric default since they have no
 * "please pick" UI.
 */
export function defaultCondition(op = 'gt') {
  const shape = conditionShape(op);
  if (shape === 'range') {
    return { op, operand: null, min: null, max: null };
  }
  if (shape === 'rolling') {
    return { op, operand: null, lookback: 1 };
  }
  return { op, lhs: null, rhs: null };
}

/**
 * Return the list of operand-slot names for a given condition shape.
 * Used by Run-validation to detect unset operands.
 */
export function operandSlots(op) {
  const shape = conditionShape(op);
  if (shape === 'range') return ['operand', 'min', 'max'];
  if (shape === 'rolling') return ['operand'];
  return ['lhs', 'rhs'];
}

/**
 * Return ``true`` if the operand is "fully specified" — meaning Run can
 * ship it to the backend without tripping validation. Unset (``null``)
 * and instrument stubs with empty ``collection``/``instrument_id`` count
 * as incomplete. An indicator operand with empty ``indicator_id`` is also
 * incomplete.
 */
export function isOperandComplete(operand) {
  if (!operand || typeof operand !== 'object') return false;
  if (operand.kind === 'constant') return Number.isFinite(operand.value);
  if (operand.kind === 'indicator') {
    return typeof operand.indicator_id === 'string' && operand.indicator_id.length > 0;
  }
  if (operand.kind === 'instrument') {
    return typeof operand.collection === 'string' && operand.collection.length > 0
      && typeof operand.instrument_id === 'string' && operand.instrument_id.length > 0;
  }
  return false;
}

/** Return ``true`` if every operand slot on the condition is complete. */
export function isConditionComplete(condition) {
  if (!condition || typeof condition !== 'object') return false;
  for (const slot of operandSlots(condition.op)) {
    if (!isOperandComplete(condition[slot])) return false;
  }
  return true;
}

/**
 * Migrate a condition's payload when the user switches its operator.
 * Preserves any operands that have a compatible slot in the target shape
 * (indicator-on-lhs stays indicator-on-lhs; for range↔binary we map
 * ``operand``↔``lhs``). Anything that doesn't map cleanly falls back to
 * the default from ``defaultCondition``.
 */
export function migrateCondition(current, nextOp) {
  if (!current || typeof current !== 'object') return defaultCondition(nextOp);
  const nextShape = conditionShape(nextOp);
  const base = defaultCondition(nextOp);
  const currShape = conditionShape(current.op);
  if (nextShape === currShape) {
    return { ...current, op: nextOp };
  }
  // Try to preserve the most-structurally-similar slot — but never fabricate
  // a default operand: if nothing compatible carries over, the slot stays
  // ``null`` (unset) per iter-2 policy.
  if (nextShape === 'binary') {
    const lhs = current.lhs || current.operand || base.lhs;
    const rhs = current.rhs || current.max || base.rhs;
    return { op: nextOp, lhs, rhs };
  }
  if (nextShape === 'range') {
    const operand = current.operand || current.lhs || base.operand;
    return {
      op: nextOp,
      operand,
      min: current.min || base.min,
      max: current.max || current.rhs || base.max,
    };
  }
  if (nextShape === 'rolling') {
    const operand = current.operand || current.lhs || base.operand;
    const lookback = Number.isFinite(current.lookback) ? current.lookback : 1;
    return { op: nextOp, operand, lookback };
  }
  return base;
}
