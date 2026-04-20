// Pure data helpers for Block-level validation.
//
// These helpers are the single source of truth for the question
// "is this block complete enough to Run?". They are imported by both
// the UI (to gate the Run button and per-row state) and by the request
// builder (pre-submit validation). No React imports — unit-testable in
// isolation.
//
// A Block's v2 shape is
//   { instrument: {collection, instrument_id} | null,
//     weight:     number (finite, non-negative),
//     conditions: Condition[] }
// Each Condition has a discriminated shape keyed off its ``op`` (see
// conditionOps.js § conditionShape / operandSlots).
//
// Note: this module intentionally does NOT re-export the operand /
// condition helpers from conditionOps.js — it provides its OWN
// ``isOperandComplete`` because the brief for the Run-gate requires
// stricter validation of the ``instrument`` operand (``field`` must be
// set, not just collection + instrument_id). conditionOps.isOperandComplete
// is looser (it does not require ``field``) and is kept for backwards
// compatibility with iter-2 UI state.

import { operandSlots } from './conditionOps';

/**
 * Build a brand-new empty block.
 *   - instrument: null        — user must pick before Run.
 *   - weight: 0               — sensible non-negative default.
 *   - conditions: []          — empty until user adds rows.
 *
 * Weight of 0 is deliberately the default (per iter-3 "no defaults"
 * spirit — the weight slider / input is the user's explicit knob).
 */
export function defaultBlock() {
  return { instrument: null, weight: 0, conditions: [] };
}

/**
 * Return true iff an operand is fully specified for submission.
 *
 * Per operand kind:
 *   - indicator: ``indicator_id`` is a non-empty string.
 *   - instrument: ``collection`` + ``instrument_id`` + ``field`` are all
 *     non-empty strings.
 *   - constant: ``value`` is finite.
 *
 * Unknown / null operands ⇒ false.
 */
export function isOperandComplete(operand) {
  if (!operand || typeof operand !== 'object') return false;
  if (operand.kind === 'constant') {
    return Number.isFinite(operand.value);
  }
  if (operand.kind === 'indicator') {
    return typeof operand.indicator_id === 'string' && operand.indicator_id.length > 0;
  }
  if (operand.kind === 'instrument') {
    return typeof operand.collection === 'string' && operand.collection.length > 0
      && typeof operand.instrument_id === 'string' && operand.instrument_id.length > 0
      && typeof operand.field === 'string' && operand.field.length > 0;
  }
  return false;
}

/**
 * Return true iff every operand slot on a condition is complete.
 * Reuses the operand-slot list declared in conditionOps so new ops
 * automatically plug into the Run-gate.
 */
export function isConditionComplete(condition) {
  if (!condition || typeof condition !== 'object') return false;
  if (typeof condition.op !== 'string' || !condition.op) return false;
  for (const slot of operandSlots(condition.op)) {
    if (!isOperandComplete(condition[slot])) return false;
  }
  return true;
}

/**
 * Return true iff a block can be submitted to the backend:
 *   - instrument picked (non-null ``{collection, instrument_id}``)
 *   - at least one condition
 *   - every condition complete (all operand slots filled)
 *   - every indicator operand has a non-empty ``indicator_id``
 *     (implied by isConditionComplete → isOperandComplete, but spelled
 *     out in the docstring for readers).
 *
 * ENTRY directions additionally require a strictly positive weight
 * (PROB-2 fix, Review #1): the backend's ``_is_usable_block`` drops
 * entry blocks with ``weight <= 0``, so the UI must not promise a Run
 * that the backend will silently discard. EXIT directions do not gate
 * on weight — the backend ignores weight on ``*_exit`` blocks.
 *
 * @param {Object} block
 * @param {string} [direction] — one of ``long_entry`` / ``long_exit`` /
 *   ``short_entry`` / ``short_exit``. Only ``*_entry`` values switch on
 *   the weight check. Any other value (including ``undefined``) skips
 *   the weight gate for backwards compatibility with non-directional
 *   callers (tests, tooling).
 */
export function isBlockRunnable(block, direction) {
  if (!block || typeof block !== 'object') return false;
  const inst = block.instrument;
  if (!inst || typeof inst !== 'object') return false;
  if (typeof inst.collection !== 'string' || !inst.collection) return false;
  if (typeof inst.instrument_id !== 'string' || !inst.instrument_id) return false;
  if (!Array.isArray(block.conditions) || block.conditions.length === 0) return false;
  for (const c of block.conditions) {
    if (!isConditionComplete(c)) return false;
  }
  // PROB-2 fix: entry blocks require weight > 0. Backend silently skips
  // entry blocks with weight <= 0 (signal_exec._is_usable_block); if the
  // UI considered them runnable the Run button would lie to the user.
  if (direction === 'long_entry' || direction === 'short_entry') {
    if (!Number.isFinite(block.weight) || block.weight <= 0) return false;
  }
  return true;
}
